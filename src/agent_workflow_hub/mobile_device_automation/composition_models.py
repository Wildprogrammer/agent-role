from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from .models import AssertionSpec, Decision, DeviceSpec, Effect, ParameterSpec


TextInputMode = Literal["direct-ime", "pinyin-fallback"]


@dataclass(frozen=True)
class ComponentRecoverySpec:
    script: Path
    postcondition: AssertionSpec


@dataclass(frozen=True)
class AppComponentSpec:
    source: Path
    descriptor_sha256: str
    app_id: str
    id: str
    version: int
    effect: Effect
    timeout_seconds: float
    parameters: tuple[str, ...]
    precondition: AssertionSpec | None
    already_complete: AssertionSpec | None
    implementations: Mapping[str, Path]
    recovery: ComponentRecoverySpec | None
    postcondition: AssertionSpec


@dataclass(frozen=True)
class ComponentUseSpec:
    component_id: str
    version: int
    parameter_map: Mapping[str, str]


@dataclass(frozen=True)
class AppScenarioSpec:
    source: Path
    app_id: str
    id: str
    description: str
    parameters: tuple[ParameterSpec, ...]
    components: tuple[ComponentUseSpec, ...]


@dataclass(frozen=True)
class CompositionRequest:
    source: Path
    scenario: Path
    catalog_root: Path
    text_input_mode: TextInputMode
    source_surface: Literal["agent-device-mcp", "agent-device-cli"]
    target_platform: Literal["android"]
    device: DeviceSpec
    decision: Decision
    effective_path_confirmed: bool
