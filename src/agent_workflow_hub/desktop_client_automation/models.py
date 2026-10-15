from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

from agent_workflow_hub.automation_composition.models import ComponentProvenance


Lifecycle = Literal["restart", "reuse", "attach-only"]
TargetKind = Literal["application", "system-dialog", "desktop"]
DisplayId = Literal["primary"]
Effect = Literal["none", "read", "idempotent", "create", "submit", "send", "delete", "unknown"]
Decision = Literal["generate-only", "generate-and-replay"]


@dataclass(frozen=True)
class AppSpec:
    alias: str
    target_kind: TargetKind
    lifecycle: Lifecycle
    display_id: DisplayId | None
    executable: Path | None
    launch_executable: Path | None
    window_process_executable: Path | None
    launch_args: tuple[str, ...]
    window_title_regex: str | None
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
class DesktopComponentRecovery:
    steps: tuple["StepSpec", ...]
    postcondition: "AssertionSpec"


@dataclass(frozen=True)
class DesktopComponentLifecycle:
    app_id: str
    component_id: str
    version: int
    occurrence_id: str
    descriptor_sha256: str
    implementation: str
    parameter_map: Mapping[str, str]
    precondition: "AssertionSpec | None"
    already_complete: "AssertionSpec | None"
    recovery: DesktopComponentRecovery | None
    postcondition: "AssertionSpec"


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
    component: DesktopComponentLifecycle | None = None


@dataclass(frozen=True)
class AssertionSpec:
    app: str
    template: Path
    threshold: float
    timeout_seconds: float
    stable_seconds: float


@dataclass(frozen=True)
class DesktopComposition:
    scenario_id: str
    provenance: tuple[ComponentProvenance, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "components": [
                {
                    "occurrence_id": item.occurrence_id,
                    "component_id": item.component_id,
                    "version": item.version,
                    "descriptor_sha256": item.descriptor_sha256,
                    "implementation": item.implementation,
                    "parameter_map": dict(sorted(item.parameter_map.items())),
                    "asset_sha256": dict(sorted(item.asset_sha256.items())),
                }
                for item in self.provenance
            ],
        }


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
    composition: DesktopComposition | None = None
