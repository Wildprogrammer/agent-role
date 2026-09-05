"""Hermes 0.19.0 Profile adapter."""

from __future__ import annotations

import hashlib
import json
import os
import re
import yaml
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

from ..contracts import (
    DeploymentRequest,
    EnablementCheck,
    EnablementResult,
    HostFacts,
    SkillSelection,
    SkillSnapshot,
    VerificationResult,
    WriteIntent,
    canonical_sha256,
    read_json_object,
)
from ..filesystem import (
    MANAGED_MARKER,
    ManagedWrite,
    TransactionApplyError,
    TransactionOutcomeUnknown,
    apply_managed_transaction,
    validate_managed_target,
)
from ..runner import CommandExecutionError, CommandResult, CommandRunner
from ..mcp_bindings import mcp_servers
from ..sources import SourceSnapshotError, snapshot_skill
from .base import (
    ApplyContext,
    FinalizeContext,
    HostApplyResult,
    VerifyContext,
    guidance_host_facts,
)
from .hermes_enablement import (
    HermesEnablementError,
    HermesEnablementProjection,
    HermesEnablementTransaction,
    build_enablement_projection,
    parse_enablement_request,
)

VERIFIED_HERMES_VERSION = "0.20.6"
BEHAVIOR_PROMPT_TEMPLATE = (
    "不要调用任何工具。仅返回 JSON："
    '{{"identity":"{agent_id}","primary_workflow":"{primary_workflow}",'
    '"first_action":"完整加载主工作流"}}'
)
_VERSION_RE = re.compile(r"Hermes Agent v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)")
_FEATURE_PROBES = (
    ("hermes", "profile", "create", "--help"),
    ("hermes", "profile", "delete", "--help"),
    ("hermes", "config", "--help"),
    ("hermes", "skills", "list", "--help"),
    ("hermes", "gateway", "restart", "--help"),
)


class HermesAdapterError(RuntimeError):
    """Raised when Hermes cannot safely complete a bounded operation."""


def _mcp_values(request: DeploymentRequest) -> dict[str, object]:
    return {f"mcp_servers.{server['server_name']}.{key}": server[key]
            for server in mcp_servers(request, deployed=True, error_type=HermesAdapterError)
            for key in ("command", "args", "cwd")}


def _read_mcp_values(path: Path, keys: dict) -> dict:
    if not path.exists():
        return {key: None for key in keys}
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    values = {}
    for key in keys:
        value = config
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        values[key] = value
    return values


def _default_profiles_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return (Path(local_app_data) / "hermes" / "profiles").resolve()
    return (Path.home() / ".hermes" / "profiles").resolve()


def _profile_list_contains(output: str, profile: str) -> bool:
    return re.search(rf"(?<![a-z0-9-]){re.escape(profile)}(?![a-z0-9-])", output) is not None


