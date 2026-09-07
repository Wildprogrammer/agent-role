from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


Lifecycle = Literal["restart", "reuse", "attach-only"]
TargetKind = Literal["application", "system-dialog"]
Effect = Literal["none", "read", "idempotent", "create", "submit", "send", "delete", "unknown"]
Decision = Literal["generate-only", "generate-and-replay"]


@dataclass(frozen=True)
class AppSpec:
    alias: str
    target_kind: TargetKind
    lifecycle: Lifecycle
    executable: Path | None
    launch_executable: Path | None
    window_process_executable: Path | None
    launch_args: tuple[str, ...]
    window_title_regex: str
    startup_timeout_seconds: float
    shutdown_timeout_seconds: float
    force_terminate: bool


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    kind: Literal["text", "path"]
    default: str | None
    sensitive: bool


@dataclass(frozen=True)
class StepSpec:
    id: str
    app: str
    action: str
    effect: Effect
    optional: bool
    retries: int
    timeout_seconds: float
    values: dict[str, object]


@dataclass(frozen=True)
class AssertionSpec:
    app: str
    template: Path
    threshold: float
    timeout_seconds: float
    stable_seconds: float


@dataclass(frozen=True)
class AutomationRequest:
    source: Path
    name: str
    description: str
    source_surface: str
    target_platform: str
    apps: tuple[AppSpec, ...]
    parameters: tuple[ParameterSpec, ...]
    steps: tuple[StepSpec, ...]
    success_assertion: AssertionSpec
    decision: Decision
    effective_path_confirmed: bool
    aggregate_effect: Effect
