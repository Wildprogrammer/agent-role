from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping


Transport = Literal["usb", "wireless", "emulator"]
RunnerKind = Literal["agent-device", "airtest"]
Effect = Literal["none", "read", "idempotent", "create", "submit", "send", "delete"]
Decision = Literal["generate-only", "generate-and-replay"]


@dataclass(frozen=True)
class ViewportSpec:
    width: int
    height: int
    orientation: Literal["portrait", "landscape"]
    density: float


@dataclass(frozen=True)
class DeviceSpec:
    serial: str
    transport: Transport
    viewport: ViewportSpec


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    default: str | None
    sensitive: bool


@dataclass(frozen=True)
class AssertionSpec:
    kind: Literal["selector-visible", "image-visible"]
    selector: str | None
    template: Path | None
    threshold: float | None
    timeout_seconds: float


@dataclass(frozen=True)
class ComponentRecoveryStepSpec:
    script: Path
    postcondition: AssertionSpec


@dataclass(frozen=True)
class ComponentLifecycleSpec:
    app_id: str
    component_id: str
    version: int
    occurrence_id: str
    descriptor_sha256: str
    text_input_mode: Literal["direct-ime", "pinyin-fallback"]
    implementation: Literal["default", "direct-ime", "pinyin-fallback"]
    parameter_map: Mapping[str, str]
    precondition: AssertionSpec | None
    already_complete: AssertionSpec | None
    recovery: ComponentRecoveryStepSpec | None


@dataclass(frozen=True)
class StepSpec:
    id: str
    runner: RunnerKind
    effect: Effect
    timeout_seconds: float
    script: Path | None
    action: Literal["click-image", "wait-image"] | None
    template: Path | None
    threshold: float | None
    assertion: AssertionSpec | None
    component: ComponentLifecycleSpec | None = None


@dataclass(frozen=True)
class AutomationRequest:
    source: Path
    name: str
    description: str
    source_surface: Literal["agent-device-mcp", "agent-device-cli"]
    target_platform: Literal["android"]
    device: DeviceSpec
    parameters: tuple[ParameterSpec, ...]
    steps: tuple[StepSpec, ...]
    decision: Decision
    effective_path_confirmed: bool
    aggregate_effect: Effect
