"""Closed JSON contracts for the Xiaozhi multi-host agent bridge."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit


_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_ATTACHMENT_ID_RE = re.compile(r"^att_[a-f0-9]{32}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1"))
_HOSTS = frozenset(("hermes", "deepseek-harness"))
_ATTACHMENT_MEDIA_TYPES = frozenset(
    (
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
    )
)
_OPERATIONS = frozenset(
    (
        "list_agents",
        "list_profiles",
        "switch_profile",
        "dispatch",
        "list_sessions",
        "bind_session",
        "show_binding",
        "unbind_session",
        "dispatch_shared",
        "dispatch_bound_voice",
        "create_session",
        "compress_session",
    )
)


class BridgeContractError(ValueError):
    """Raised when bridge configuration or input is outside its contract."""


def _object(value: object, *, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BridgeContractError(f"{name} must be an object")
    if not all(type(key) is str for key in value):
        raise BridgeContractError(f"{name} keys must be strings")
    return cast(Mapping[str, object], value)


def _closed(
    value: Mapping[str, object],
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    name: str,
) -> None:
    keys = set(value)
    if missing := required - keys:
        raise BridgeContractError(f"{name} missing fields: {sorted(missing)!r}")
    if unknown := keys - required - optional:
        raise BridgeContractError(f"{name} unknown fields: {sorted(unknown)!r}")


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not cast(str, value).strip():
        raise BridgeContractError(f"{name} must be a non-blank string")
    try:
        cast(str, value).encode("utf-8")
    except UnicodeEncodeError:
        raise BridgeContractError(f"{name} must be valid UTF-8") from None
    return cast(str, value)


def _integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= cast(int, value) <= maximum:
        raise BridgeContractError(f"{name} must be an integer from {minimum} to {maximum}")
    return cast(int, value)


def _boolean(value: object, *, name: str) -> bool:
    if type(value) is not bool:
        raise BridgeContractError(f"{name} must be a boolean")
    return cast(bool, value)


def _profile_name(value: object, *, name: str) -> str:
    profile = _text(value, name=name)
    if _PROFILE_RE.fullmatch(profile) is None:
        raise BridgeContractError(f"{name} must be a lowercase Hermes profile name")
    return profile


def _host_name(value: object, *, name: str = "host") -> str:
    host = _text(value, name=name)
    if host not in _HOSTS:
        raise BridgeContractError(f"{name} is unsupported")
    return host


def _loopback_origin(value: object, *, name: str) -> str:
    url = _text(value, name=name)
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise BridgeContractError(f"{name} must be a valid URL") from None
    expected_netloc = f"[{host}]" if host is not None and ":" in host else host or ""
    if port is not None:
        expected_netloc += f":{port}"
    if (
        parsed.scheme.lower() != "http"
        or host not in _LOOPBACK_HOSTS
        or parsed.netloc != expected_netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BridgeContractError(f"{name} must be a numeric-loopback HTTP origin")
    return f"http://{expected_netloc}"


def _loopback_profile_url(value: object, *, profile: str) -> str:
    url = _text(value, name="api_server_base_url")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise BridgeContractError("api_server_base_url must be a valid URL") from None
    expected_netloc = f"[{host}]" if host is not None and ":" in host else host or ""
    if port is not None:
        expected_netloc += f":{port}"
    if (
        parsed.scheme.lower() != "http"
        or host is None
        or host not in _LOOPBACK_HOSTS
        or parsed.netloc != expected_netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != f"/p/{profile}"
    ):
        raise BridgeContractError(
            "api_server_base_url must be loopback HTTP with exact path "
            f"/p/{profile}"
        )
    return url


def _load_json_object(path: Path, *, name: str) -> Mapping[str, object]:
    if not isinstance(path, Path) or not path.is_absolute():
        raise BridgeContractError(f"{name} path must be absolute")

    def reject_constant(value: str) -> object:
        raise BridgeContractError(f"non-finite JSON constant: {value}")

    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
        )
    except BridgeContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeContractError(f"invalid {name} JSON: {exc}") from None
    return _object(parsed, name=name)


@dataclass(frozen=True, slots=True)
class BridgeAttachment:
    attachment_id: str
    path: Path
    name: str
    media_type: str
    kind: Literal["image", "pdf", "text"]
    bytes: int
    sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BridgeAttachment:
        _closed(
            value,
            required={
                "attachment_id",
                "path",
                "name",
                "media_type",
                "kind",
                "bytes",
                "sha256",
            },
            name="bridge attachment",
        )
        attachment_id = _text(value["attachment_id"], name="attachment_id")
        if _ATTACHMENT_ID_RE.fullmatch(attachment_id) is None:
            raise BridgeContractError("attachment_id is invalid")
        path = Path(_text(value["path"], name="attachment path"))
        if not path.is_absolute():
            raise BridgeContractError("attachment path must be absolute")
        name = _text(value["name"], name="attachment name")
        if Path(name.replace("\\", "/")).name != name or len(name) > 160:
            raise BridgeContractError("attachment name must be a bounded leaf name")
        media_type = _text(value["media_type"], name="attachment media_type")
        if media_type not in _ATTACHMENT_MEDIA_TYPES:
            raise BridgeContractError("attachment media_type is unsupported")
        kind = _text(value["kind"], name="attachment kind")
        if kind not in {"image", "pdf", "text"}:
            raise BridgeContractError("attachment kind is unsupported")
        expected_kind = (
            "image"
            if media_type.startswith("image/")
            else "pdf"
            if media_type == "application/pdf"
            else "text"
        )
        if kind != expected_kind:
            raise BridgeContractError("attachment kind does not match media_type")
        size = _integer(
            value["bytes"],
            name="attachment bytes",
            minimum=1,
            maximum=50 * 1024 * 1024,
        )
        sha256 = _text(value["sha256"], name="attachment sha256")
        if _SHA256_RE.fullmatch(sha256) is None:
            raise BridgeContractError("attachment sha256 is invalid")
        return cls(
            attachment_id=attachment_id,
            path=path.resolve(),
            name=name,
            media_type=media_type,
            kind=cast('Literal["image", "pdf", "text"]', kind),
            bytes=size,
            sha256=sha256,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "attachment_id": self.attachment_id,
            "path": str(self.path),
            "name": self.name,
            "media_type": self.media_type,
            "kind": self.kind,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class ProfileRoute:
    name: str
    api_server_base_url: str
    api_key_env: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ProfileRoute:
        _closed(
            value,
            required={"name", "api_server_base_url", "api_key_env"},
            name="profile route",
        )
        name = _profile_name(value["name"], name="profile route name")
        env_name = _text(value["api_key_env"], name="api_key_env")
        if _ENV_RE.fullmatch(env_name) is None:
            raise BridgeContractError("api_key_env must be an environment variable name")
        return cls(
            name=name,
            api_server_base_url=_loopback_profile_url(
                value["api_server_base_url"], profile=name
            ),
            api_key_env=env_name,
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "name": self.name,
            "api_server_base_url": self.api_server_base_url,
            "api_key_env": self.api_key_env,
        }


@dataclass(frozen=True, slots=True)
class DshWebConfig:
    base_url: str
    launch_token_env: str
    dsh_home: Path
    expected_version: str
    allowed_presets: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DshWebConfig:
        _closed(
            value,
            required={
                "base_url",
                "launch_token_env",
                "dsh_home",
                "expected_version",
                "allowed_presets",
            },
            name="deepseek_harness config",
        )
        token_env = _text(value["launch_token_env"], name="launch_token_env")
        if _ENV_RE.fullmatch(token_env) is None:
            raise BridgeContractError(
                "launch_token_env must be an environment variable name"
            )
        dsh_home = Path(_text(value["dsh_home"], name="dsh_home"))
        if not dsh_home.is_absolute():
            raise BridgeContractError("dsh_home must be absolute")
        version = _text(value["expected_version"], name="expected_version")
        if version != "0.2.0-rc.2":
            raise BridgeContractError("expected_version must be 0.2.0-rc.2")
        raw_presets = value["allowed_presets"]
        if type(raw_presets) is not list or not raw_presets:
            raise BridgeContractError("allowed_presets must be a non-empty array")
        presets = tuple(
            _profile_name(item, name="allowed preset")
            for item in cast(Sequence[object], raw_presets)
        )
        if len(presets) != len(set(presets)):
            raise BridgeContractError("allowed_presets must be unique")
        return cls(
            base_url=_loopback_origin(value["base_url"], name="base_url"),
            launch_token_env=token_env,
            dsh_home=dsh_home.resolve(),
            expected_version=version,
            allowed_presets=presets,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "base_url": self.base_url,
            "launch_token_env": self.launch_token_env,
            "dsh_home": str(self.dsh_home),
            "expected_version": self.expected_version,
            "allowed_presets": list(self.allowed_presets),
        }


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    schema_version: str
    trusted_caller_id: str
    default_profile: str
    profiles: tuple[ProfileRoute, ...]
    dispatch_enabled: bool
    require_confirmation: bool
    request_timeout_seconds: int
    poll_interval_seconds: int
    max_polls: int
    max_input_chars: int
    bound_voice_dispatch_mode: Literal["confirm", "direct"] = "confirm"
    deepseek_harness: DshWebConfig | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BridgeConfig:
        required = {
            "schema_version",
            "trusted_caller_id",
            "default_profile",
            "profiles",
            "dispatch_enabled",
            "require_confirmation",
            "request_timeout_seconds",
            "poll_interval_seconds",
            "max_polls",
            "max_input_chars",
        }
        _closed(
            value,
            required=required,
            optional={"deepseek_harness", "bound_voice_dispatch_mode"},
            name="bridge config",
        )
        if value["schema_version"] != "1.0":
            raise BridgeContractError("schema_version must be 1.0")
        trusted_caller_id = _text(value["trusted_caller_id"], name="trusted_caller_id")
        if _SESSION_RE.fullmatch(trusted_caller_id) is None:
            raise BridgeContractError("trusted_caller_id contains unsupported characters")
        raw_profiles = value["profiles"]
        if type(raw_profiles) is not list or not raw_profiles:
            raise BridgeContractError("profiles must be a non-empty array")
        profiles = tuple(
            ProfileRoute.from_mapping(_object(item, name="profile route"))
            for item in cast(Sequence[object], raw_profiles)
        )
        names = [item.name for item in profiles]
        if len(names) != len(set(names)):
            raise BridgeContractError("profile route names must be unique")
        default = _profile_name(value["default_profile"], name="default_profile")
        if default not in names:
            raise BridgeContractError("default_profile must name an allowed profile")
        raw_dsh = value.get("deepseek_harness")
        bound_voice_dispatch_mode = value.get("bound_voice_dispatch_mode", "confirm")
        if (
            type(bound_voice_dispatch_mode) is not str
            or bound_voice_dispatch_mode not in {"confirm", "direct"}
        ):
            raise BridgeContractError(
                "bound_voice_dispatch_mode must be confirm or direct"
            )
        return cls(
            schema_version="1.0",
            trusted_caller_id=trusted_caller_id,
            default_profile=default,
            profiles=profiles,
            dispatch_enabled=_boolean(value["dispatch_enabled"], name="dispatch_enabled"),
            require_confirmation=_boolean(
                value["require_confirmation"], name="require_confirmation"
            ),
            request_timeout_seconds=_integer(
                value["request_timeout_seconds"],
                name="request_timeout_seconds",
                minimum=1,
                maximum=120,
            ),
            poll_interval_seconds=_integer(
                value["poll_interval_seconds"],
                name="poll_interval_seconds",
                minimum=1,
                maximum=30,
            ),
            max_polls=_integer(
                value["max_polls"], name="max_polls", minimum=1, maximum=120
            ),
            max_input_chars=_integer(
                value["max_input_chars"],
                name="max_input_chars",
                minimum=1,
                maximum=20_000,
            ),
            bound_voice_dispatch_mode=cast(
                'Literal["confirm", "direct"]', bound_voice_dispatch_mode
            ),
            deepseek_harness=(
                DshWebConfig.from_mapping(
                    _object(raw_dsh, name="deepseek_harness config")
                )
                if raw_dsh is not None
                else None
            ),
        )

    def profile(self, name: str) -> ProfileRoute | None:
        return next((item for item in self.profiles if item.name == name), None)

    def to_mapping(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schema_version": self.schema_version,
            "trusted_caller_id": self.trusted_caller_id,
            "default_profile": self.default_profile,
            "profiles": [item.to_mapping() for item in self.profiles],
            "dispatch_enabled": self.dispatch_enabled,
            "require_confirmation": self.require_confirmation,
            "request_timeout_seconds": self.request_timeout_seconds,
            "poll_interval_seconds": self.poll_interval_seconds,
            "max_polls": self.max_polls,
            "max_input_chars": self.max_input_chars,
            "bound_voice_dispatch_mode": self.bound_voice_dispatch_mode,
        }
        if self.deepseek_harness is not None:
            result["deepseek_harness"] = self.deepseek_harness.to_mapping()
        return result


@dataclass(frozen=True, slots=True)
class BridgeRequest:
    schema_version: str
    caller_id: str
    session_id: str
    operation: Literal[
        "list_agents",
        "list_profiles",
        "switch_profile",
        "dispatch",
        "list_sessions",
        "bind_session",
        "show_binding",
        "unbind_session",
        "dispatch_shared",
        "dispatch_bound_voice",
        "create_session",
        "compress_session",
    ]
    profile: str | None = None
    input: str | None = None
    confirmed: bool = False
    device_id: str | None = None
    stored_session_id: str | None = None
    limit: int | None = None
    idempotency_key: str | None = None
    host: Literal["hermes", "deepseek-harness"] | None = None
    binding_revision: int | None = None
    attachments: tuple[BridgeAttachment, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> BridgeRequest:
        _closed(
            value,
            required={"schema_version", "caller_id", "session_id", "operation"},
            optional={
                "profile",
                "input",
                "confirmed",
                "device_id",
                "stored_session_id",
                "limit",
                "idempotency_key",
                "host",
                "binding_revision",
                "attachments",
            },
            name="bridge request",
        )
        if value["schema_version"] != "1.0":
            raise BridgeContractError("schema_version must be 1.0")
        caller_id = _text(value["caller_id"], name="caller_id")
        if _SESSION_RE.fullmatch(caller_id) is None:
            raise BridgeContractError("caller_id contains unsupported characters")
        session_id = _text(value["session_id"], name="session_id")
        if _SESSION_RE.fullmatch(session_id) is None:
            raise BridgeContractError("session_id contains unsupported characters")
        operation = _text(value["operation"], name="operation")
        if operation not in _OPERATIONS:
            raise BridgeContractError("operation is not supported")
        profile = value.get("profile")
        prompt = value.get("input")
        confirmed = value.get("confirmed", False)
        device_id = value.get("device_id")
        stored_session_id = value.get("stored_session_id")
        limit = value.get("limit")
        idempotency_key = value.get("idempotency_key")
        host = value.get("host")
        binding_revision = value.get("binding_revision")
        raw_attachments = value.get("attachments")
        if raw_attachments is None:
            attachments: tuple[BridgeAttachment, ...] = ()
        else:
            if type(raw_attachments) is not list:
                raise BridgeContractError("attachments must be an array")
            attachments = tuple(
                BridgeAttachment.from_mapping(
                    _object(item, name="bridge attachment")
                )
                for item in cast(Sequence[object], raw_attachments)
            )
            if not 1 <= len(attachments) <= 4:
                raise BridgeContractError("attachments must contain 1 to 4 items")
            if len({item.attachment_id for item in attachments}) != len(attachments):
                raise BridgeContractError("attachment ids must be unique")
            if sum(item.bytes for item in attachments) > 100 * 1024 * 1024:
                raise BridgeContractError("attachments exceed the message byte limit")
            if operation != "dispatch_bound_voice":
                raise BridgeContractError(f"{operation} forbids attachments")
        if host is not None and operation not in {
            "list_sessions",
            "bind_session",
            "dispatch_shared",
            "dispatch_bound_voice",
            "create_session",
            "compress_session",
        }:
            raise BridgeContractError(f"{operation} forbids host")
        if operation == "switch_profile":
            if (
                profile is None
                or prompt is not None
                or "confirmed" in value
                or device_id is not None
                or stored_session_id is not None
                or limit is not None
                or idempotency_key is not None
                or binding_revision is not None
            ):
                raise BridgeContractError(
                    "switch_profile requires profile and forbids input/confirmed"
                )
            profile = _profile_name(profile, name="profile")
        elif operation == "dispatch":
            if (
                prompt is None
                or profile is not None
                or device_id is not None
                or stored_session_id is not None
                or limit is not None
                or idempotency_key is not None
                or binding_revision is not None
            ):
                raise BridgeContractError("dispatch requires input and forbids profile")
            prompt = _text(prompt, name="input")
            confirmed = _boolean(confirmed, name="confirmed")
        elif operation == "list_sessions":
            if (
                profile is None
                or prompt is not None
                or "confirmed" in value
                or device_id is not None
                or stored_session_id is not None
                or idempotency_key is not None
                or binding_revision is not None
            ):
                raise BridgeContractError(
                    "list_sessions requires profile and only permits optional limit"
                )
            profile = _profile_name(profile, name="profile")
            host = "hermes" if host is None else _host_name(host)
            limit = 20 if limit is None else _integer(
                limit, name="limit", minimum=1, maximum=100
            )
        elif operation == "bind_session":
            if (
                profile is None
                or device_id is None
                or stored_session_id is None
                or prompt is not None
                or "confirmed" in value
                or limit is not None
                or idempotency_key is not None
            ):
                raise BridgeContractError(
                    "bind_session requires device_id, profile, and stored_session_id"
                )
            profile = _profile_name(profile, name="profile")
            host = "hermes" if host is None else _host_name(host)
            device_id = _text(device_id, name="device_id")
            if _SESSION_RE.fullmatch(device_id) is None:
                raise BridgeContractError("device_id contains unsupported characters")
            stored_session_id = _text(
                stored_session_id, name="stored_session_id"
            )
            if len(stored_session_id) > 512:
                raise BridgeContractError("stored_session_id exceeds 512 characters")
            if binding_revision is not None:
                binding_revision = _integer(
                    binding_revision,
                    name="binding_revision",
                    minimum=0,
                    maximum=2_147_483_647,
                )
        elif operation == "show_binding":
            if (
                device_id is None
                or profile is not None
                or prompt is not None
                or "confirmed" in value
                or stored_session_id is not None
                or limit is not None
                or idempotency_key is not None
                or binding_revision is not None
            ):
                raise BridgeContractError(
                    f"{operation} requires only device_id"
                )
            device_id = _text(device_id, name="device_id")
            if _SESSION_RE.fullmatch(device_id) is None:
                raise BridgeContractError("device_id contains unsupported characters")
        elif operation == "unbind_session":
            if (
                device_id is None
                or profile is not None
                or prompt is not None
                or "confirmed" in value
                or stored_session_id is not None
                or limit is not None
                or idempotency_key is not None
            ):
                raise BridgeContractError(
                    "unbind_session requires device_id and permits binding_revision"
                )
            device_id = _text(device_id, name="device_id")
            if _SESSION_RE.fullmatch(device_id) is None:
                raise BridgeContractError("device_id contains unsupported characters")
            if binding_revision is not None:
                binding_revision = _integer(
                    binding_revision,
                    name="binding_revision",
                    minimum=0,
                    maximum=2_147_483_647,
                )
        elif operation in {"dispatch_shared", "dispatch_bound_voice"}:
            if (
                device_id is None
                or prompt is None
                or profile is None
                or stored_session_id is None
                or host is None
                or binding_revision is None
                or limit is not None
                or idempotency_key is not None
                or (operation == "dispatch_bound_voice" and "confirmed" in value)
            ):
                raise BridgeContractError(
                    f"{operation} requires device_id, input, and an expected binding"
                )
            device_id = _text(device_id, name="device_id")
            if _SESSION_RE.fullmatch(device_id) is None:
                raise BridgeContractError("device_id contains unsupported characters")
            prompt = _text(prompt, name="input")
            confirmed = (
                _boolean(confirmed, name="confirmed")
                if operation == "dispatch_shared"
                else False
            )
            profile = _profile_name(profile, name="profile")
            host = _host_name(host)
            stored_session_id = _text(
                stored_session_id, name="stored_session_id"
            )
            if len(stored_session_id) > 512:
                raise BridgeContractError("stored_session_id exceeds 512 characters")
            binding_revision = _integer(
                binding_revision,
                name="binding_revision",
                minimum=1,
                maximum=2_147_483_647,
            )
        elif operation == "create_session":
            if (
                device_id is None
                or idempotency_key is None
                or "confirmed" not in value
                or profile is None
                or prompt is not None
                or stored_session_id is None
                or host is None
                or binding_revision is None
                or limit is not None
            ):
                raise BridgeContractError(
                    "create_session requires device_id, confirmed, idempotency_key, and an expected binding"
                )
            device_id = _text(device_id, name="device_id")
            if _SESSION_RE.fullmatch(device_id) is None:
                raise BridgeContractError("device_id contains unsupported characters")
            confirmed = _boolean(confirmed, name="confirmed")
            idempotency_key = _text(idempotency_key, name="idempotency_key")
            if _IDEMPOTENCY_RE.fullmatch(idempotency_key) is None:
                raise BridgeContractError("idempotency_key contains unsupported characters")
            profile = _profile_name(profile, name="profile")
            host = _host_name(host)
            stored_session_id = _text(
                stored_session_id, name="stored_session_id"
            )
            if len(stored_session_id) > 512:
                raise BridgeContractError("stored_session_id exceeds 512 characters")
            binding_revision = _integer(
                binding_revision,
                name="binding_revision",
                minimum=1,
                maximum=2_147_483_647,
            )
        elif operation == "compress_session":
            if (
                device_id is None
                or "confirmed" not in value
                or profile is None
                or prompt is not None
                or stored_session_id is None
                or host is None
                or binding_revision is None
                or limit is not None
                or idempotency_key is not None
            ):
                raise BridgeContractError(
                    "compress_session requires device_id, confirmed, and an expected binding"
                )
            device_id = _text(device_id, name="device_id")
            if _SESSION_RE.fullmatch(device_id) is None:
                raise BridgeContractError("device_id contains unsupported characters")
            confirmed = _boolean(confirmed, name="confirmed")
            profile = _profile_name(profile, name="profile")
            host = _host_name(host)
            stored_session_id = _text(
                stored_session_id, name="stored_session_id"
            )
            if len(stored_session_id) > 512:
                raise BridgeContractError("stored_session_id exceeds 512 characters")
            binding_revision = _integer(
                binding_revision,
                name="binding_revision",
                minimum=1,
                maximum=2_147_483_647,
            )
        elif (
            profile is not None
            or prompt is not None
            or "confirmed" in value
            or device_id is not None
            or stored_session_id is not None
            or limit is not None
            or idempotency_key is not None
            or binding_revision is not None
        ):
            raise BridgeContractError(f"{operation} forbids extra fields")
        return cls(
            schema_version="1.0",
            caller_id=caller_id,
            session_id=session_id,
            operation=cast(
                Literal[
                    "list_agents",
                    "list_profiles",
                    "switch_profile",
                    "dispatch",
                    "list_sessions",
                    "bind_session",
                    "show_binding",
                    "unbind_session",
                    "dispatch_shared",
                    "dispatch_bound_voice",
                    "create_session",
                    "compress_session",
                ],
                operation,
            ),
            profile=cast("str | None", profile),
            input=cast("str | None", prompt),
            confirmed=cast(bool, confirmed),
            device_id=cast("str | None", device_id),
            stored_session_id=cast("str | None", stored_session_id),
            limit=cast("int | None", limit),
            idempotency_key=cast("str | None", idempotency_key),
            host=cast('Literal["hermes", "deepseek-harness"] | None', host),
            binding_revision=cast("int | None", binding_revision),
            attachments=attachments,
        )


def read_config(path: Path) -> BridgeConfig:
    return BridgeConfig.from_mapping(_load_json_object(path, name="bridge config"))


def read_request(path: Path) -> BridgeRequest:
    return BridgeRequest.from_mapping(_load_json_object(path, name="bridge request"))
