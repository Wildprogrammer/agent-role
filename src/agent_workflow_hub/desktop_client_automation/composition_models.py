from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .models import AppSpec, AssertionSpec, Decision, StepSpec


@dataclass(frozen=True)
class DesktopAssertionSpec:
    kind: Literal["image-visible"]
    template: Path
    threshold: float
    timeout_seconds: float
    stable_seconds: float


@dataclass(frozen=True)
class DesktopRecoverySpec:
    steps: tuple[StepSpec, ...]
    postcondition: DesktopAssertionSpec


@dataclass(frozen=True)
class DesktopImplementationSpec:
    steps: tuple[StepSpec, ...]


@dataclass(frozen=True)
class DesktopCompositionRequest:
    source: Path
    scenario: Path
    catalog_root: Path
    source_surface: str
    target_platform: Literal["windows"]
    apps: tuple[AppSpec, ...]
    success_assertion: AssertionSpec
    decision: Decision
    effective_path_confirmed: bool
