from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_workflow_hub.automation_composition.models import (
    ComponentDefinition as AppComponentSpec,
    ComponentUse as ComponentUseSpec,
    ScenarioDefinition as AppScenarioSpec,
)

from .models import AssertionSpec, Decision, DeviceSpec


TextInputMode = Literal["direct-ime", "pinyin-fallback"]


@dataclass(frozen=True)
class ComponentRecoverySpec:
    script: Path
    postcondition: AssertionSpec


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
