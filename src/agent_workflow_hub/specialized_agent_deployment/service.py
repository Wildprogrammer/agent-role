"""Orchestrate deterministic specialized-agent deployment lifecycles."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import cast

from agent_workflow_hub.frontmatter import parse_markdown

from .contracts import (
    DeploymentManifest,
    DeploymentPlan,
    DeploymentRequest,
    SkillSnapshot,
    VerificationResult,
    WriteIntent,
    read_json_object,
)
from .filesystem import MANAGED_MARKER, TransactionOutcomeUnknown, _atomic_replace
from .hosts.base import ApplyContext, FinalizeContext, HostAdapter, VerifyContext
from .hosts.deepseek_harness import DeepSeekHarnessAdapter
from .hosts.hermes import HermesAdapter
from .planning import build_deployment_plan, planned_manifest
from .rendering import render_deployment_preview, render_persona
from .runner import CommandRunner
from .sources import resolve_skill_source, snapshot_composition
from .runtime_bundle import plan_runtime, prepare_runtime, verify_runtime

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
AdapterFactory = Callable[[DeploymentRequest, Path], HostAdapter]


class DeploymentServiceError(RuntimeError):
    """Raised when a bounded deployment lifecycle operation cannot complete."""


class StaleDeploymentConfirmation(DeploymentServiceError):
    """Raised before host writes when the confirmed deterministic plan changed."""


def _require_absolute(path: Path, label: str) -> Path:
    if not isinstance(path, Path) or not path.is_absolute():
        raise DeploymentServiceError(f"{label} must be an absolute Path")
    return path.resolve(strict=False)


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_unknown_outcome(error: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, TransactionOutcomeUnknown):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


class DeploymentService:
    """Coordinate preview, one SHA-bound apply, and layered verification."""

    def __init__(self, *, adapter_factory: AdapterFactory | None = None) -> None:
        self._adapter_factory = adapter_factory or self._default_adapter

    @staticmethod
    def _default_adapter(
        request: DeploymentRequest,
        staging_root: Path,
    ) -> HostAdapter:
        runner = CommandRunner()
        if request.host == "hermes":
            return HermesAdapter(runner, staging_root=staging_root)
        if request.host == "deepseek-harness":
            return DeepSeekHarnessAdapter(runner)
        raise DeploymentServiceError(f"unsupported host: {request.host}")

    @staticmethod
    def _roots(hub_root: Path, deployment_id: str) -> tuple[Path, Path]:
        output_root = (
            hub_root
            / "workflows"
            / "specialized-agent-deployment"
            / "outputs"
            / deployment_id
        )
        staging_root = (
            hub_root
            / "workspace"
            / "workflows"
            / "specialized-agent-deployment"
            / deployment_id
        )
        return output_root, staging_root

    def _prepare(
        self,
        hub_root: Path,
        request: DeploymentRequest,
    ) -> tuple[DeploymentPlan, HostAdapter, Path, Path]:
        output_root, staging_root = self._roots(hub_root, request.deployment_id)
        snapshots = snapshot_composition(hub_root, request)
        adapter = self._adapter_factory(request, staging_root)
        facts = adapter.discover(request)
        if facts.compatibility in {"verified", "conditional"}:
            mapped = {item["workflow"] for item in request.host_options.get("mcp_servers", ())}
            missing = []
            for snapshot in snapshots:
                source = resolve_skill_source(hub_root, snapshot.selection) / "SKILL.md"
                frontmatter, _ = parse_markdown(source)
                entrypoints = frontmatter.get("metadata", {}).get("entrypoints", "{}")
                if isinstance(entrypoints, str):
                    entrypoints = json.loads(entrypoints)
                if entrypoints.get("mcp") and snapshot.selection.name not in mapped:
                    missing.append(snapshot.selection.name)
            if missing:
                facts = replace(facts, compatibility="compatible_not_runnable", facts={
                    **facts.facts, "missing_mcp_workflows": missing,
                    "guidance": ["所选工作流声明了 MCP 入口；按所属 Skill 补充 mcp_servers 接入映射后重新预览。"],
                })
        runtime = None
        if request.runtime is not None:
            runtime = plan_runtime(hub_root, request, snapshots)
            if facts.target_root:
                target, release = Path(facts.target_root), Path(runtime["destination"])
                if release.is_relative_to(target) or target.is_relative_to(release):
                    raise DeploymentServiceError("runtime release and host Profile/Preset must be separate directories")
            facts = replace(facts, facts={**facts.facts, "runtime": runtime})
        persona = render_persona(request, snapshots)
        writes = adapter.plan_writes(request, snapshots, persona, facts)
        if runtime is not None and writes:
            runtime_description = (
                "准备独立运行副本：系统 Python 加副本本地依赖，验证成功后才切换宿主；旧版本保留"
                if runtime["mode"] == "system-source"
                else "准备独立运行副本和私有 Python 环境，验证成功后才切换宿主；旧版本保留"
            )
            writes = (WriteIntent(
                target=runtime["destination"], action="command", content_sha256=runtime["sha256"],
                size=sum(item["size"] for item in runtime["files"]),
                description=runtime_description,
                parameters={"kind": "runtime-prepare", "commands": runtime["commands"]},
            ), *writes)
        plan = build_deployment_plan(request, snapshots, facts, writes)
        self._stage_file_payloads(hub_root, staging_root, plan)
        return plan, adapter, output_root, staging_root

    @staticmethod
    def _staging_target(staging_root: Path, relative: str) -> Path:
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or pure.as_posix() != relative
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise DeploymentServiceError("staging_relative must be normalized POSIX")
        target = staging_root.joinpath(*pure.parts).resolve(strict=False)
        if staging_root != target and staging_root not in target.parents:
            raise DeploymentServiceError("staging path escaped its deployment root")
        return target

    @staticmethod
    def _skill_payload(
        hub_root: Path,
        snapshots: tuple[SkillSnapshot, ...],
        relative: str,
    ) -> bytes:
        prefix = "host-files/skills/"
        if not relative.startswith(prefix):
            raise DeploymentServiceError("Skill payload has an invalid staging path")
        remainder = relative[len(prefix):]
        name, separator, file_relative = remainder.partition("/")
        if not separator or not file_relative:
            raise DeploymentServiceError("Skill payload is missing its relative file")
        snapshot = next(
            (item for item in snapshots if item.selection.name == name),
            None,
        )
        if snapshot is None:
            raise DeploymentServiceError(f"unknown staged Skill: {name}")
        record = next(
            (item for item in snapshot.files if item.relative_path == file_relative),
            None,
        )
        if record is None:
            raise DeploymentServiceError(f"unplanned staged Skill file: {relative}")
        source_root = resolve_skill_source(hub_root, snapshot.selection)
        source = source_root.joinpath(*PurePosixPath(file_relative).parts)
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise DeploymentServiceError(f"cannot stage Skill file: {source}: {exc}") from None
        if len(content) != record.size or hashlib.sha256(content).hexdigest() != record.sha256:
            raise DeploymentServiceError(f"Skill source drifted while staging: {source}")
        return content

    @staticmethod
    def _marker_payload(request: DeploymentRequest) -> bytes:
        return _json_bytes(
            {
                "schema_version": request.schema_version,
                "deployment_id": request.deployment_id,
                "agent_id": request.agent_id,
                "host": request.host,
            }
        )

    def _payload_for_intent(
        self,
        hub_root: Path,
        plan: DeploymentPlan,
        intent: WriteIntent,
    ) -> bytes | None:
        relative = cast(str, intent.parameters.get("staging_relative"))
        kind = intent.parameters.get("payload_kind")
        if kind == "persona" or relative == "host-files/AGENTS.md":
            return plan.persona.encode("utf-8")
        if kind == "skill" or relative.startswith("host-files/skills/"):
            return self._skill_payload(hub_root, plan.snapshots, relative)
        if kind == "marker" or Path(intent.target).name == MANAGED_MARKER:
            return self._marker_payload(plan.request)
        # DeepSeek Harness renders its two template-derived files inside its
        # adapter and verifies them against the plan digest before host writes.
        return None

    def _stage_file_payloads(
        self,
        hub_root: Path,
        staging_root: Path,
        plan: DeploymentPlan,
    ) -> None:
        for intent in plan.writes:
            if intent.parameters.get("kind") != "file":
                continue
            relative = intent.parameters.get("staging_relative")
            if type(relative) is not str:
                raise DeploymentServiceError("file intent is missing staging_relative")
            payload = self._payload_for_intent(hub_root, plan, intent)
            if payload is None:
                continue
            if len(payload) != intent.size or hashlib.sha256(payload).hexdigest() != intent.content_sha256:
                raise DeploymentServiceError(f"staged payload does not match plan: {intent.target}")
            _atomic_replace(self._staging_target(staging_root, relative), payload)

    @staticmethod
    def _write_manifest(path: Path, manifest: DeploymentManifest) -> None:
        _atomic_replace(path, _json_bytes(manifest.to_mapping()))

    @staticmethod
    def _write_verification(path: Path, result: VerificationResult) -> None:
        _atomic_replace(path, _json_bytes(result.to_mapping()))

    def preview(self, hub_root: Path, request_path: Path) -> DeploymentManifest:
        hub_root = _require_absolute(hub_root, "hub_root")
        request_path = _require_absolute(request_path, "request_path")
        try:
            request = DeploymentRequest.from_mapping(read_json_object(request_path))
            plan, _, output_root, _ = self._prepare(hub_root, request)
            manifest = planned_manifest(plan)
            _atomic_replace(
                output_root / "deployment-preview.md",
                render_deployment_preview(plan).encode("utf-8"),
            )
            self._write_manifest(output_root / "deployment-manifest.json", manifest)
            return manifest
        except DeploymentServiceError:
            raise
        except Exception as exc:
            raise DeploymentServiceError(f"deployment preview failed: {exc}") from exc

    def apply(
        self,
        hub_root: Path,
        manifest_path: Path,
        confirmed_plan_sha256: str,
    ) -> DeploymentManifest:
        hub_root = _require_absolute(hub_root, "hub_root")
        manifest_path = _require_absolute(manifest_path, "manifest_path")
        if _SHA256_RE.fullmatch(confirmed_plan_sha256) is None:
            raise StaleDeploymentConfirmation(
                "confirmed_plan_sha256 must be a lowercase SHA-256"
            )
        original = DeploymentManifest.from_mapping(read_json_object(manifest_path))
        if original.status != "planned":
            raise DeploymentServiceError("only a planned manifest can be applied")
        if confirmed_plan_sha256 != original.plan_sha256:
            raise StaleDeploymentConfirmation("confirmed plan SHA does not match manifest")
        try:
            plan, adapter, output_root, staging_root = self._prepare(
                hub_root, original.request
            )
        except Exception as exc:
            raise StaleDeploymentConfirmation(
                f"deployment inputs changed since preview: {exc}"
            ) from exc
        if plan.plan_sha256 != original.plan_sha256:
            raise StaleDeploymentConfirmation(
                "deployment plan changed since preview; generate and confirm a new preview"
            )

        applying = replace(original, status="applying", updated_at=_now())
        self._write_manifest(manifest_path, applying)
        backup_root = staging_root / "backups" / plan.plan_sha256
        try:
            if plan.request.runtime is not None:
                prepare_runtime(plan.host_facts.facts["runtime"])
            outcome = adapter.apply(
                ApplyContext(
                    plan=plan,
                    manifest=applying,
                    staging_root=staging_root,
                    backup_root=backup_root,
                )
            )
        except Exception as exc:
            if _has_unknown_outcome(exc):
                unknown = replace(
                    applying,
                    status="outcome_unknown",
                    updated_at=_now(),
                )
                self._write_manifest(manifest_path, unknown)
                return unknown
            rolled_back = replace(applying, status="rolled_back", updated_at=_now())
            self._write_manifest(manifest_path, rolled_back)
            raise DeploymentServiceError(f"deployment apply failed: {exc}") from exc

        if outcome.status != "applied":
            terminal = replace(applying, status=outcome.status, updated_at=_now())
            self._write_manifest(manifest_path, terminal)
            return terminal

        applied = replace(applying, status="applied", updated_at=_now())
        self._write_manifest(manifest_path, applied)
        verification = adapter.verify(
            VerifyContext(
                manifest=applied,
                staging_root=staging_root,
                behavior_evidence_path=None,
            )
        )
        verification = self._runtime_verification(applied, verification)
        finalize = getattr(adapter, "finalize", None)
        if callable(finalize):
            verification = finalize(
                FinalizeContext(
                    apply_result=outcome,
                    verification=verification,
                )
            )
        self._write_verification(output_root / "verification.json", verification)
        if verification.status in {"verified", "partially_verified"}:
            final = replace(applied, status=verification.status, updated_at=_now())
            self._write_manifest(manifest_path, final)
            return final
        return applied

    def verify(
        self,
        hub_root: Path,
        manifest_path: Path,
        behavior_evidence_path: Path | None,
    ) -> VerificationResult:
        hub_root = _require_absolute(hub_root, "hub_root")
        manifest_path = _require_absolute(manifest_path, "manifest_path")
        if behavior_evidence_path is not None:
            behavior_evidence_path = _require_absolute(
                behavior_evidence_path, "behavior_evidence_path"
            )
        manifest = DeploymentManifest.from_mapping(read_json_object(manifest_path))
        output_root, staging_root = self._roots(hub_root, manifest.deployment_id)
        adapter = self._adapter_factory(manifest.request, staging_root)
        result = adapter.verify(
            VerifyContext(
                manifest=manifest,
                staging_root=staging_root,
                behavior_evidence_path=behavior_evidence_path,
            )
        )
        result = self._runtime_verification(manifest, result)
        self._write_verification(output_root / "verification.json", result)
        if manifest.status in {"applied", "partially_verified", "verified"} and result.status in {
            "verified",
            "partially_verified",
        }:
            updated = replace(manifest, status=result.status, updated_at=_now())
            self._write_manifest(manifest_path, updated)
        return result

    @staticmethod
    def _runtime_verification(manifest: DeploymentManifest, result: VerificationResult) -> VerificationResult:
        if manifest.request.runtime is None:
            return result
        try:
            runtime = verify_runtime(manifest.host_facts.facts["runtime"])
        except Exception as exc:
            runtime = {"status": "failed", "error": type(exc).__name__}
            return replace(result, status="failed", static={**result.static, "runtime": runtime})
        return replace(result, static={**result.static, "runtime": runtime})


__all__ = [
    "DeploymentService",
    "DeploymentServiceError",
    "StaleDeploymentConfirmation",
]