def _raw_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _argv_size(argv: tuple[str, ...]) -> int:
    return len(
        json.dumps(
            list(argv),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _command_intent(
    *,
    target: Path,
    argv: tuple[str, ...],
    description: str,
    kind: str,
    extra: dict[str, object] | None = None,
) -> WriteIntent:
    parameters: dict[str, object] = {"kind": kind, "argv": list(argv)}
    if extra:
        parameters.update(extra)
    return WriteIntent(
        target=str(target),
        action="command" if kind not in {"config-set"} else "config-set",
        content_sha256=canonical_sha256(list(argv)),
        size=_argv_size(argv),
        description=description,
        parameters=parameters,
    )


class HermesAdapter:
    kind = "hermes"

    def __init__(
        self,
        runner: CommandRunner,
        *,
        profiles_root: Path | None = None,
        staging_root: Path | None = None,
    ) -> None:
        self._runner = runner
        self._profiles_root = (
            profiles_root.resolve() if profiles_root is not None else _default_profiles_root()
        )
        self._staging_root = staging_root.resolve() if staging_root is not None else None
        if not self._profiles_root.is_absolute():
            raise HermesAdapterError("Hermes profiles root must be absolute")
        if self._staging_root is not None and not self._staging_root.is_absolute():
            raise HermesAdapterError("deployment staging root must be absolute")
        self._enablement_projection: HermesEnablementProjection | None = None
        self._enablement_transaction: HermesEnablementTransaction | None = None
        self._profile_list_output: str | None = None

    def discover(self, request: DeploymentRequest) -> HostFacts:
        if type(request) is not DeploymentRequest or request.host != "hermes":
            raise HermesAdapterError("Hermes discovery requires a Hermes request")
        try:
            version_result = self._runner.run(
                ("hermes", "--version"), phase="preview"
            )
        except CommandExecutionError:
            return guidance_host_facts(
                host="hermes",
                compatibility="missing",
                version=None,
                target_root=None,
                guidance=(
                    "请先按 Hermes 官方方式准备既有宿主，再重新运行预览。",
                ),
            )
        match = _VERSION_RE.search(version_result.stdout)
        if version_result.exit_code != 0 or match is None:
            return guidance_host_facts(
                host="hermes",
                compatibility="unverified",
                version=None,
                target_root=self._profiles_root / request.agent_id,
                guidance=("无法确认 Hermes 版本；请检查现有安装。",),
            )
        version = match.group("version")
        list_result = self._runner.run(
            ("hermes", "profile", "list"), phase="preview"
        )
        self._profile_list_output = list_result.stdout
        probe_results = [
            self._runner.run(argv, phase="preview") for argv in _FEATURE_PROBES
        ]
        features_available = (
            list_result.exit_code == 0
            and all(item.exit_code == 0 for item in probe_results)
        )
        compatibility = (
            "verified"
            if version == VERIFIED_HERMES_VERSION and features_available
            else "conditional"
            if features_available
            else "unverified"
        )
        profile_exists = _profile_list_contains(
            list_result.stdout, request.agent_id
        )
        target_root = self._profiles_root / request.agent_id
        config_path: str | None = None
        system_prompt: str | None = None
        terminal_cwd: str | None = None
        if profile_exists:
            path_result = self._runner.run(
                (
                    "hermes",
                    "--profile",
                    request.agent_id,
                    "config",
                    "path",
                ),
                phase="preview",
            )
            prompt_result = self._runner.run(
                (
                    "hermes",
                    "--profile",
                    request.agent_id,
                    "config",
                    "get",
                    "agent.system_prompt_file",
                ),
                phase="preview",
            )
            cwd_result = self._runner.run(
                (
                    "hermes",
                    "--profile",
                    request.agent_id,
                    "config",
                    "get",
                    "terminal.cwd",
                ),
                phase="preview",
            )
            if path_result.exit_code != 0:
                compatibility = "unverified"
            else:
                candidate = Path(path_result.stdout.strip())
                if not candidate.is_absolute():
                    compatibility = "unverified"
                else:
                    config_path = str(candidate)
                    target_root = candidate.parent
            if prompt_result.exit_code == 0:
                system_prompt = prompt_result.stdout.strip()
            if cwd_result.exit_code == 0:
                terminal_cwd = cwd_result.stdout.strip()
        facts: dict[str, object] = {
            "profile_exists": profile_exists,
            "required_commands_available": features_available,
            "config_path": config_path,
            "agent.system_prompt_file": system_prompt,
            "terminal.cwd": terminal_cwd,
        }
        bindings = _mcp_values(request)
        if bindings:
            facts["mcp_previous"] = _read_mcp_values(Path(config_path) if config_path else target_root / "config.yaml", bindings)
        enablement = parse_enablement_request(request)
        if enablement is not None:
            if version != VERIFIED_HERMES_VERSION or not features_available:
                return HostFacts(
                    host="hermes",
                    compatibility="compatible_not_runnable",
                    version=version,
                    target_root=str(target_root.resolve()),
                    facts={
                        **facts,
                        "guidance": [
                            "Hermes 完整启用需要已验证的 0.20.6 multiplex/profile-routes 能力；"
                            "当前仍可使用核心部署请求。"
                        ],
                    },
                )
            try:
                self._enablement_projection = build_enablement_projection(
                    request,
                    profiles_root=self._profiles_root,
                    profile_list_output=list_result.stdout,
                )
            except HermesEnablementError as exc:
                if "active source Profile cannot be the target Profile" in str(exc):
                    return HostFacts(
                        host="hermes",
                        compatibility="compatible_not_runnable",
                        version=version,
                        target_root=str(target_root.resolve()),
                        facts={**facts, "guidance": [str(exc)]},
                    )
                raise HermesAdapterError(f"Hermes enablement discovery failed: {exc}") from exc
            facts["enablement"] = self._enablement_projection.redacted_facts()
        if compatibility == "unverified":
            facts["guidance"] = [
                "Hermes 必需命令或配置路径探测未通过；请先修复现有宿主。"
            ]
        return HostFacts(
            host="hermes",
            compatibility=compatibility,
            version=version,
            target_root=str(target_root.resolve()),
            facts=facts,
        )

    def plan_writes(
        self,
        request: DeploymentRequest,
        snapshots: tuple[SkillSnapshot, ...],
        persona: str,
        facts: HostFacts,
    ) -> tuple[WriteIntent, ...]:
        if facts.host != "hermes" or facts.compatibility in {
            "missing",
            "unverified",
            "compatible_not_runnable",
        }:
            return ()
        if self._staging_root is None:
            raise HermesAdapterError("Hermes planning requires a staging root")
        if facts.target_root is None:
            raise HermesAdapterError("Hermes target root was not discovered")
        target_root = Path(facts.target_root)
        profile_exists = facts.facts.get("profile_exists") is True
        if request.mode == "create" and profile_exists:
            raise HermesAdapterError("Hermes create target already exists")
        marker_identity = {
            "schema_version": request.schema_version,
            "deployment_id": request.deployment_id,
            "agent_id": request.agent_id,
            "host": "hermes",
        }
        if request.mode == "update":
            if not profile_exists:
                raise HermesAdapterError("Hermes update target does not exist")
            validate_managed_target(
                target_root,
                mode="update",
                expected_marker=marker_identity,
            )

        intents: list[WriteIntent] = []
        if request.mode == "create":
            create_argv = (
                "hermes",
                "profile",
                "create",
                request.agent_id,
                "--no-alias",
                "--no-skills",
            )
            intents.append(
                _command_intent(
                    target=target_root,
                    argv=create_argv,
                    description="create isolated Hermes Profile",
                    kind="profile-create",
                )
            )

        def file_intent(
            target: Path,
            *,
            content_sha256: str,
            size: int,
            description: str,
            staging_relative: str,
        ) -> WriteIntent:
            exists = target.is_file()
            expected = hashlib.sha256(target.read_bytes()).hexdigest() if exists else None
            return WriteIntent(
                target=str(target),
                action="update" if exists else "create",
                content_sha256=content_sha256,
                size=size,
                description=description,
                expected_before_sha256=expected,
                parameters={
                    "kind": "file",
                    "staging_relative": staging_relative,
                },
            )

        persona_bytes = persona.encode("utf-8")
        intents.append(
            file_intent(
                target_root / "AGENTS.md",
                content_sha256=hashlib.sha256(persona_bytes).hexdigest(),
                size=len(persona_bytes),
                description="generated minimal Hermes persona",
                staging_relative="host-files/AGENTS.md",
            )
        )
        for snapshot in snapshots:
            for record in snapshot.files:
                relative = f"skills/{snapshot.selection.name}/{record.relative_path}"
                intents.append(
                    file_intent(
                        target_root.joinpath(*relative.split("/")),
                        content_sha256=record.sha256,
                        size=record.size,
                        description=f"fixed Skill snapshot {snapshot.selection.name}",
                        staging_relative=f"host-files/{relative}",
                    )
                )
        marker_bytes = json.dumps(
            marker_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        intents.append(
            file_intent(
                target_root / MANAGED_MARKER,
                content_sha256=hashlib.sha256(marker_bytes).hexdigest(),
                size=len(marker_bytes),
                description="Agent Workflow Hub ownership marker",
                staging_relative=f"host-files/{MANAGED_MARKER}",
            )
        )

        config_path_text = cast(
            str | None, facts.facts.get("config_path")
        ) or str(target_root / "config.yaml")
        config_path = Path(config_path_text)
        config_values = (
            (
                "agent.system_prompt_file",
                "AGENTS.md",
                cast(str | None, facts.facts.get("agent.system_prompt_file")),
            ),
            (
                "terminal.cwd",
                request.workdir,
                cast(str | None, facts.facts.get("terminal.cwd")),
            ),
        )
        for key, value in _mcp_values(request).items():
            previous = facts.facts.get("mcp_previous", {}).get(key)
            config_values += ((key, json.dumps(value, ensure_ascii=False) if isinstance(value, list) else str(value),
                               (json.dumps(previous, ensure_ascii=False) if isinstance(previous, (list, tuple))
                                else str(previous)) if previous is not None else None),)
        for key, value, previous in config_values:
            argv = (
                "hermes",
                "--profile",
                request.agent_id,
                "config",
                "set",
                key,
                value,
            )
            intent = _command_intent(
                target=config_path,
                argv=argv,
                description=f"set Hermes config key {key}",
                kind="config-set",
                extra={
                    "config_key": key,
                    "value": value,
                    "previous_value": previous,
                },
            )
            intents.append(intent)

        prompt = BEHAVIOR_PROMPT_TEMPLATE.format(
            agent_id=request.agent_id,
            primary_workflow=request.primary_workflow,
        )
        usage_path = self._staging_root / "hermes-usage.json"
        behavior_argv = (
            "hermes",
            "--profile",
            request.agent_id,
            "-z",
            prompt,
            "--usage-file",
            str(usage_path),
        )
        intents.append(
            _command_intent(
                target=usage_path,
                argv=behavior_argv,
                description="run one no-tool identity behavior smoke",
                kind="behavior-smoke",
            )
        )
        if parse_enablement_request(request) is not None:
            projection = self._enablement_projection
            if projection is None:
                raise HermesAdapterError("Hermes enablement projection was not discovered")
            redacted = projection.redacted_facts()
            intents.extend(
                (
                    WriteIntent(
                        target=str(projection.target_config_path),
                        action="update" if projection.target_config_path.is_file() else "create",
                        content_sha256=hashlib.sha256(projection.target_config_bytes).hexdigest(),
                        size=len(projection.target_config_bytes),
                        description="merge managed model fields and disable target platform adapters",
                        expected_before_sha256=projection.target_before_sha256,
                        parameters={
                            "kind": "enablement-target-config",
                            "model_fields": redacted["model_fields"],
                            "disabled_platforms": list(projection.disabled_platforms),
                            "operation_sha256": redacted["operation_sha256"],
                        },
                    ),
                    WriteIntent(
                        target=str(projection.target_env_path),
                        action="update" if projection.target_env_path.is_file() else "create",
                        content_sha256=projection.source_env_sha256,
                        size=len(projection.env_bytes),
                        description="copy the complete active Profile environment by digest",
                        expected_before_sha256=projection.target_env_before_sha256,
                        parameters={
                            "kind": "enablement-env-copy",
                            "source_path": str(projection.source_env_path),
                            "source_sha256": projection.source_env_sha256,
                            "source_size": len(projection.env_bytes),
                        },
                    ),
                    WriteIntent(
                        target=str(projection.gateway_config_path),
                        action="update",
                        content_sha256=hashlib.sha256(projection.gateway_config_bytes).hexdigest(),
                        size=len(projection.gateway_config_bytes),
                        description="enable multiplex routing on the active Profile Gateway",
                        expected_before_sha256=projection.source_config_sha256,
                        parameters={
                            "kind": "enablement-gateway-config",
                            "source_profile": projection.source_profile,
                            "route_names": list(projection.route_names),
                            "platforms": list(projection.platforms),
                        },
                    ),
                    _command_intent(
                        target=projection.gateway_config_path,
                        argv=(
                            "hermes",
                            "--profile",
                            projection.source_profile,
                            "gateway",
                            "restart",
                        ),
                        description="restart the existing active Profile Gateway",
                        kind="enablement-gateway-restart",
                        extra={
                            "source_profile": projection.source_profile,
                            "platforms": list(projection.platforms),
                        },
                    ),
                )
            )
        return tuple(intents)

    def _rollback_config(
        self,
        request: DeploymentRequest,
        applied: list[WriteIntent],
    ) -> None:
        for intent in reversed(applied):
            key = cast(str, intent.parameters["config_key"])
            previous = intent.parameters.get("previous_value")
            argv = (
                (
                    "hermes",
                    "--profile",
                    request.agent_id,
                    "config",
                    "unset",
                    key,
                )
                if previous is None
                else (
                    "hermes",
                    "--profile",
                    request.agent_id,
                    "config",
                    "set",
                    key,
                    cast(str, previous),
                )
            )
            try:
                result = self._runner.run(argv, phase="apply")
            except CommandExecutionError as exc:
                raise TransactionOutcomeUnknown(f"Hermes config rollback could not be observed: {key}") from exc
            if result.exit_code != 0:
                raise TransactionOutcomeUnknown(f"Hermes config rollback failed: {key}")

    def _delete_confirmed_create(self, agent_id: str) -> None:
        self._runner.run(
            ("hermes", "profile", "delete", "-y", agent_id),
            phase="apply",
        )

    def apply(self, context: ApplyContext) -> HostApplyResult:
        plan = context.plan
        request = plan.request
        if request.host != "hermes" or plan.plan_sha256 != context.manifest.plan_sha256:
            raise HermesAdapterError("Hermes apply context is not bound to its manifest")
        profile_confirmed = request.mode == "update"
        if request.mode == "create":
            create = next(
                item for item in plan.writes if item.parameters.get("kind") == "profile-create"
            )
            create_result = self._runner.run(
                tuple(cast(tuple[str, ...], create.parameters["argv"])),
                phase="apply",
            )
            list_result = self._runner.run(
                ("hermes", "profile", "list"), phase="apply"
            )
            appeared = list_result.exit_code == 0 and _profile_list_contains(
                list_result.stdout, request.agent_id
            )
            if create_result.exit_code != 0 or not appeared:
                return HostApplyResult(
                    status="outcome_unknown" if appeared or create_result.exit_code == 0 else "rolled_back",
                    managed_paths=(),
                    details={"profile_create_exit": create_result.exit_code, "profile_appeared": appeared},
                )
            profile_confirmed = True

        config_intents = [
            item for item in plan.writes if item.parameters.get("kind") == "config-set"
        ]
        applied_config: list[WriteIntent] = []
        try:
            for intent in config_intents:
                # A command can write and then fail; include the attempted key
                # in recovery, not just commands with a successful exit.
                applied_config.append(intent)
                result = self._runner.run(
                    tuple(cast(tuple[str, ...], intent.parameters["argv"])),
                    phase="apply",
                )
                if result.exit_code != 0:
                    raise HermesAdapterError(
                        f"Hermes config command failed: {intent.parameters['config_key']}"
                    )

            managed_writes: list[ManagedWrite] = []
            for intent in plan.writes:
                if intent.parameters.get("kind") != "file":
                    continue
                relative = cast(str, intent.parameters["staging_relative"])
                staged = context.staging_root.joinpath(*relative.split("/"))
                content = staged.read_bytes()
                if (
                    len(content) != intent.size
                    or hashlib.sha256(content).hexdigest() != intent.content_sha256
                ):
                    raise HermesAdapterError(f"staged payload drift: {relative}")
                managed_writes.append(
                    ManagedWrite(
                        target=Path(intent.target),
                        content=content,
                        expected_before_sha256=intent.expected_before_sha256,
                    )
                )
            transaction = apply_managed_transaction(
                tuple(managed_writes), backup_root=context.backup_root
            )
        except Exception as exc:
            self._rollback_config(request, applied_config)
            if request.mode == "create" and profile_confirmed and not isinstance(exc, TransactionOutcomeUnknown):
                self._delete_confirmed_create(request.agent_id)
            if isinstance(exc, TransactionApplyError):
                raise HermesAdapterError(str(exc)) from exc
            raise HermesAdapterError(f"Hermes apply failed: {exc}") from exc
        enablement_status = "not_requested"
        enablement_details: dict[str, object] = {}
        if parse_enablement_request(request) is not None:
            if self._profile_list_output is None:
                enablement_status = "rolled_back"
                enablement_details = {"reason": "active Profile discovery was unavailable"}
            else:
                try:
                    projection = build_enablement_projection(
                        request,
                        profiles_root=self._profiles_root,
                        profile_list_output=self._profile_list_output,
                    )
                    enablement_transaction = HermesEnablementTransaction(
                        projection=projection,
                        plan_sha256=plan.plan_sha256,
                        runner=self._runner,
                    )
                    enablement_outcome = enablement_transaction.apply()
                    self._enablement_projection = projection
                    self._enablement_transaction = enablement_transaction
                    enablement_status = enablement_outcome.status
                    enablement_details = dict(enablement_outcome.details)
                except HermesEnablementError as exc:
                    enablement_status = "rolled_back"
                    enablement_details = {"reason": str(exc)}
        return HostApplyResult(
            status="applied",
            managed_paths=(*transaction.created, *transaction.updated),
            details={
                "profile_confirmed": profile_confirmed,
                "enablement_status": enablement_status,
                "enablement": enablement_details,
            },
        )

    def verify(self, context: VerifyContext) -> VerificationResult:
        manifest = context.manifest
        request = manifest.request
        target_root = Path(manifest.host_facts.target_root or "")
        static_ok = target_root.is_absolute()
        marker_ok = False
        trees_ok = True
        if static_ok:
            try:
                marker = read_json_object(target_root / MANAGED_MARKER)
                marker_ok = all(
                    marker.get(field) == value
                    for field, value in {
                        "deployment_id": request.deployment_id,
                        "agent_id": request.agent_id,
                        "host": "hermes",
                    }.items()
                )
                for name, expected in manifest.skill_tree_sha256s.items():
                    source = (target_root / "skills" / name).resolve()
                    selection = SkillSelection(
                        name=name,
                        source_kind="external-skill",
                        source=str(source),
                        reason="verify deployed Hermes snapshot",
                    )
                    actual = snapshot_skill(target_root, selection)
                    trees_ok = trees_ok and actual.tree_sha256 == expected
            except (ValueError, OSError, SourceSnapshotError):
                static_ok = False
                trees_ok = False
        prompt_config_result = self._runner.run(
            (
                "hermes",
                "--profile",
                request.agent_id,
                "config",
                "get",
                "agent.system_prompt_file",
            ),
            phase="verify",
        )
        cwd_config_result = self._runner.run(
            (
                "hermes",
                "--profile",
                request.agent_id,
                "config",
                "get",
                "terminal.cwd",
            ),
            phase="verify",
        )
        config_ok = (
            prompt_config_result.exit_code == 0
            and prompt_config_result.stdout.strip() == "AGENTS.md"
            and cwd_config_result.exit_code == 0
            and cwd_config_result.stdout.strip() == request.workdir
        )
        bindings = _mcp_values(request)
        if bindings:
            try:
                config_ok = config_ok and _read_mcp_values(target_root / "config.yaml", bindings) == bindings
            except (ValueError, OSError, yaml.YAMLError):
                config_ok = False
        static_ok = static_ok and marker_ok and trees_ok and config_ok

        skills_result = self._runner.run(
            (
                "hermes",
                "--profile",
                request.agent_id,
                "skills",
                "list",
                "--source",
                "local",
                "--enabled-only",
            ),
            phase="verify",
        )
        discovery_ok = skills_result.exit_code == 0 and all(
            name in skills_result.stdout or (target_root / "skills" / name).is_dir()
            for name in manifest.skill_tree_sha256s
        )
        prompt = BEHAVIOR_PROMPT_TEMPLATE.format(
            agent_id=request.agent_id,
            primary_workflow=request.primary_workflow,
        )
        usage_path = context.staging_root / "hermes-usage.json"
        behavior_result = self._runner.run(
            (
                "hermes",
                "--profile",
                request.agent_id,
                "-z",
                prompt,
                "--usage-file",
                str(usage_path),
            ),
            phase="verify",
        )
        behavior_ok = False
        parsed: dict[str, object] = {}
        if behavior_result.exit_code == 0:
            try:
                value = json.loads(behavior_result.stdout)
                if isinstance(value, dict):
                    parsed = value
                    behavior_ok = (
                        value.get("identity") == request.agent_id
                        and value.get("primary_workflow") == request.primary_workflow
                        and bool(value.get("first_action"))
                    )
            except json.JSONDecodeError:
                behavior_ok = False
        core_ok = static_ok and discovery_ok and behavior_ok
        enablement_result = EnablementResult.not_requested()
        enablement_request = parse_enablement_request(request)
        if enablement_request is not None:
            transaction_state = (
                self._enablement_transaction.state
                if self._enablement_transaction is not None
                else None
            )
            if transaction_state == "rolled_back":
                enablement_result = EnablementResult(
                    requested=True,
                    platforms=enablement_request.platforms,
                    status="rolled_back",
                    checks=(),
                    details={"reason": "enablement apply failed and recovery completed"},
                )
            elif transaction_state == "outcome_unknown":
                enablement_result = EnablementResult(
                    requested=True,
                    platforms=enablement_request.platforms,
                    status="outcome_unknown",
                    checks=(),
                    details={"reason": "enablement apply outcome requires reconciliation"},
                )
            else:
                profile_list_result = self._runner.run(
                    ("hermes", "profile", "list"), phase="verify"
                )
                checks: list[EnablementCheck] = []
                try:
                    projection = build_enablement_projection(
                        request,
                        profiles_root=self._profiles_root,
                        profile_list_output=profile_list_result.stdout,
                    )
                    target_config_ok = (
                        projection.target_config_path.is_file()
                        and hashlib.sha256(
                            projection.target_config_path.read_bytes()
                        ).hexdigest()
                        == hashlib.sha256(projection.target_config_bytes).hexdigest()
                    )
                    checks.append(
                        EnablementCheck(
                            name="model_and_adapters",
                            status="passed" if target_config_ok else "failed",
                            details={
                                "disabled_platforms": list(projection.disabled_platforms),
                                "model_fields": projection.redacted_facts()["model_fields"],
                            },
                        )
                    )
                    environment_ok = (
                        projection.target_env_path.is_file()
                        and hashlib.sha256(
                            projection.target_env_path.read_bytes()
                        ).hexdigest()
                        == projection.source_env_sha256
                    )
                    checks.append(
                        EnablementCheck(
                            name="environment",
                            status="passed" if environment_ok else "failed",
                            details={
                                "sha256": projection.source_env_sha256,
                                "size": len(projection.env_bytes),
                            },
                        )
                    )
                    gateway_status = self._runner.run(
                        (
                            "hermes",
                            "--profile",
                            projection.source_profile,
                            "gateway",
                            "status",
                        ),
                        phase="verify",
                    )
                    gateway_output = (
                        gateway_status.stdout + "\n" + gateway_status.stderr
                    ).casefold()
                    gateway_config_ok = (
                        hashlib.sha256(
                            projection.gateway_config_path.read_bytes()
                        ).hexdigest()
                        == hashlib.sha256(projection.gateway_config_bytes).hexdigest()
                    )
                    gateway_ok = (
                        gateway_status.exit_code == 0
                        and gateway_config_ok
                        and "duplicate credential" not in gateway_output
                        and "duplicate-credential" not in gateway_output
                    )
                    checks.append(
                        EnablementCheck(
                            name="gateway",
                            status="passed" if gateway_ok else "failed",
                            details={
                                "source_profile": projection.source_profile,
                                "route_names": list(projection.route_names),
                            },
                        )
                    )
                    workdir_ok = Path(request.workdir).is_dir()
                    checks.append(
                        EnablementCheck(
                            name="workdir",
                            status="passed" if workdir_ok else "failed",
                            details={"path": request.workdir},
                        )
                    )
                    external_status = "passed" if not request.config_refs else "not_checked"
                    checks.append(
                        EnablementCheck(
                            name="external_resources",
                            status=cast(
                                Literal["passed", "failed", "not_checked"],
                                external_status,
                            ),
                            details={
                                "mode": "check_only",
                                "reason": (
                                    "no external resources declared"
                                    if not request.config_refs
                                    else "no owning read-only workflow entrypoint was executed"
                                ),
                            },
                        )
                    )
                    local_failed = any(
                        item.status == "failed"
                        for item in checks
                        if item.name != "external_resources"
                    )
                    external_ready = checks[-1].status == "passed"
                    if local_failed:
                        if self._enablement_transaction is not None:
                            self._enablement_transaction.rollback()
                            enablement_status = "rolled_back"
                        else:
                            enablement_status = "outcome_unknown"
                    else:
                        enablement_status = "verified" if external_ready else "partially_ready"
                    enablement_result = EnablementResult(
                        requested=True,
                        platforms=enablement_request.platforms,
                        status=cast(
                            Literal[
                                "verified",
                                "partially_ready",
                                "rolled_back",
                                "outcome_unknown",
                            ],
                            enablement_status,
                        ),
                        checks=tuple(checks),
                        details={
                            "source_profile": projection.source_profile,
                            "target_profile": projection.target_profile,
                        },
                    )
                except (HermesEnablementError, OSError) as exc:
                    enablement_result = EnablementResult(
                        requested=True,
                        platforms=enablement_request.platforms,
                        status="outcome_unknown",
                        checks=tuple(checks),
                        details={"reason": str(exc)},
                    )

        if not core_ok:
            status = "failed"
        elif enablement_result.status == "verified" or not enablement_result.requested:
            status = "verified"
        elif enablement_result.status == "outcome_unknown":
            status = "outcome_unknown"
        else:
            status = "partially_verified"
        if bindings and status == "verified":
            status = "partially_verified"
        return VerificationResult(
            schema_version=manifest.schema_version,
            deployment_id=manifest.deployment_id,
            status=status,
            static={
                "status": "passed" if static_ok else "failed",
                "marker": marker_ok,
                "skill_trees": trees_ok,
                "config": config_ok,
            },
            discovery={"status": "passed" if discovery_ok else "failed",
                       **({"mcp": {"status": "configured_not_probed"}} if bindings else {})},
            behavior={
                "status": "passed" if behavior_ok else "failed",
                "parsed": parsed,
            },
            details=(),
            enablement=enablement_result,
        )

    def finalize(self, context: FinalizeContext) -> VerificationResult:
        """Clear target-local backups only after enablement readiness is known."""

        transaction = self._enablement_transaction
        verification = context.verification
        if transaction is None:
            return verification
        if (
            verification.status in {"verified", "partially_verified"}
            and verification.enablement.status in {"verified", "partially_ready"}
            and transaction.state == "applied"
        ):
            try:
                transaction.commit()
            except (HermesEnablementError, OSError) as exc:
                unknown = EnablementResult(
                    requested=True,
                    platforms=verification.enablement.platforms,
                    status="outcome_unknown",
                    checks=verification.enablement.checks,
                    details={"reason": str(exc)},
                )
                return replace(
                    verification,
                    status="outcome_unknown",
                    enablement=unknown,
                )
        return verification


__all__ = [
    "BEHAVIOR_PROMPT_TEMPLATE",
    "HermesAdapter",
    "HermesAdapterError",
    "VERIFIED_HERMES_VERSION",
]
