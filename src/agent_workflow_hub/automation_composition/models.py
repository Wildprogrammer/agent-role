from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, Mapping, TypeVar


A = TypeVar("A")
I = TypeVar("I")
R = TypeVar("R")
P = TypeVar("P")

Effect = Literal[
    "none",
    "read",
    "idempotent",
    "create",
    "submit",
    "send",
    "delete",
    "unknown",
]
EFFECT_RANK: dict[Effect, int] = {
    "none": 0,
    "read": 1,
    "idempotent": 2,
    "create": 3,
    "submit": 4,
    "send": 5,
    "delete": 6,
    "unknown": 7,
}


@dataclass(frozen=True)
class ComponentUse:
    component_id: str
    version: int
    parameter_map: Mapping[str, str]


@dataclass(frozen=True)
class ParameterDeclaration:
    name: str
    kind: Literal["text", "path"]
    default: str | None
    sensitive: bool


@dataclass(frozen=True)
class ComponentDefinition(Generic[A, I, R]):
    source: Path
    descriptor_sha256: str
    app_id: str
    id: str
    version: int
    effect: Effect
    timeout_seconds: float
    parameters: tuple[str, ...]
    precondition: A | None
    already_complete: A | None
    implementations: Mapping[str, I]
    recovery: R | None
    postcondition: A


@dataclass(frozen=True)
class ScenarioDefinition(Generic[P]):
    source: Path
    app_id: str
    id: str
    description: str
    parameters: tuple[P, ...]
    components: tuple[ComponentUse, ...]


@dataclass(frozen=True)
class ComponentProvenance:
    occurrence_id: str
    component_id: str
    version: int
    descriptor_sha256: str
    implementation: str
    parameter_map: Mapping[str, str]
    asset_sha256: Mapping[str, str]
