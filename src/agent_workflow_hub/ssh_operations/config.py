from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .models import OperationRequest, SSHConfig, StepSpec, TargetConfig, TargetGroup


class ConfigError(ValueError):
    """Raised when SSH workflow configuration is incomplete or ambiguous."""


_TARGET_KEYS = {
    "host", "port", "username", "auth", "password", "password_env",
    "sudo_password", "sudo_password_env", "private_key", "private_key_passphrase",
    "private_key_passphrase_env", "via", "remote_os", "shell", "forward_agent",
    "timeout_seconds",
}
_GROUP_KEYS = {"targets", "max_parallel"}
_SSH_KEYS = {"known_hosts"}
_REQUEST_KEYS = {
    "operation", "request_id", "targets", "steps", "confirmed_high_impact", "max_parallel",
    "command", "target", "action", "source", "destination", "path", "mode",
    "overwrite", "recurse", "preserve", "resume", "listen_host", "listen_port",
    "destination_host", "destination_port", "sudo", "timeout_seconds",
    "content", "encoding",
}
_STEP_KEYS = {
    "id", "command", "working_directory", "environment", "depends_on",
    "timeout_seconds", "sudo", "use_pty", "on_failure",
}


def _absolute_existing(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise ConfigError(f"{label} path must be absolute")
    if not path.is_file():
        raise ConfigError(f"{label} path does not exist: {path}")
    return path.resolve()


def _unknown(actual: set[str], allowed: set[str], label: str) -> None:
    extra = sorted(actual - allowed)
    if extra:
        raise ConfigError(f"unknown field in {label}: {', '.join(extra)}")


def _bool(value: str, label: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "yes", "true", "on"}:
        return True
    if normalized in {"0", "no", "false", "off"}:
        return False
    raise ConfigError(f"{label} must be true or false")


def _secret(
    section: Mapping[str, str], direct: str, env_name: str, environ: Mapping[str, str]
) -> str | None:
    direct_value = section.get(direct, "")
    source = section.get(env_name, "").strip()
    if direct_value and source:
        raise ConfigError(f"{direct} source conflict")
    if source:
        if source not in environ:
            raise ConfigError(f"environment variable {source} is not set")
        return environ[source]
    return direct_value or None


def _resolve_file(base: Path, value: str, label: str) -> Path | None:
    if not value.strip():
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        raise ConfigError(f"{label} file does not exist: {candidate}")
    return candidate


def _validate_jump_graph(targets: Mapping[str, TargetConfig]) -> None:
    for name in targets:
        seen: set[str] = set()
        current: str | None = name
        while current:
            if current in seen:
                raise ConfigError(f"jump cycle detected at target {current}")
            seen.add(current)
            target = targets.get(current)
            if target is None:
                raise ConfigError(f"unknown jump target: {current}")
            current = target.via


def load_config(path: Path, *, environ: Mapping[str, str] | None = None) -> SSHConfig:
    source = _absolute_existing(path, "config")
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(source, encoding="utf-8")
    except configparser.Error as exc:
        raise ConfigError(f"invalid INI: {exc}") from exc
    env = os.environ if environ is None else environ
    base = source.parent
    known_hosts = base / "known_hosts"
    targets: dict[str, TargetConfig] = {}
    groups: dict[str, TargetGroup] = {}

    for section_name in parser.sections():
        section = parser[section_name]
        keys = set(section)
        if section_name == "ssh":
            _unknown(keys, _SSH_KEYS, section_name)
            configured = section.get("known_hosts", "known_hosts").strip()
            known_hosts = Path(configured)
            if not known_hosts.is_absolute():
                known_hosts = base / known_hosts
            known_hosts = known_hosts.resolve()
            continue
        if section_name.startswith("target:"):
            _unknown(keys, _TARGET_KEYS, section_name)
            name = section_name.split(":", 1)[1].strip()
            if not name or name in targets:
                raise ConfigError(f"invalid or duplicate target name: {name}")
            host = section.get("host", "").strip()
            username = section.get("username", "").strip()
            if not host or not username:
                raise ConfigError(f"target {name} requires host and username")
            auth = section.get("auth", "auto").strip().casefold()
            remote_os = section.get("remote_os", "auto").strip().casefold()
            shell = section.get("shell", "auto").strip().casefold()
            if auth not in {"password", "key", "agent", "auto"}:
                raise ConfigError(f"target {name} has unsupported auth")
            if remote_os not in {"auto", "windows", "macos", "linux"}:
                raise ConfigError(f"target {name} has unsupported remote_os")
            if shell not in {"auto", "powershell", "cmd", "sh", "bash", "zsh"}:
                raise ConfigError(f"target {name} has unsupported shell")
            password = _secret(section, "password", "password_env", env)
            sudo_password = _secret(section, "sudo_password", "sudo_password_env", env)
            passphrase = _secret(
                section, "private_key_passphrase", "private_key_passphrase_env", env
            )
            if sudo_password is None:
                sudo_password = password
            targets[name] = TargetConfig(
                name=name,
                host=host,
                port=section.getint("port", fallback=22),
                username=username,
                auth=auth,  # type: ignore[arg-type]
                password=password,
                sudo_password=sudo_password,
                private_key=_resolve_file(base, section.get("private_key", ""), "private_key"),
                private_key_passphrase=passphrase,
                via=section.get("via", "").strip() or None,
                remote_os=remote_os,  # type: ignore[arg-type]
                shell=shell,  # type: ignore[arg-type]
                forward_agent=_bool(section.get("forward_agent", "false"), "forward_agent"),
                timeout_seconds=section.getfloat("timeout_seconds", fallback=15.0),
            )
            continue
        if section_name.startswith("group:"):
            _unknown(keys, _GROUP_KEYS, section_name)
            name = section_name.split(":", 1)[1].strip()
            members = tuple(item.strip() for item in section.get("targets", "").split(",") if item.strip())
            if not name or not members:
                raise ConfigError(f"group {name} requires targets")
            groups[name] = TargetGroup(name, members, section.getint("max_parallel", fallback=1))
            continue
        # A private environment INI may be shared by multiple workflows. Ignore
        # unrelated sections while keeping SSH-owned sections strict above.
        continue

    if not targets:
        raise ConfigError("configuration requires at least one target")
    _validate_jump_graph(targets)
    for group in groups.values():
        missing = sorted(set(group.targets) - set(targets))
        if missing:
            raise ConfigError(f"group {group.name} references unknown targets: {', '.join(missing)}")
        if group.max_parallel < 1:
            raise ConfigError("max_parallel must be at least 1")
    return SSHConfig(
        source, known_hosts, MappingProxyType(targets), MappingProxyType(groups)
    )


def _step(value: Any, seen: set[str]) -> StepSpec:
    if not isinstance(value, dict):
        raise ConfigError("each step must be an object")
    _unknown(set(value), _STEP_KEYS, "step")
    step_id = value.get("id")
    command = value.get("command")
    if not isinstance(step_id, str) or not step_id or step_id in seen:
        raise ConfigError("step id must be non-empty and unique")
    if not isinstance(command, str) or not command:
        raise ConfigError(f"step {step_id} requires command")
    dependencies = tuple(value.get("depends_on", ()))
    if any(not isinstance(item, str) for item in dependencies):
        raise ConfigError(f"step {step_id} dependencies must be strings")
    if not set(dependencies) <= seen:
        raise ConfigError(f"step {step_id} depends on incomplete or unknown step")
    environment = value.get("environment", {})
    if not isinstance(environment, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()
    ):
        raise ConfigError(f"step {step_id} environment must be string pairs")
    on_failure = value.get("on_failure", "stop")
    if on_failure not in {"stop", "continue"}:
        raise ConfigError(f"step {step_id} has unsupported on_failure")
    seen.add(step_id)
    return StepSpec(
        id=step_id,
        command=command,
        working_directory=value.get("working_directory"),
        environment=MappingProxyType(dict(environment)),
        depends_on=dependencies,
        timeout_seconds=value.get("timeout_seconds"),
        sudo=bool(value.get("sudo", False)),
        use_pty=bool(value.get("use_pty", False)),
        on_failure=on_failure,
    )


def load_request(path: Path) -> OperationRequest:
    source = _absolute_existing(path, "request")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"invalid request JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("request must be an object")
    _unknown(set(value), _REQUEST_KEYS, "request")
    operation = value.get("operation")
    if not isinstance(operation, str) or not operation:
        raise ConfigError("request operation is required")
    raw_targets = value.get("targets")
    if raw_targets is None and isinstance(value.get("target"), str):
        raw_targets = [value["target"]]
    targets = tuple(raw_targets or ())
    if any(not isinstance(item, str) or not item for item in targets):
        raise ConfigError("targets must contain non-empty strings")
    if operation in {"run-steps", "sftp", "upload", "download", "forward"} and not targets:
        raise ConfigError(f"request operation {operation} requires at least one target")
    seen: set[str] = set()
    steps = tuple(_step(item, seen) for item in value.get("steps", ()))
    parameters = {key: item for key, item in value.items() if key not in {
        "operation", "request_id", "targets", "target", "steps",
        "confirmed_high_impact", "max_parallel"
    }}
    max_parallel = value.get("max_parallel", 1)
    if not isinstance(max_parallel, int) or max_parallel < 1:
        raise ConfigError("max_parallel must be at least 1")
    raw_request_id = value.get("request_id")
    if raw_request_id is None:
        request_id = re.sub(r"[^A-Za-z0-9._-]+", "-", source.stem).strip("-.")
        if not request_id:
            request_id = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    elif not isinstance(raw_request_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", raw_request_id
    ):
        raise ConfigError("request_id must be 1-80 safe filename characters")
    else:
        request_id = raw_request_id
    return OperationRequest(
        operation=operation,
        request_id=request_id,
        targets=targets,
        steps=steps,
        parameters=MappingProxyType(parameters),
        confirmed_high_impact=bool(value.get("confirmed_high_impact", False)),
        max_parallel=max_parallel,
    )
