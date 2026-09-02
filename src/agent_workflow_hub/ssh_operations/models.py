from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

AuthMode = Literal["password", "key", "agent", "auto"]
RemoteOS = Literal["auto", "windows", "macos", "linux"]
ShellName = Literal["auto", "powershell", "cmd", "sh", "bash", "zsh"]


def frozen_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True)
class TargetConfig:
    name: str
    host: str
    port: int = 22
    username: str = ""
    auth: AuthMode = "auto"
    password: str | None = None
    sudo_password: str | None = None
    private_key: Path | None = None
    private_key_passphrase: str | None = None
    via: str | None = None
    remote_os: RemoteOS = "auto"
    shell: ShellName = "auto"
    forward_agent: bool = False
    timeout_seconds: float = 15.0


@dataclass(frozen=True)
class TargetGroup:
    name: str
    targets: tuple[str, ...]
    max_parallel: int = 1


@dataclass(frozen=True)
class SSHConfig:
    source: Path
    known_hosts: Path
    targets: Mapping[str, TargetConfig]
    groups: Mapping[str, TargetGroup] = field(default_factory=frozen_mapping)


@dataclass(frozen=True)
class StepSpec:
    id: str
    command: str
    working_directory: str | None = None
    environment: Mapping[str, str] = field(default_factory=frozen_mapping)
    depends_on: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    sudo: bool = False
    use_pty: bool = False
    on_failure: Literal["stop", "continue"] = "stop"


@dataclass(frozen=True)
class OperationRequest:
    operation: str
    request_id: str = ""
    targets: tuple[str, ...] = ()
    steps: tuple[StepSpec, ...] = ()
    parameters: Mapping[str, Any] = field(default_factory=frozen_mapping)
    confirmed_high_impact: bool = False
    max_parallel: int = 1


@dataclass(frozen=True)
class CommandResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    signal: str | None = None
    duration_seconds: float = 0.0
    target: str | None = None
    step_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class OperationResult:
    status: str
    results: tuple[Any, ...] = ()
    details: Mapping[str, Any] = field(default_factory=frozen_mapping)
