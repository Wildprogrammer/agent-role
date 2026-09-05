"""DeepSeek Harness 0.1.2-alpha.2 Web user-Preset adapter."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from ..contracts import (
    DeploymentManifest,
    DeploymentRequest,
    HostFacts,
    SkillSelection,
    SkillSnapshot,
    VerificationResult,
    WriteIntent,
    read_json_object,
)
from ..filesystem import (
    MANAGED_MARKER,
    ManagedWrite,
    TransactionApplyError,
    apply_managed_transaction,
    validate_managed_target,
)
from ..runner import CommandRunner
from ..sources import SourceSnapshotError, snapshot_skill
from .base import ApplyContext, FinalizeContext, HostApplyResult, VerifyContext


@dataclass(frozen=True, kw_only=True)
class DeepSeekHarnessEvidence:
    version: str
    commit: str
    agent_template_sha256: str
    preset_template_sha256: str


SUPPORTED_DSH = DeepSeekHarnessEvidence(
    version="0.1.2-alpha.2",
    commit="0a53fb55bea101816fa226bb964ae2bed71c343b",
    agent_template_sha256="f04fbc6ec6d38aab78f18690c293ddcb76293107f7e6cd157904b7c0e83094bd",
    preset_template_sha256="3c61b4ce68e5dd5cb2c099693fdcb30b91d5f22bbbef546e233321b0fa68f0e4",
)
_OFFICIAL_ORIGINS = frozenset(
    (
        "https://github.com/deepseek-ai/deepseek-harness.git",
        "git@github.com:deepseek-ai/deepseek-harness.git",
    )
)
_BUILD_ARTIFACTS = (
    "apps/cli/lib/bin.js",
    "packages/bundle/base/lib/index.js",
    "packages/bundle/web-app/lib/index.js",
    "apps/web/dist/index.html",
)
_AGENT_TEMPLATE_RELATIVE = "packages/preset/agent-presets/presets/standard/agent.cordis.yml"
_PRESET_TEMPLATE_RELATIVE = "packages/preset/agent-presets/presets/standard/preset.yml"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MCP_PLUGIN = "@deepseek-ai/dsh-mcp-client"
_MCP_ARTIFACT = "packages/mcp/mcp-client/lib/index.js"


class DeepSeekHarnessAdapterError(RuntimeError):
    """Raised when a DSH target cannot be transformed safely."""


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stable_bytes(path: Path) -> bytes:
    try:
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise DeepSeekHarnessAdapterError(f"cannot read DSH evidence {path}: {exc}") from None
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or path.is_symlink():
        raise DeepSeekHarnessAdapterError(f"DSH evidence drifted or is linked: {path}")
    return content


def _replace_row(source: str, row_id: str, replacement: str) -> str:
    lines = source.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == f"- id: {row_id}"]
    if len(starts) != 1:
        raise DeepSeekHarnessAdapterError(f"expected one {row_id} row")
    start = starts[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("- id: "):
            end = index
            break
    newline = "\r\n" if "\r\n" in source else "\n"
    replacement_text = replacement.replace("\n", newline).rstrip(newline) + newline
    return "".join((*lines[:start], replacement_text, *lines[end:]))


def patch_standard_agent_template(
    source: str,
    *,
    persona: str,
    provider_name: str,
    skill_root: Path,
) -> str:
    """Replace only the standard template's persona and Skill filesystem rows."""

    if type(source) is not str or type(persona) is not str or not persona.strip():
        raise DeepSeekHarnessAdapterError("template and persona must be nonblank text")
    if type(provider_name) is not str or not re.fullmatch(r"[a-z0-9-]+", provider_name):
        raise DeepSeekHarnessAdapterError("provider_name must be kebab-case")
    if not isinstance(skill_root, Path) or not skill_root.is_absolute():
        raise DeepSeekHarnessAdapterError("skill_root must be absolute")
    persona_lines = "\n".join(f"      {line}" for line in persona.rstrip("\n").splitlines())
    persona_row = (
        "- id: persona\n"
        "  name: '@deepseek-ai/dsh-persona'\n"
        "  config:\n"
        "    text: |-\n"
        f"{persona_lines}\n"
    )
    patched = _replace_row(source, "persona", persona_row)
    quoted_root = json.dumps(str(skill_root), ensure_ascii=False)
    skill_row = (
        "- id: skill-filesystem\n"
        "  name: '@deepseek-ai/dsh-skill-filesystem'\n"
        "  config:\n"
        f"    providerName: {provider_name}\n"
        "    includeDefaultRoots: false\n"
        "    customSkillDirs:\n"
        f"      - {quoted_root}\n"
        "    watch: false\n"
    )
    return _replace_row(patched, "skill-filesystem", skill_row)


