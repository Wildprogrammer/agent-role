"""Optional Hermes full-enablement planning with redacted public facts."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast

import yaml

from ..contracts import DeploymentRequest, canonical_sha256
from ..runner import CommandExecutionError, CommandResult

_PLATFORM_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ACTIVE_PROFILE_RE = re.compile(r"^[ \t]*[◆*]\s*([^\s]+)", re.MULTILINE)
_MANAGED_MODEL_FIELDS = ("default", "provider", "base_url", "api_key", "api_mode")
_ROUTE_FIELDS = frozenset(
    ("name", "platform", "profile", "guild_id", "chat_id", "thread_id", "enabled")
)
_ENV_PLATFORM_PREFIXES = {
    "TELEGRAM_": "telegram",
    "DISCORD_": "discord",
    "WHATSAPP_": "whatsapp",
    "SLACK_": "slack",
    "SIGNAL_": "signal",
    "MATTERMOST_": "mattermost",
    "MATRIX_": "matrix",
    "HOMEASSISTANT_": "homeassistant",
    "EMAIL_": "email",
    "SMS_": "sms",
    "DINGTALK_": "dingtalk",
    "FEISHU_": "feishu",
    "WECOM_": "wecom",
    "WEIXIN_": "weixin",
    "BLUEBUBBLES_": "bluebubbles",
    "QQBOT_": "qqbot",
    "YUANBAO_": "yuanbao",
}


class HermesEnablementError(RuntimeError):
    """Raised when full enablement cannot be planned without ambiguity."""


@dataclass(frozen=True, kw_only=True)
class HermesEnablementRequest:
    source_profile: str
    platforms: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class HermesEnablementProjection:
    deployment_id: str
    source_profile: str
    target_profile: str
    platforms: tuple[str, ...]
    model_fields: tuple[str, ...]
    disabled_platforms: tuple[str, ...]
    route_names: tuple[str, ...]
    source_config_path: Path
    target_config_path: Path
    source_env_path: Path
    target_env_path: Path
    source_config_sha256: str
    target_before_sha256: str | None
    source_env_sha256: str
    target_env_before_sha256: str | None
    target_config_bytes: bytes = field(repr=False)
    gateway_config_bytes: bytes = field(repr=False)
    env_bytes: bytes = field(repr=False)

    @property
    def gateway_config_path(self) -> Path:
        return self.source_config_path

    def redacted_facts(self) -> dict[str, object]:
        return {
            "source_profile": self.source_profile,
            "target_profile": self.target_profile,
            "platforms": list(self.platforms),
            "disabled_platforms": list(self.disabled_platforms),
            "route_names": list(self.route_names),
            "model_fields": list(self.model_fields),
            "source_config": {
                "path": str(self.source_config_path),
                "sha256": self.source_config_sha256,
            },
            "target_config": {
                "path": str(self.target_config_path),
                "before_sha256": self.target_before_sha256,
                "planned_sha256": _sha256(self.target_config_bytes),
                "planned_size": len(self.target_config_bytes),
            },
            "source_env": {
                "path": str(self.source_env_path),
                "sha256": self.source_env_sha256,
                "size": len(self.env_bytes),
            },
            "target_env": {
                "path": str(self.target_env_path),
                "before_sha256": self.target_env_before_sha256,
            },
            "gateway_config": {
                "path": str(self.gateway_config_path),
                "planned_sha256": _sha256(self.gateway_config_bytes),
                "planned_size": len(self.gateway_config_bytes),
            },
            "operation_sha256": canonical_sha256(
                {
                    "deployment_id": self.deployment_id,
                    "source_config_sha256": self.source_config_sha256,
                    "source_env_sha256": self.source_env_sha256,
                    "target_before_sha256": self.target_before_sha256,
                    "target_env_before_sha256": self.target_env_before_sha256,
                    "platforms": list(self.platforms),
                    "disabled_platforms": list(self.disabled_platforms),
                    "route_names": list(self.route_names),
                }
            ),
        }


@dataclass(frozen=True, kw_only=True)
class HermesEnablementApplyResult:
    status: Literal["applied", "rolled_back", "outcome_unknown"]
    details: Mapping[str, object]


class _Runner(Protocol):
    def run(self, argv: tuple[str, ...], *, phase: str) -> CommandResult: ...


@dataclass(kw_only=True)
class HermesEnablementTransaction:
    projection: HermesEnablementProjection
    plan_sha256: str
    runner: _Runner
    state: Literal[
        "planned", "applying", "applied", "verified", "rolled_back", "outcome_unknown"
    ] = "planned"

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", self.plan_sha256) is None:
            raise HermesEnablementError("enablement transaction requires a plan SHA-256")
        target_root = self.projection.target_config_path.parent.resolve()
        backup_root = self.backup_root.resolve()
        if target_root not in backup_root.parents:
            raise HermesEnablementError("enablement backup root escaped the target Profile")

    @property
    def backup_root(self) -> Path:
        return (
            self.projection.target_config_path.parent
            / ".agent-workflow-hub-transaction"
            / self.plan_sha256
        )

    @property
    def marker_path(self) -> Path:
        return self.backup_root / "transaction.json"

    def _write_marker(self, state: str) -> None:
        payload = json.dumps(
            {
                "schema_version": "1.0",
                "plan_sha256": self.plan_sha256,
                "state": state,
                "source_profile": self.projection.source_profile,
                "target_profile": self.projection.target_profile,
                "paths": [
                    str(self.projection.target_config_path),
                    str(self.projection.target_env_path),
                    str(self.projection.gateway_config_path),
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        _atomic_replace(self.marker_path, payload)

    def _backups(self) -> tuple[tuple[Path, Path], ...]:
        return (
            (self.projection.target_config_path, self.backup_root / "tc"),
            (self.projection.target_env_path, self.backup_root / "te"),
            (self.projection.gateway_config_path, self.backup_root / "gc"),
        )

    def _prepare_backups(self) -> None:
        if self.backup_root.exists():
            raise HermesEnablementError("enablement transaction backup already exists")
        self.backup_root.mkdir(parents=True)
        try:
            os.chmod(self.backup_root, 0o700)
        except OSError:
            pass
        records: list[dict[str, object]] = []
        for target, backup in self._backups():
            existed = target.is_file()
            if existed:
                content = target.read_bytes()
                _atomic_replace(backup, content)
                records.append(
                    {
                        "target": str(target),
                        "backup": backup.name,
                        "existed": True,
                        "sha256": _sha256(content),
                        "size": len(content),
                    }
                )
            else:
                records.append(
                    {
                        "target": str(target),
                        "backup": None,
                        "existed": False,
                        "sha256": None,
                        "size": 0,
                    }
                )
        _atomic_replace(
            self.backup_root / "files.json",
            json.dumps(
                records,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    def _preflight(self) -> None:
        if _optional_sha256(self.projection.source_config_path) != self.projection.source_config_sha256:
            raise HermesEnablementError("source Profile config changed after preview")
        if _optional_sha256(self.projection.source_env_path) != self.projection.source_env_sha256:
            raise HermesEnablementError("source Profile .env changed after preview")
        if _optional_sha256(self.projection.target_config_path) != self.projection.target_before_sha256:
            raise HermesEnablementError("target Profile config changed after planning")
        if _optional_sha256(self.projection.target_env_path) != self.projection.target_env_before_sha256:
            raise HermesEnablementError("target Profile .env changed after preview")

    def apply(self) -> HermesEnablementApplyResult:
        if self.state != "planned":
            raise HermesEnablementError("enablement transaction cannot be replayed")
        self._preflight()
        self._prepare_backups()
        self.state = "applying"
        self._write_marker(self.state)
        try:
            _atomic_replace(
                self.projection.target_config_path,
                self.projection.target_config_bytes,
            )
            _atomic_replace(self.projection.target_env_path, self.projection.env_bytes)
            _atomic_replace(
                self.projection.gateway_config_path,
                self.projection.gateway_config_bytes,
            )
            result = self.runner.run(
                (
                    "hermes",
                    "--profile",
                    self.projection.source_profile,
                    "gateway",
                    "restart",
                ),
                phase="apply",
            )
        except CommandExecutionError as exc:
            self.state = "outcome_unknown"
            self._write_marker(self.state)
            return HermesEnablementApplyResult(
                status="outcome_unknown",
                details={"reason": str(exc), "backup_root": str(self.backup_root)},
            )
        except Exception as exc:
            return self._rollback_result(exc)
        if result.exit_code != 0:
            return self._rollback_result(
                HermesEnablementError("active Profile Gateway restart failed")
            )
        self.state = "applied"
        self._write_marker(self.state)
        return HermesEnablementApplyResult(
            status="applied",
            details={"backup_root": str(self.backup_root)},
        )

    def _rollback_result(self, cause: Exception) -> HermesEnablementApplyResult:
        try:
            self.rollback()
        except Exception as rollback_exc:
            self.state = "outcome_unknown"
            self._write_marker(self.state)
            return HermesEnablementApplyResult(
                status="outcome_unknown",
                details={
                    "reason": str(cause),
                    "rollback_reason": str(rollback_exc),
                    "backup_root": str(self.backup_root),
                },
            )
        return HermesEnablementApplyResult(
            status="rolled_back",
            details={"reason": str(cause)},
        )

    def rollback(self) -> None:
        for target, backup in self._backups():
            if backup.is_file():
                _atomic_replace(target, backup.read_bytes())
            elif target.exists():
                target.unlink()
        result = self.runner.run(
            (
                "hermes",
                "--profile",
                self.projection.source_profile,
                "gateway",
                "restart",
            ),
            phase="apply",
        )
        if result.exit_code != 0:
            raise HermesEnablementError("Gateway restart failed during rollback")
        self.state = "rolled_back"
        self._write_marker(self.state)

    def commit(self) -> None:
        if self.state not in {"applied", "verified"}:
            raise HermesEnablementError("only a verified enablement transaction can commit")
        self.state = "verified"
        self._write_marker(self.state)
        resolved = self.backup_root.resolve()
        target_root = self.projection.target_config_path.parent.resolve()
        if target_root not in resolved.parents:
            raise HermesEnablementError("refusing to clear backup outside target Profile")
        shutil.rmtree(resolved)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_replace(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=".t",
        suffix="",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _optional_sha256(path: Path) -> str | None:
    return _sha256(path.read_bytes()) if path.is_file() else None


def _read_yaml(path: Path, *, required: bool) -> dict[str, object]:
    if not path.is_file():
        if required:
            raise HermesEnablementError(f"required Hermes config is missing: {path}")
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        raise HermesEnablementError(f"invalid or unreadable Hermes config at {path}") from None
    if value is None:
        return {}
    if type(value) is not dict or not all(type(key) is str for key in value):
        raise HermesEnablementError(f"Hermes config must be a string-keyed object: {path}")
    return cast(dict[str, object], value)


def _yaml_bytes(value: Mapping[str, object]) -> bytes:
    return yaml.safe_dump(
        dict(value),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).encode("utf-8")


def parse_enablement_request(
    request: DeploymentRequest,
) -> HermesEnablementRequest | None:
    """Parse the closed Hermes enablement object; absence preserves legacy behavior."""

    raw = request.host_options.get("enablement")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise HermesEnablementError("host_options.enablement must be an object")
    mode = raw.get("mode")
    if mode == "none":
        if set(raw) != {"mode"}:
            raise HermesEnablementError("mode none cannot include enablement actions")
        return None
    fields = {
        "mode",
        "source_profile",
        "model_strategy",
        "env_strategy",
        "platforms",
        "gateway_strategy",
        "external_resources",
        "behavior_check",
    }
    if set(raw) != fields:
        raise HermesEnablementError("full enablement fields are incomplete or unknown")
    expected = {
        "mode": "full",
        "source_profile": "active",
        "model_strategy": "managed-fields",
        "env_strategy": "full",
        "gateway_strategy": "multiplex-routes",
        "external_resources": "check_only",
        "behavior_check": "readiness_only",
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise HermesEnablementError("unsupported Hermes enablement strategy")
    platforms_raw = raw.get("platforms")
    if type(platforms_raw) not in (list, tuple):
        raise HermesEnablementError("enablement platforms must be an array")
    platforms = tuple(cast(list[object] | tuple[object, ...], platforms_raw))
    if not platforms or not all(
        type(item) is str and _PLATFORM_RE.fullmatch(cast(str, item)) is not None
        for item in platforms
    ):
        raise HermesEnablementError("enablement platforms must be lowercase Hermes identifiers")
    normalized = tuple(sorted(cast(tuple[str, ...], platforms), key=lambda item: item.encode("utf-8")))
    if len(normalized) != len(set(normalized)):
        raise HermesEnablementError("enablement platforms must be unique")
    return HermesEnablementRequest(source_profile="active", platforms=normalized)


def _active_profile(profile_list_output: str) -> str:
    plain = _ANSI_RE.sub("", profile_list_output)
    matches = _ACTIVE_PROFILE_RE.findall(plain)
    if len(matches) != 1:
        raise HermesEnablementError("exactly one active Hermes Profile is required")
    profile = matches[0].strip()
    if _PLATFORM_RE.fullmatch(profile) is None:
        raise HermesEnablementError("active Hermes Profile name is invalid")
    return profile


def _profile_root(profiles_root: Path, profile: str) -> Path:
    return profiles_root.parent if profile == "default" else profiles_root / profile


def _mapping_at(document: dict[str, object], key: str) -> dict[str, object]:
    value = document.get(key)
    if value is None:
        result: dict[str, object] = {}
        document[key] = result
        return result
    if type(value) is not dict or not all(type(item) is str for item in value):
        raise HermesEnablementError(f"Hermes config key {key} must be an object")
    return cast(dict[str, object], value)


def _env_platforms(env_bytes: bytes) -> set[str]:
    try:
        text = env_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise HermesEnablementError("source Profile .env must be UTF-8") from None
    platforms: set[str] = set()
    for line in text.splitlines():
        name = line.split("=", 1)[0].strip()
        if not name or name.startswith("#"):
            continue
        for prefix, platform in _ENV_PLATFORM_PREFIXES.items():
            if name.startswith(prefix):
                platforms.add(platform)
    return platforms


def _merge_routes(
    source: dict[str, object],
    *,
    deployment_id: str,
    target_profile: str,
    platforms: tuple[str, ...],
) -> tuple[str, ...]:
    if "profile_routes" in source:
        raise HermesEnablementError(
            "top-level profile_routes ownership is ambiguous; migrate it before enablement"
        )
    gateway = _mapping_at(source, "gateway")
    raw_routes = gateway.get("profile_routes", [])
    if type(raw_routes) is not list:
        raise HermesEnablementError("gateway.profile_routes must be an array")
    routes = cast(list[object], raw_routes)
    selected = set(platforms)
    managed_by_platform: dict[str, dict[str, object]] = {}
    for raw in routes:
        if type(raw) is not dict or not all(type(key) is str for key in raw):
            raise HermesEnablementError("unknown route shape blocks enablement")
        route = cast(dict[str, object], raw)
        if set(route) - _ROUTE_FIELDS:
            raise HermesEnablementError("unknown route fields block enablement")
        name = route.get("name")
        platform = route.get("platform")
        profile = route.get("profile")
        if not all(type(item) is str and item for item in (name, platform, profile)):
            raise HermesEnablementError("unknown route shape blocks enablement")
        platform_text = cast(str, platform)
        if platform_text not in selected or route.get("enabled", True) is False:
            continue
        expected_name = f"agent-workflow-hub-{deployment_id}-{platform_text}"
        selectors = any(route.get(key) not in (None, "") for key in ("guild_id", "chat_id", "thread_id"))
        if name == expected_name:
            if profile != target_profile or selectors:
                raise HermesEnablementError(f"route conflict for platform {platform_text}")
            managed_by_platform[platform_text] = route
            continue
        if profile != target_profile or not selectors:
            raise HermesEnablementError(f"route conflict for platform {platform_text}")

    route_names: list[str] = []
    for platform in platforms:
        name = f"agent-workflow-hub-{deployment_id}-{platform}"
        route_names.append(name)
        if platform not in managed_by_platform:
            routes.append(
                {
                    "name": name,
                    "platform": platform,
                    "profile": target_profile,
                    "enabled": True,
                }
            )
    gateway["multiplex_profiles"] = True
    gateway["profile_routes"] = routes
    return tuple(route_names)


def build_enablement_projection(
    request: DeploymentRequest,
    *,
    profiles_root: Path,
    profile_list_output: str,
) -> HermesEnablementProjection:
    """Build secret-bearing local payloads plus a redacted plan representation."""

    options = parse_enablement_request(request)
    if options is None:
        raise HermesEnablementError("full enablement was not requested")
    source_profile = _active_profile(profile_list_output)
    if source_profile == request.agent_id:
        raise HermesEnablementError("active source Profile cannot be the target Profile")
    profiles_root = profiles_root.resolve()
    source_root = _profile_root(profiles_root, source_profile).resolve()
    target_root = (profiles_root / request.agent_id).resolve()
    source_config_path = source_root / "config.yaml"
    target_config_path = target_root / "config.yaml"
    source_env_path = source_root / ".env"
    target_env_path = target_root / ".env"
    source_config_bytes = source_config_path.read_bytes() if source_config_path.is_file() else b""
    if not source_config_bytes:
        raise HermesEnablementError(f"required Hermes config is missing: {source_config_path}")
    if not source_env_path.is_file():
        raise HermesEnablementError(f"required source Profile .env is missing: {source_env_path}")
    env_bytes = source_env_path.read_bytes()
    source = _read_yaml(source_config_path, required=True)
    target = _read_yaml(target_config_path, required=False)
    target_projection = copy.deepcopy(target)
    source_model = source.get("model")
    if type(source_model) is not dict:
        raise HermesEnablementError("source Profile model config is missing")
    target_model = _mapping_at(target_projection, "model")
    model_fields = tuple(
        key for key in _MANAGED_MODEL_FIELDS if key in cast(dict[str, object], source_model)
    )
    for key in model_fields:
        if key in cast(dict[str, object], source_model):
            target_model[key] = copy.deepcopy(cast(dict[str, object], source_model)[key])
    target_agent = _mapping_at(target_projection, "agent")
    target_agent["system_prompt_file"] = "AGENTS.md"
    target_terminal = _mapping_at(target_projection, "terminal")
    target_terminal["cwd"] = request.workdir

    source_platforms = source.get("platforms", {})
    if source_platforms is not None and type(source_platforms) is not dict:
        raise HermesEnablementError("source Profile platforms config must be an object")
    disabled = set(options.platforms) | _env_platforms(env_bytes)
    if type(source_platforms) is dict:
        disabled.update(
            key for key in cast(dict[object, object], source_platforms) if type(key) is str
        )
    target_platforms = _mapping_at(target_projection, "platforms")
    for platform in sorted(disabled, key=lambda item: item.encode("utf-8")):
        value = target_platforms.get(platform)
        if value is None:
            value = {}
            target_platforms[platform] = value
        if type(value) is not dict:
            raise HermesEnablementError(f"target platform config must be an object: {platform}")
        cast(dict[str, object], value)["enabled"] = False

    gateway_projection = copy.deepcopy(source)
    route_names = _merge_routes(
        gateway_projection,
        deployment_id=request.deployment_id,
        target_profile=request.agent_id,
        platforms=options.platforms,
    )
    return HermesEnablementProjection(
        deployment_id=request.deployment_id,
        source_profile=source_profile,
        target_profile=request.agent_id,
        platforms=options.platforms,
        model_fields=model_fields,
        disabled_platforms=tuple(sorted(disabled, key=lambda item: item.encode("utf-8"))),
        route_names=route_names,
        source_config_path=source_config_path,
        target_config_path=target_config_path,
        source_env_path=source_env_path,
        target_env_path=target_env_path,
        source_config_sha256=_sha256(source_config_bytes),
        target_before_sha256=_optional_sha256(target_config_path),
        source_env_sha256=_sha256(env_bytes),
        target_env_before_sha256=_optional_sha256(target_env_path),
        target_config_bytes=_yaml_bytes(target_projection),
        gateway_config_bytes=_yaml_bytes(gateway_projection),
        env_bytes=env_bytes,
    )


__all__ = [
    "HermesEnablementError",
    "HermesEnablementApplyResult",
    "HermesEnablementProjection",
    "HermesEnablementRequest",
    "HermesEnablementTransaction",
    "build_enablement_projection",
    "parse_enablement_request",
]
