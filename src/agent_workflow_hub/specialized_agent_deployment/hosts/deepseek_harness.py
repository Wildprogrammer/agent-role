"""DeepSeek Harness Web user-Preset adapter."""

from __future__ import annotations

import hashlib
import json
import os
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
from .deepseek_harness_v2 import (
    DSH_V2_STANDARD_PATCH_SHA256_BY_VERSION,
    DeepSeekHarnessV2Error,
    extract_v2_preset,
    merge_v2_preset,
    render_v2_preset_patch,
    v2_preset_conflicts,
)


@dataclass(frozen=True, kw_only=True)
class DeepSeekHarnessEvidence:
    version: str
    commit: str
    agent_template_sha256: str
    preset_template_sha256: str


@dataclass(frozen=True, kw_only=True)
class DeepSeekHarnessProfileEvidence:
    persona_field: str
    agent_template_sha256: str
    preset_template_sha256: str


SUPPORTED_DSH = DeepSeekHarnessEvidence(
    version="0.1.2-alpha.2",
    commit="0a53fb55bea101816fa226bb964ae2bed71c343b",
    agent_template_sha256="f04fbc6ec6d38aab78f18690c293ddcb76293107f7e6cd157904b7c0e83094bd",
    preset_template_sha256="3c61b4ce68e5dd5cb2c099693fdcb30b91d5f22bbbef546e233321b0fa68f0e4",
)
SUPPORTED_DSH_PROFILES: Mapping[str, DeepSeekHarnessProfileEvidence] = {
    "0.1.2-alpha.2": DeepSeekHarnessProfileEvidence(
        persona_field="text",
        agent_template_sha256="f04fbc6ec6d38aab78f18690c293ddcb76293107f7e6cd157904b7c0e83094bd",
        preset_template_sha256="3c61b4ce68e5dd5cb2c099693fdcb30b91d5f22bbbef546e233321b0fa68f0e4",
    ),
    "0.1.5-rc.2": DeepSeekHarnessProfileEvidence(
        persona_field="prefix",
        agent_template_sha256="08a029c64fdeaabe415a5627ef73ce6f6c6bc61dadaf9989d11f9e15608907f6",
        preset_template_sha256="3c61b4ce68e5dd5cb2c099693fdcb30b91d5f22bbbef546e233321b0fa68f0e4",
    ),
}
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
_PROFILE_PACKAGE_NAMES = (
    "dsh-agent-presets",
    "dsh-persona",
    "dsh-base",
    "dsh-web-app",
)
_PROFILE_PACKAGE_ARTIFACTS = {
    "dsh-agent-presets": "lib/index.js",
    "dsh-persona": "lib/index.js",
    "dsh-base": "lib/index.js",
    "dsh-web-app": "lib/index.js",
    "dsh-mcp-client": "lib/index.js",
}
_PERSONA_REQUIRED_FIELD_RE = re.compile(
    r"\b(text|prefix)\s*:\s*z\.string\(\)\.required\(\)"
)


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
    persona_config_field: str = "prefix",
) -> str:
    """Replace only the standard template's persona and Skill filesystem rows."""

    if type(source) is not str or type(persona) is not str or not persona.strip():
        raise DeepSeekHarnessAdapterError("template and persona must be nonblank text")
    if type(provider_name) is not str or not re.fullmatch(r"[a-z0-9-]+", provider_name):
        raise DeepSeekHarnessAdapterError("provider_name must be kebab-case")
    if not isinstance(skill_root, Path) or not skill_root.is_absolute():
        raise DeepSeekHarnessAdapterError("skill_root must be absolute")
    if persona_config_field not in {"text", "prefix"}:
        raise DeepSeekHarnessAdapterError("unsupported DSH persona config field")
    persona_lines = "\n".join(f"      {line}" for line in persona.rstrip("\n").splitlines())
    persona_row = (
        "- id: persona\n"
        "  name: '@deepseek-ai/dsh-persona'\n"
        "  config:\n"
        f"    {persona_config_field}: |-\n"
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


def _profile_evidence_from_mapping(
    value: Mapping[str, object],
) -> DeepSeekHarnessProfileEvidence:
    try:
        evidence = DeepSeekHarnessProfileEvidence(
            persona_field=cast(str, value["persona_field"]),
            agent_template_sha256=cast(str, value["agent_template_sha256"]),
            preset_template_sha256=cast(str, value["preset_template_sha256"]),
        )
    except (KeyError, TypeError):
        raise DeepSeekHarnessAdapterError("invalid DSH profile evidence") from None
    if evidence.persona_field not in {"text", "prefix"}:
        raise DeepSeekHarnessAdapterError("invalid DSH profile persona field")
    if not _SHA256_RE.fullmatch(evidence.agent_template_sha256):
        raise DeepSeekHarnessAdapterError("invalid DSH profile agent template digest")
    if not _SHA256_RE.fullmatch(evidence.preset_template_sha256):
        raise DeepSeekHarnessAdapterError("invalid DSH profile preset template digest")
    return evidence


def _persona_schema_field(path: Path) -> str | None:
    try:
        source = _stable_bytes(path).decode("utf-8")
    except UnicodeDecodeError:
        return None
    fields = set(_PERSONA_REQUIRED_FIELD_RE.findall(source))
    return next(iter(fields)) if len(fields) == 1 else None


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


def _marker_bytes(request: DeploymentRequest, persona: str) -> bytes:
    return json.dumps(
        {
            "schema_version": request.schema_version,
            "deployment_id": request.deployment_id,
            "agent_id": request.agent_id,
            "host": "deepseek-harness",
            "persona_sha256": _sha256(persona.encode("utf-8")),
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
        profile_evidence: Mapping[
            str, DeepSeekHarnessProfileEvidence | Mapping[str, object]
        ] = SUPPORTED_DSH_PROFILES,
    ) -> None:
        self._runner = runner
        self._evidence = evidence
        self._profile_evidence = {
            version: (
                value
                if isinstance(value, DeepSeekHarnessProfileEvidence)
                else _profile_evidence_from_mapping(value)
            )
            for version, value in profile_evidence.items()
        }

    @staticmethod
    def _options(request: DeploymentRequest) -> tuple[Path, Path | None, bool]:
        options = request.host_options
        required = {"dsh_home", "profile"}
        allowed = required | {"runtime_root", "mcp_servers", "schedule"}
        if not required <= set(options) or set(options) - allowed or options.get("profile") != "web":
            raise DeepSeekHarnessAdapterError(
                "DeepSeek host_options require dsh_home and profile=web; optional runtime_root, mcp_servers, schedule"
            )
        dsh_home = Path(cast(str, options["dsh_home"]))
        runtime_value = options.get("runtime_root")
        runtime_root = Path(cast(str, runtime_value)) if runtime_value is not None else None
        if not dsh_home.is_absolute() or (
            runtime_root is not None and not runtime_root.is_absolute()
        ):
            raise DeepSeekHarnessAdapterError("DeepSeek host roots must be absolute")
        schedule = options.get("schedule", {"enabled": False})
        if (
            not isinstance(schedule, Mapping)
            or set(schedule) != {"enabled"}
            or type(schedule.get("enabled")) is not bool
        ):
            raise DeepSeekHarnessAdapterError(
                "DeepSeek schedule must be the closed object {enabled: boolean}"
            )
        return dsh_home, runtime_root, cast(bool, schedule["enabled"])

    def _discover_v2(
        self,
        request: DeploymentRequest,
        *,
        dsh_home: Path,
        version: str,
        schedule_enabled: bool,
        servers: tuple[dict[str, object], ...],
    ) -> HostFacts:
        profile_modules = dsh_home / "profiles/node_modules/@deepseek-ai"
        profile_path = dsh_home / "profiles/web/package.json"
        profile_patch = dsh_home / "profiles/web/cordis.patch.yml"
        standard_patch = profile_modules / "dsh-web-app/presets/standard.patch.yml"
        dsh_package = profile_modules / "dsh/package.json"
        try:
            resolved_package_root = dsh_package.resolve(strict=True).parent.parent
        except OSError:
            resolved_package_root = None
        packages = [
            "dsh",
            "dsh-base",
            "dsh-web-app",
            "dsh-persona",
            "dsh-agent-instructions",
            "dsh-skill-filesystem",
            "dsh-tool-skill",
            *(
                ("dsh-agent-preset", "dsh-agent-preset-registry", "dsh-tool-subagent-control")
                if version == "0.2.2-rc.1"
                else ()
            ),
            *(("dsh-time-context", "dsh-tool-schedule") if schedule_enabled else ()),
            *(("dsh-mcp-client",) if servers else ()),
        ]
        web_modules = dsh_home / "profiles/web/node_modules/@deepseek-ai"
        package_paths = {}
        for name in packages:
            candidates = (
                profile_modules / name / "package.json",
                web_modules / name / "package.json",
                *(
                    (resolved_package_root / name / "package.json",)
                    if resolved_package_root is not None
                    else ()
                ),
            )
            package_paths[name] = next(
                (path for path in candidates if path.is_file()), candidates[0]
            )
        required = (profile_path, profile_patch, standard_patch, *package_paths.values())
        missing = [str(path) for path in required if not path.is_file()]
        target_root = dsh_home / ".agent-presets" / request.agent_id
        if missing:
            return HostFacts(
                host="deepseek-harness",
                compatibility="missing",
                version=None,
                target_root=str(target_root),
                facts={
                    "generation": "profile-v2",
                    "missing_evidence": missing,
                    "guidance": ["请先完成 DeepSeek Harness 0.2 Web Profile 安装。"],
                },
            )
        versions = {
            name: cast(str | None, read_json_object(path).get("version"))
            for name, path in package_paths.items()
        }
        package_versions_ok = all(value == version for value in versions.values())
        standard_sha = _sha256(_stable_bytes(standard_patch))
        expected_standard_sha = DSH_V2_STANDARD_PATCH_SHA256_BY_VERSION.get(version)
        profile = read_json_object(profile_path)
        dsh = profile.get("dsh")
        profile_config = dsh.get("profile") if isinstance(dsh, Mapping) else None
        bundles = profile_config.get("bundles", []) if isinstance(profile_config, Mapping) else []
        patch_reload = (
            profile_config.get("patchReload")
            if isinstance(profile_config, Mapping)
            else None
        )
        web_profile_ok = (
            isinstance(bundles, list)
            and all(isinstance(bundle, str) for bundle in bundles)
            and {
                "@deepseek-ai/dsh-base",
                "@deepseek-ai/dsh-web-app",
            }
            <= set(cast(list[str], bundles))
            and patch_reload in (None, "live")
        )
        subagent_control_root_available = True
        if version == "0.2.2-rc.1":
            exports = read_json_object(package_paths["dsh-tool-subagent-control"]).get("exports")
            # In 0.2.2 list_agents is registered by the package root. The old
            # /list-agents subpath is intentionally absent and must not be a gate.
            subagent_control_root_available = isinstance(exports, Mapping) and "." in exports
        missing_mcp_paths = [
            str(server[field])
            for server in servers
            for field in ("command", "cwd")
            if not (
                Path(cast(str, server[field])).is_file()
                if field == "command"
                else Path(cast(str, server[field])).is_dir()
            )
        ]
        exact = (
            package_versions_ok
            and expected_standard_sha is not None
            and standard_sha == expected_standard_sha
            and web_profile_ok
            and subagent_control_root_available
            and not missing_mcp_paths
        )
        facts: dict[str, object] = {
            "generation": "profile-v2",
            "dsh_home": str(dsh_home),
            "profile_version": version,
            "profile_package_versions": versions,
            "profile_versions_consistent": package_versions_ok,
            "standard_patch": str(standard_patch),
            "standard_patch_sha256": standard_sha,
            "profile_patch": str(profile_patch),
            "profile_patch_sha256": _sha256(_stable_bytes(profile_patch)),
            "web_profile_package": str(profile_path),
            "web_profile_ok": web_profile_ok,
            "subagent_control_root_available": subagent_control_root_available,
            "patch_reload": patch_reload,
            "schedule_enabled": schedule_enabled,
            "mcp_servers": list(servers),
            "missing_mcp_paths": missing_mcp_paths,
        }
        if not exact:
            guidance = []
            if not subagent_control_root_available:
                guidance.append(
                    "DSH 0.2.2 的 dsh-tool-subagent-control 根入口不可用。"
                )
            if expected_standard_sha is None or standard_sha != expected_standard_sha:
                guidance.append(
                    "DSH 0.2 Profile 的 standard.patch.yml 未匹配任何已验证版本摘要。"
                )
            if not package_versions_ok or not web_profile_ok:
                guidance.append(
                    "DSH 0.2 Profile 包版本或 Web bundle 配置不一致。"
                )
            facts["guidance"] = guidance or [
                "DSH 0.2 Profile 包版本、standard.patch.yml 摘要或 Web 配置与已验证基线不一致。"
            ]
        return HostFacts(
            host="deepseek-harness",
            compatibility="verified" if exact else "unverified",
            version=version,
            target_root=str(target_root),
            facts=facts,
        )

    def discover(self, request: DeploymentRequest) -> HostFacts:
        if request.host != "deepseek-harness":
            raise DeepSeekHarnessAdapterError("request is not for DeepSeek Harness")
        dsh_home, runtime_root, schedule_enabled = self._options(request)
        servers = _mcp_servers(request)
        dsh_package = dsh_home / "profiles/node_modules/@deepseek-ai/dsh/package.json"
        if dsh_package.is_file():
            version = read_json_object(dsh_package).get("version")
            if type(version) is str and cast(str, version).startswith("0.2."):
                return self._discover_v2(
                    request,
                    dsh_home=dsh_home,
                    version=version,
                    schedule_enabled=schedule_enabled,
                    servers=servers,
                )
        if runtime_root is None:
            return HostFacts(
                host="deepseek-harness",
                compatibility="missing",
                version=None,
                target_root=str(dsh_home / ".agent-presets" / request.agent_id),
                facts={
                    "missing_evidence": ["host_options.runtime_root"],
                    "guidance": ["DSH 0.1.x 兼容路径仍需要 runtime_root。"],
                },
            )
        profile_modules = dsh_home / "profiles/node_modules/@deepseek-ai"
        profile_package_names = (
            *_PROFILE_PACKAGE_NAMES,
            *(("dsh-mcp-client",) if servers else ()),
        )
        profile_packages = {
            name: profile_modules / name / "package.json"
            for name in profile_package_names
        }
        installed_presets = profile_modules / "dsh-agent-presets/presets/standard"
        installed_agent_path = installed_presets / "agent.cordis.yml"
        installed_preset_path = installed_presets / "preset.yml"
        persona_artifact = profile_modules / "dsh-persona/lib/index.js"
        required_evidence = (
            runtime_root / "package.json",
            runtime_root / _AGENT_TEMPLATE_RELATIVE,
            runtime_root / _PRESET_TEMPLATE_RELATIVE,
            dsh_home / "profiles/web/package.json",
            *profile_packages.values(),
            installed_agent_path,
            installed_preset_path,
            persona_artifact,
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
        source_version = cast(str | None, package.get("version"))
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
        source_agent_path = runtime_root / _AGENT_TEMPLATE_RELATIVE
        source_preset_path = runtime_root / _PRESET_TEMPLATE_RELATIVE
        source_agent_sha = _sha256(_stable_bytes(source_agent_path))
        source_preset_sha = _sha256(_stable_bytes(source_preset_path))
        profile_package_versions = {
            name: cast(str | None, read_json_object(path).get("version"))
            for name, path in profile_packages.items()
        }
        profile_versions_consistent = (
            None not in profile_package_versions.values()
            and len(set(profile_package_versions.values())) == 1
        )
        profile_version = (
            profile_package_versions["dsh-agent-presets"]
            if profile_versions_consistent
            else None
        )
        expected_profile = (
            self._profile_evidence.get(cast(str, profile_version))
            if profile_version is not None
            else None
        )
        agent_path = installed_agent_path
        preset_path = installed_preset_path
        agent_sha = _sha256(_stable_bytes(agent_path))
        preset_sha = _sha256(_stable_bytes(preset_path))
        persona_schema_field = _persona_schema_field(persona_artifact)
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
        source_exact = (
            source_version == self._evidence.version
            and commit == self._evidence.commit
            and _COMMIT_RE.fullmatch(commit) is not None
            and origin in _OFFICIAL_ORIGINS
            and not dirty
            and source_agent_sha == self._evidence.agent_template_sha256
            and source_preset_sha == self._evidence.preset_template_sha256
        )
        profile_exact = (
            profile_versions_consistent
            and expected_profile is not None
            and agent_sha == expected_profile.agent_template_sha256
            and preset_sha == expected_profile.preset_template_sha256
            and persona_schema_field == expected_profile.persona_field
        )
        exact = (
            source_exact
            and profile_exact
            and web_profile_ok
        )
        artifacts = (*_BUILD_ARTIFACTS, *((_MCP_ARTIFACT,) if servers else ()))
        missing = [relative for relative in artifacts if not (runtime_root / relative).is_file()]
        missing_profile_artifacts = [
            str(profile_modules / name / relative)
            for name, relative in _PROFILE_PACKAGE_ARTIFACTS.items()
            if (name in profile_package_names)
            and not (profile_modules / name / relative).is_file()
        ]
        missing_mcp_paths = [str(server[field]) for server in servers for field in ("command", "cwd")
                             if not (Path(cast(str, server[field])).is_file() if field == "command"
                                     else Path(cast(str, server[field])).is_dir())]
        compatibility = (
            "verified" if exact and not missing and not missing_profile_artifacts and not missing_mcp_paths else
            "compatible_not_runnable" if exact else
            "unverified"
        )
        facts: dict[str, object] = {
            "runtime_root": str(runtime_root),
            "dsh_home": str(dsh_home),
            "source_version": source_version,
            "origin": origin,
            "commit": commit,
            "dirty": bool(dirty),
            "agent_template": str(agent_path),
            "agent_template_sha256": agent_sha,
            "preset_template": str(preset_path),
            "preset_template_sha256": preset_sha,
            "source_agent_template": str(source_agent_path),
            "source_agent_template_sha256": source_agent_sha,
            "source_preset_template": str(source_preset_path),
            "source_preset_template_sha256": source_preset_sha,
            "profile_package_versions": profile_package_versions,
            "profile_versions_consistent": profile_versions_consistent,
            "profile_version": profile_version,
            "persona_config_field": (
                expected_profile.persona_field if expected_profile is not None else None
            ),
            "persona_schema_field": persona_schema_field,
            "web_profile_package": str(profile_path),
            "web_profile_ok": web_profile_ok,
            "missing_build_artifacts": missing,
            "missing_profile_artifacts": missing_profile_artifacts,
            "mcp_servers": list(servers),
            "missing_mcp_paths": missing_mcp_paths,
        }
        if compatibility != "verified":
            facts["guidance"] = (
                ["请按 DeepSeek Harness 官方文档在仓库外单独准备构建产物，然后重新预览。"]
                if compatibility == "compatible_not_runnable"
                else ["源码、Profile 包版本、模板摘要或 Persona Schema 与已验证基线不一致。"]
            )
        return HostFacts(
            host="deepseek-harness",
            compatibility=compatibility,
            version=cast(str | None, profile_version),
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

    def _v2_preset_patch(
        self,
        request: DeploymentRequest,
        persona: str,
        facts: HostFacts,
    ) -> str:
        try:
            return render_v2_preset_patch(
                _stable_bytes(Path(cast(str, facts.facts["standard_patch"]))).decode("utf-8"),
                agent_id=request.agent_id,
                display_name=request.display_name,
                purpose=request.purpose,
                persona=persona,
                provider_name=f"agent-workflow-hub-{request.agent_id}",
                skill_root=Path(facts.target_root or "") / "skills",
                schedule_enabled=facts.facts.get("schedule_enabled") is True,
                mcp_servers=_mcp_servers(request, deployed=True),
            )
        except (UnicodeDecodeError, DeepSeekHarnessV2Error) as exc:
            raise DeepSeekHarnessAdapterError(str(exc)) from exc

    def _plan_v2_writes(
        self,
        request: DeploymentRequest,
        snapshots: tuple[SkillSnapshot, ...],
        persona: str,
        facts: HostFacts,
    ) -> tuple[WriteIntent, ...]:
        target_root = Path(facts.target_root or "")
        preset = self._v2_preset_patch(request, persona, facts).encode("utf-8")
        profile_patch = Path(cast(str, facts.facts["profile_patch"]))
        try:
            original = _stable_bytes(profile_patch)
            current = original.decode("utf-8")
            if request.agent_id in {"standard", "minimal", "ptc", "cordis"}:
                raise DeepSeekHarnessV2Error("DSH Web Profile already registers this preset ID")
            if request.mode == "create" and v2_preset_conflicts(current, agent_id=request.agent_id):
                raise DeepSeekHarnessV2Error("DSH Web Profile already registers this preset ID")
            if request.mode == "update":
                registered = extract_v2_preset(current, agent_id=request.agent_id)
                if registered is not None:
                    previous = extract_v2_preset(
                        _stable_bytes(target_root / "preset.patch.yml").decode("utf-8"),
                        agent_id=request.agent_id,
                    )
                    if previous != registered:
                        raise DeepSeekHarnessV2Error("DSH Web Preset differs from its managed copy")
            self._check_v2_bundle_conflicts(request, facts)
            merged = merge_v2_preset(
                current,
                preset.decode("utf-8"),
                agent_id=request.agent_id,
            ).encode("utf-8")
        except (UnicodeDecodeError, DeepSeekHarnessV2Error) as exc:
            raise DeepSeekHarnessAdapterError(str(exc)) from exc

        def intent(target: Path, content: bytes, relative: str, kind: str) -> WriteIntent:
            exists = target.is_file()
            return WriteIntent(
                target=str(target),
                action="update" if exists else "create",
                content_sha256=_sha256(content),
                size=len(content),
                description=f"DeepSeek 0.2 {kind}",
                expected_before_sha256=_sha256(target.read_bytes()) if exists else None,
                parameters={
                    "kind": "file",
                    "payload_kind": kind,
                    "staging_relative": f"host-files/{relative}",
                },
            )

        intents = [intent(target_root / "preset.patch.yml", preset, "preset.patch.yml", "v2-preset")]
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
        intents.append(intent(target_root / MANAGED_MARKER, _marker_bytes(request, persona), MANAGED_MARKER, "marker"))
        intents.append(
            WriteIntent(
                target=str(profile_patch),
                action="update",
                content_sha256=_sha256(merged),
                size=len(merged),
                description="register one managed DSH 0.2 agent preset",
                expected_before_sha256=_sha256(original),
                parameters={
                    "kind": "file",
                    "payload_kind": "v2-profile-patch",
                    "staging_relative": "host-files/profile-cordis.patch.yml",
                },
            )
        )
        return tuple(intents)

    def _check_v2_bundle_conflicts(self, request: DeploymentRequest, facts: HostFacts) -> None:
        home = Path(cast(str, facts.facts["dsh_home"]))
        profile_manifest = Path(cast(str, facts.facts["web_profile_package"]))
        standard = Path(cast(str, facts.facts["standard_patch"]))
        install_modules = (home / "profiles/node_modules/@deepseek-ai/dsh").resolve().parent.parent
        module_roots = (install_modules, profile_manifest.parent / "node_modules", standard.parents[2].parent)
        profile = read_json_object(profile_manifest)
        dsh = cast(Mapping, profile["dsh"])
        bundles = cast(Mapping, dsh["profile"])["bundles"]
        paths = [home / "cordis.patch.yml"]
        for bundle in bundles:
            if not re.fullmatch(r"(?:@[a-zA-Z0-9_-][\w.-]*/)?[a-zA-Z0-9_-][\w.-]*", bundle):
                raise DeepSeekHarnessV2Error("invalid DSH bundle package name")
            for root in module_roots:
                package = root / bundle / "package.json"
                if package.is_file():
                    break
            else:
                raise DeepSeekHarnessV2Error(f"cannot inspect DSH Web bundle {bundle}")
            metadata = read_json_object(package).get("dsh")
            bundle_info = metadata.get("bundle") if isinstance(metadata, Mapping) else None
            patches = bundle_info.get("patch", "./cordis.patch.yml") if isinstance(bundle_info, Mapping) else ()
            files = (patches,) if isinstance(patches, str) else patches
            package_root = package.parent.resolve()
            for name in files:
                path = (package_root / name).resolve()
                if not path.is_relative_to(package_root) or not path.is_file():
                    raise DeepSeekHarnessV2Error(f"cannot inspect DSH Web bundle patch for {bundle}")
                paths.append(path)
        for path in paths:
            if path.is_file() and v2_preset_conflicts(
                _stable_bytes(path).decode("utf-8"), agent_id=request.agent_id
            ):
                raise DeepSeekHarnessV2Error("DSH Web bundle or home patch already registers this preset ID")

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
        if facts.facts.get("generation") == "profile-v2":
            return self._plan_v2_writes(request, snapshots, persona, facts)
        source = _agent_source(request, facts)
        agent_text = patch_standard_agent_template(
            source,
            persona=persona,
            provider_name=f"agent-workflow-hub-{request.agent_id}",
            skill_root=target_root / "skills",
            persona_config_field=cast(str, facts.facts["persona_config_field"]),
        )
        agent_text = _patch_mcp_servers(agent_text, _mcp_servers(request, deployed=True))
        preset_text = _render_preset(request)
        marker = _marker_bytes(request, persona)

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
        if kind == "v2-preset":
            return self._v2_preset_patch(
                request,
                context.plan.persona,
                context.plan.host_facts,
            ).encode("utf-8")
        if kind == "v2-profile-patch":
            profile_patch = Path(intent.target)
            try:
                return merge_v2_preset(
                    _stable_bytes(profile_patch).decode("utf-8"),
                    self._v2_preset_patch(
                        request,
                        context.plan.persona,
                        context.plan.host_facts,
                    ),
                    agent_id=request.agent_id,
                ).encode("utf-8")
            except (UnicodeDecodeError, DeepSeekHarnessV2Error) as exc:
                raise DeepSeekHarnessAdapterError(str(exc)) from exc
        if kind == "agent-template":
            source = _agent_source(request, context.plan.host_facts)
            patched = patch_standard_agent_template(
                source,
                persona=context.plan.persona,
                provider_name=f"agent-workflow-hub-{request.agent_id}",
                skill_root=Path(context.plan.host_facts.target_root or "") / "skills",
                persona_config_field=cast(
                    str, context.plan.host_facts.facts["persona_config_field"]
                ),
            )
            return _patch_mcp_servers(patched, _mcp_servers(request, deployed=True)).encode("utf-8")
        if kind == "preset":
            return _render_preset(request).encode("utf-8")
        if kind == "marker":
            return _marker_bytes(request, context.plan.persona)
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
            if context.plan.host_facts.facts.get("generation") == "profile-v2":
                patch = Path(cast(str, context.plan.host_facts.facts["profile_patch"]))
                lock = patch.with_name(".awh-preset-apply.lock")
                try:
                    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    raise DeepSeekHarnessAdapterError("another DSH Preset apply holds the Web Profile lock") from None
                try:
                    result = apply_managed_transaction(tuple(writes), backup_root=context.backup_root)
                finally:
                    os.close(descriptor)
                    lock.unlink()
            else:
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
        generation = manifest.host_facts.facts.get("generation")
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
            servers = _mcp_servers(manifest.request, deployed=True)
            if generation == "profile-v2":
                preset_path = target_root / "preset.patch.yml"
                profile_patch = Path(cast(str, manifest.host_facts.facts["profile_patch"]))
                expected = extract_v2_preset(
                    _stable_bytes(preset_path).decode("utf-8"),
                    agent_id=manifest.agent_id,
                )
                actual = extract_v2_preset(
                    _stable_bytes(profile_patch).decode("utf-8"),
                    agent_id=manifest.agent_id,
                )
                static_ok = static_ok and expected is not None and actual == expected
            else:
                static_ok = static_ok and (target_root / "agent.cordis.yml").is_file() and (target_root / "preset.yml").is_file()
            if servers and generation != "profile-v2":
                rows = _agent_rows(_stable_bytes(target_root / "agent.cordis.yml").decode("utf-8"))
                for server in servers:
                    expected = _agent_rows(_mcp_row(server))[0]
                    actual = [row for row in rows if row.get("id") == expected["id"]]
                    static_ok = static_ok and actual == [expected]
        except (
            ValueError,
            OSError,
            UnicodeDecodeError,
            SourceSnapshotError,
            DeepSeekHarnessAdapterError,
            DeepSeekHarnessV2Error,
        ):
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
    "SUPPORTED_DSH_PROFILES",
    "DeepSeekHarnessAdapter",
    "DeepSeekHarnessAdapterError",
    "DeepSeekHarnessEvidence",
    "DeepSeekHarnessProfileEvidence",
    "WebBehaviorEvidence",
    "patch_standard_agent_template",
    "validate_web_behavior_evidence",
]