def _render_preset(request: DeploymentRequest) -> str:
    return (
        f"name: {json.dumps(request.display_name, ensure_ascii=False)}\n"
        f"description: {json.dumps(request.purpose, ensure_ascii=False)}\n"
        "order: 100\n"
    )


def _mcp_servers(request: DeploymentRequest, *, deployed: bool = False) -> tuple[dict[str, object], ...]:
    from ..mcp_bindings import mcp_servers
    return mcp_servers(request, deployed=deployed, error_type=DeepSeekHarnessAdapterError)


def _agent_rows(source: str) -> list[dict[str, object]]:
    # BaseLoader reads !!js as inert text, never executes or reconstructs it.
    try:
        rows = yaml.load(source, Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        raise DeepSeekHarnessAdapterError("invalid DSH agent YAML") from None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise DeepSeekHarnessAdapterError("DSH agent YAML must be a row list")
    return rows


def _mcp_row(server: Mapping[str, object]) -> str:
    config = {"serverName": server["server_name"], "transport": "stdio",
              "command": server["command"], "args": server["args"], "cwd": server["cwd"]}
    return (f"- id: hub-mcp-{server['server_name']}\n"
            f"  name: '{_MCP_PLUGIN}'\n"
            f"  config: {json.dumps(config, ensure_ascii=False)}\n")


def _patch_mcp_servers(source: str, servers: tuple[dict[str, object], ...]) -> str:
    if not servers:
        return source
    for server in servers:
        row_id = f"hub-mcp-{server['server_name']}"
        rows = _agent_rows(source)
        matches = [row for row in rows if row.get("id") == row_id]
        if len(matches) > 1 or (matches and matches[0].get("name") != _MCP_PLUGIN):
            raise DeepSeekHarnessAdapterError("MCP row id conflicts with an existing plugin")
        for row in rows:
            config = row.get("config", {})
            if (row.get("id") != row_id and row.get("name") == _MCP_PLUGIN
                    and isinstance(config, dict) and config.get("serverName") == server["server_name"]):
                raise DeepSeekHarnessAdapterError("MCP namespace already belongs to another row")
        replacement = _mcp_row(server)
        if matches:
            source = _replace_row(source, row_id, replacement)
        else:
            newline = "\r\n" if "\r\n" in source else "\n"
            source = source.rstrip("\r\n") + newline + newline + replacement.replace("\n", newline)
    return source


def _agent_source(request: DeploymentRequest, facts: HostFacts) -> str:
    path = (Path(facts.target_root or "") / "agent.cordis.yml"
            if request.mode == "update" else Path(cast(str, facts.facts["agent_template"])))
    return _stable_bytes(path).decode("utf-8")


def _marker_bytes(request: DeploymentRequest) -> bytes:
    return json.dumps(
        {
            "schema_version": request.schema_version,
            "deployment_id": request.deployment_id,
            "agent_id": request.agent_id,
            "host": "deepseek-harness",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass(frozen=True, kw_only=True)
class WebBehaviorEvidence:
    session_id: str
    preset_id: str
    prompt_sha256: str
    response_sha256: str
    identity: str
    primary_workflow: str
    first_action: str

    def __post_init__(self) -> None:
        for field in ("session_id", "preset_id", "identity", "primary_workflow", "first_action"):
            value = getattr(self, field)
            if type(value) is not str or not value.strip():
                raise DeepSeekHarnessAdapterError(f"{field} must be nonblank")
        for field in ("prompt_sha256", "response_sha256"):
            if _SHA256_RE.fullmatch(getattr(self, field)) is None:
                raise DeepSeekHarnessAdapterError(f"{field} must be SHA-256")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WebBehaviorEvidence:
        fields = {
            "session_id", "preset_id", "prompt_sha256", "response_sha256",
            "identity", "primary_workflow", "first_action",
        }
        if not isinstance(value, Mapping) or set(value) != fields:
            raise DeepSeekHarnessAdapterError("invalid Web behavior evidence fields")
        return cls(**{field: cast(str, value[field]) for field in fields})

    def to_mapping(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "preset_id": self.preset_id,
            "prompt_sha256": self.prompt_sha256,
            "response_sha256": self.response_sha256,
            "identity": self.identity,
            "primary_workflow": self.primary_workflow,
            "first_action": self.first_action,
        }


class DeepSeekHarnessAdapter:
    kind = "deepseek-harness"

    def __init__(
        self,
        runner: CommandRunner,
        *,
        evidence: DeepSeekHarnessEvidence = SUPPORTED_DSH,
    ) -> None:
        self._runner = runner
        self._evidence = evidence

    @staticmethod
    def _options(request: DeploymentRequest) -> tuple[Path, Path]:
        options = request.host_options
        required = {"dsh_home", "runtime_root", "profile"}
        if not required <= set(options) or set(options) - required - {"mcp_servers"} or options.get("profile") != "web":
            raise DeepSeekHarnessAdapterError(
                "DeepSeek host_options require dsh_home, runtime_root, profile=web; optional mcp_servers"
            )
        dsh_home = Path(cast(str, options["dsh_home"]))
        runtime_root = Path(cast(str, options["runtime_root"]))
        if not dsh_home.is_absolute() or not runtime_root.is_absolute():
            raise DeepSeekHarnessAdapterError("DeepSeek host roots must be absolute")
        return dsh_home, runtime_root

    def discover(self, request: DeploymentRequest) -> HostFacts:
        if request.host != "deepseek-harness":
            raise DeepSeekHarnessAdapterError("request is not for DeepSeek Harness")
        dsh_home, runtime_root = self._options(request)
        servers = _mcp_servers(request)
        required_evidence = (
            runtime_root / "package.json",
            runtime_root / _AGENT_TEMPLATE_RELATIVE,
            runtime_root / _PRESET_TEMPLATE_RELATIVE,
            dsh_home / "profiles/web/package.json",
        )
        missing_evidence = [str(path) for path in required_evidence if not path.is_file()]
        if not runtime_root.is_dir() or missing_evidence:
            return HostFacts(
                host="deepseek-harness",
                compatibility="missing",
                version=None,
                target_root=str(dsh_home / ".agent-presets" / request.agent_id),
                facts={
                    "missing_evidence": missing_evidence,
                    "guidance": [
                        "请先按 DeepSeek Harness 官方文档准备既有 runtime 和 web Profile。"
                    ],
                },
            )
        package = read_json_object(runtime_root / "package.json")
        version = cast(str | None, package.get("version"))
        commands = {
            "origin": ("git", "-C", str(runtime_root), "remote", "get-url", "origin"),
            "commit": ("git", "-C", str(runtime_root), "rev-parse", "HEAD"),
            "dirty": ("git", "-C", str(runtime_root), "status", "--porcelain"),
        }
        results = {
            name: self._runner.run(argv, phase="preview")
            for name, argv in commands.items()
        }
        origin = results["origin"].stdout.strip()
        commit = results["commit"].stdout.strip()
        dirty = results["dirty"].stdout.strip()
        agent_path = runtime_root / _AGENT_TEMPLATE_RELATIVE
        preset_path = runtime_root / _PRESET_TEMPLATE_RELATIVE
        agent_sha = _sha256(_stable_bytes(agent_path))
        preset_sha = _sha256(_stable_bytes(preset_path))
        profile_path = dsh_home / "profiles/web/package.json"
        profile = read_json_object(profile_path)
        bundles = (
            profile.get("dsh", {}).get("profile", {}).get("bundles", [])
            if isinstance(profile.get("dsh"), Mapping)
            and isinstance(cast(Mapping[str, object], profile["dsh"]).get("profile"), Mapping)
            else []
        )
        web_profile_ok = set(cast(list[str], bundles)) >= {
            "@deepseek-ai/dsh-base",
            "@deepseek-ai/dsh-web-app",
        }
        exact = (
            version == self._evidence.version
            and commit == self._evidence.commit
            and _COMMIT_RE.fullmatch(commit) is not None
            and origin in _OFFICIAL_ORIGINS
            and not dirty
            and agent_sha == self._evidence.agent_template_sha256
            and preset_sha == self._evidence.preset_template_sha256
            and web_profile_ok
        )
        artifacts = (*_BUILD_ARTIFACTS, *((_MCP_ARTIFACT,) if servers else ()))
        missing = [relative for relative in artifacts if not (runtime_root / relative).is_file()]
        missing_mcp_paths = [str(server[field]) for server in servers for field in ("command", "cwd")
                             if not (Path(cast(str, server[field])).is_file() if field == "command"
                                     else Path(cast(str, server[field])).is_dir())]
        compatibility = (
            "verified" if exact and not missing and not missing_mcp_paths else
            "compatible_not_runnable" if exact else
            "unverified"
        )
        facts: dict[str, object] = {
            "runtime_root": str(runtime_root),
            "dsh_home": str(dsh_home),
            "origin": origin,
            "commit": commit,
            "dirty": bool(dirty),
            "agent_template": str(agent_path),
            "agent_template_sha256": agent_sha,
            "preset_template": str(preset_path),
            "preset_template_sha256": preset_sha,
            "web_profile_package": str(profile_path),
            "web_profile_ok": web_profile_ok,
            "missing_build_artifacts": missing,
            "mcp_servers": list(servers),
            "missing_mcp_paths": missing_mcp_paths,
        }
        if compatibility != "verified":
            facts["guidance"] = (
                ["请按 DeepSeek Harness 官方文档在仓库外单独准备构建产物，然后重新预览。"]
                if compatibility == "compatible_not_runnable"
                else ["版本、提交、模板摘要或 Web Profile 与已验证基线不一致。"]
            )
        return HostFacts(
            host="deepseek-harness",
            compatibility=compatibility,
            version=version,
            target_root=str(dsh_home / ".agent-presets" / request.agent_id),
            facts=facts,
        )

    def behavior_prompt(self, request: DeploymentRequest) -> str:
        return (
            "不要调用任何工具。仅返回 JSON："
            f'{{"identity":"{request.agent_id}",'
            f'"primary_workflow":"{request.primary_workflow}",'
            '"first_action":"完整加载主工作流"}'
        )

    def behavior_prompt_sha256(self, request: DeploymentRequest) -> str:
        return _sha256(self.behavior_prompt(request).encode("utf-8"))

    def plan_writes(
        self,
        request: DeploymentRequest,
        snapshots: tuple[SkillSnapshot, ...],
        persona: str,
        facts: HostFacts,
    ) -> tuple[WriteIntent, ...]:
        if facts.compatibility != "verified":
            return ()
        target_root = Path(facts.target_root or "")
        if request.mode == "create" and (target_root.exists() or target_root.is_symlink()):
            raise DeepSeekHarnessAdapterError("DeepSeek user Preset target already exists")
        marker_identity = {
            "schema_version": request.schema_version,
            "deployment_id": request.deployment_id,
            "agent_id": request.agent_id,
            "host": "deepseek-harness",
        }
        if request.mode == "update":
            validate_managed_target(target_root, mode="update", expected_marker=marker_identity)
        source = _agent_source(request, facts)
        agent_text = patch_standard_agent_template(
            source,
            persona=persona,
            provider_name=f"agent-workflow-hub-{request.agent_id}",
            skill_root=target_root / "skills",
        )
        agent_text = _patch_mcp_servers(agent_text, _mcp_servers(request, deployed=True))
        preset_text = _render_preset(request)
        marker = _marker_bytes(request)

        def intent(target: Path, content: bytes, relative: str, kind: str) -> WriteIntent:
            exists = target.is_file()
            expected = _sha256(target.read_bytes()) if exists else None
            return WriteIntent(
                target=str(target),
                action="update" if exists else "create",
                content_sha256=_sha256(content),
                size=len(content),
                description=f"DeepSeek user Preset {kind}",
                expected_before_sha256=expected,
                parameters={
                    "kind": "file",
                    "payload_kind": kind,
                    "staging_relative": f"host-files/{relative}",
                },
            )

        intents = [
            intent(target_root / "agent.cordis.yml", agent_text.encode(), "agent.cordis.yml", "agent-template"),
            intent(target_root / "preset.yml", preset_text.encode(), "preset.yml", "preset"),
        ]
        for snapshot in snapshots:
            for record in snapshot.files:
                relative = f"skills/{snapshot.selection.name}/{record.relative_path}"
                target = target_root.joinpath(*relative.split("/"))
                exists = target.is_file()
                intents.append(
                    WriteIntent(
                        target=str(target),
                        action="update" if exists else "create",
                        content_sha256=record.sha256,
                        size=record.size,
                        description=f"fixed Skill snapshot {snapshot.selection.name}",
                        expected_before_sha256=_sha256(target.read_bytes()) if exists else None,
                        parameters={
                            "kind": "file",
                            "payload_kind": "skill",
                            "staging_relative": f"host-files/{relative}",
                        },
                    )
                )
        intents.append(intent(target_root / MANAGED_MARKER, marker, MANAGED_MARKER, "marker"))
        return tuple(intents)

    def _generated_content(self, intent: WriteIntent, context: ApplyContext) -> bytes:
        request = context.plan.request
        kind = intent.parameters.get("payload_kind")
        if kind == "agent-template":
            source = _agent_source(request, context.plan.host_facts)
            patched = patch_standard_agent_template(
                source,
                persona=context.plan.persona,
                provider_name=f"agent-workflow-hub-{request.agent_id}",
                skill_root=Path(context.plan.host_facts.target_root or "") / "skills",
            )
            return _patch_mcp_servers(patched, _mcp_servers(request, deployed=True)).encode("utf-8")
        if kind == "preset":
            return _render_preset(request).encode("utf-8")
        if kind == "marker":
            return _marker_bytes(request)
        relative = cast(str, intent.parameters["staging_relative"])
        return context.staging_root.joinpath(*relative.split("/")).read_bytes()

    def apply(self, context: ApplyContext) -> HostApplyResult:
        if context.plan.plan_sha256 != context.manifest.plan_sha256:
            raise DeepSeekHarnessAdapterError("apply plan does not match manifest")
        writes: list[ManagedWrite] = []
        for intent in context.plan.writes:
            if intent.parameters.get("kind") == "runtime-prepare":
                continue
            content = self._generated_content(intent, context)
            if len(content) != intent.size or _sha256(content) != intent.content_sha256:
                raise DeepSeekHarnessAdapterError(f"staged DSH payload drift: {intent.target}")
            writes.append(
                ManagedWrite(
                    target=Path(intent.target),
                    content=content,
                    expected_before_sha256=intent.expected_before_sha256,
                )
            )
        try:
            result = apply_managed_transaction(tuple(writes), backup_root=context.backup_root)
        except TransactionApplyError as exc:
            raise DeepSeekHarnessAdapterError(str(exc)) from exc
        return HostApplyResult(
            status="applied",
            managed_paths=(*result.created, *result.updated),
            details={"preset_id": context.plan.request.agent_id},
        )

    def verify(self, context: VerifyContext) -> VerificationResult:
        manifest = context.manifest
        target_root = Path(manifest.host_facts.target_root or "")
        static_ok = True
        try:
            marker = read_json_object(target_root / MANAGED_MARKER)
            static_ok = all(
                marker.get(field) == expected
                for field, expected in {
                    "deployment_id": manifest.deployment_id,
                    "agent_id": manifest.agent_id,
                    "host": "deepseek-harness",
                }.items()
            )
            for name, expected in manifest.skill_tree_sha256s.items():
                source = (target_root / "skills" / name).resolve()
                selection = SkillSelection(
                    name=name,
                    source_kind="external-skill",
                    source=str(source),
                    reason="verify DSH deployed snapshot",
                )
                static_ok = static_ok and snapshot_skill(target_root, selection).tree_sha256 == expected
            static_ok = static_ok and (target_root / "agent.cordis.yml").is_file() and (target_root / "preset.yml").is_file()
            servers = _mcp_servers(manifest.request, deployed=True)
            if servers:
                rows = _agent_rows(_stable_bytes(target_root / "agent.cordis.yml").decode("utf-8"))
                for server in servers:
                    expected = _agent_rows(_mcp_row(server))[0]
                    actual = [row for row in rows if row.get("id") == expected["id"]]
                    static_ok = static_ok and actual == [expected]
        except (ValueError, OSError, SourceSnapshotError, DeepSeekHarnessAdapterError):
            static_ok = False
        if not static_ok:
            return VerificationResult(
                schema_version=manifest.schema_version,
                deployment_id=manifest.deployment_id,
                status="failed",
                static={"status": "failed"},
                discovery={"status": "failed"},
                behavior={"status": "not-run"},
                details=(),
            )
        evidence: WebBehaviorEvidence | None = None
        if context.behavior_evidence_path is not None:
            evidence = WebBehaviorEvidence.from_mapping(
                read_json_object(context.behavior_evidence_path)
            )
        behavior = validate_web_behavior_evidence(evidence, manifest)
        discovery: dict[str, object] = {"status": "passed", "preset_id": manifest.agent_id}
        if servers:
            discovery["mcp"] = {"status": "configured_not_probed",
                                "servers": [server["server_name"] for server in servers]}
        return VerificationResult(
            schema_version=manifest.schema_version,
            deployment_id=manifest.deployment_id,
            status="partially_verified" if servers and behavior.status == "verified" else behavior.status,
            static={"status": "passed"},
            discovery=discovery,
            behavior=behavior.behavior,
            details=(),
        )

    def finalize(self, context: FinalizeContext) -> VerificationResult:
        """DSH has no post-verification transaction to finalize."""

        return context.verification


def validate_web_behavior_evidence(
    evidence: WebBehaviorEvidence | None,
    manifest: DeploymentManifest,
) -> VerificationResult:
    request = manifest.request
    if evidence is None:
        return VerificationResult(
            schema_version=manifest.schema_version,
            deployment_id=manifest.deployment_id,
            status="partially_verified",
            static={"status": "passed"},
            discovery={"status": "passed"},
            behavior={"status": "not-run", "reason": "Web behavior evidence is required"},
            details=(),
        )
    prompt = (
        "不要调用任何工具。仅返回 JSON："
        f'{{"identity":"{request.agent_id}",'
        f'"primary_workflow":"{request.primary_workflow}",'
        '"first_action":"完整加载主工作流"}'
    )
    valid = (
        evidence.preset_id == request.agent_id
        and evidence.prompt_sha256 == _sha256(prompt.encode("utf-8"))
        and evidence.identity == request.agent_id
        and evidence.primary_workflow == request.primary_workflow
        and evidence.first_action.strip() == "完整加载主工作流"
    )
    return VerificationResult(
        schema_version=manifest.schema_version,
        deployment_id=manifest.deployment_id,
        status="verified" if valid else "failed",
        static={"status": "passed"},
        discovery={"status": "passed"},
        behavior={
            "status": "passed" if valid else "failed",
            "session_id": evidence.session_id,
            "preset_id": evidence.preset_id,
            "prompt_sha256": evidence.prompt_sha256,
            "response_sha256": evidence.response_sha256,
        },
        details=(),
    )


__all__ = [
    "SUPPORTED_DSH",
    "DeepSeekHarnessAdapter",
    "DeepSeekHarnessAdapterError",
    "DeepSeekHarnessEvidence",
    "WebBehaviorEvidence",
    "patch_standard_agent_template",
    "validate_web_behavior_evidence",
]
