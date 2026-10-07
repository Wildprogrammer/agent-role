"""Host-neutral adapter protocol and finite lifecycle contexts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..contracts import (
    DeploymentManifest,
    DeploymentPlan,
    DeploymentRequest,
    HostFacts,
    SkillSnapshot,
    VerificationResult,
    WriteIntent,
)


@dataclass(frozen=True, kw_only=True)
class ApplyContext:
    plan: DeploymentPlan
    manifest: DeploymentManifest
    staging_root: Path
    backup_root: Path


@dataclass(frozen=True, kw_only=True)
class VerifyContext:
    manifest: DeploymentManifest
    staging_root: Path
    behavior_evidence_path: Path | None


@dataclass(frozen=True, kw_only=True)
class HostApplyResult:
    status: Literal["applied", "rolled_back", "outcome_unknown"]
    managed_paths: tuple[Path, ...]
    details: Mapping[str, object]


@dataclass(frozen=True, kw_only=True)
class FinalizeContext:
    apply_result: HostApplyResult
    verification: VerificationResult


class HostAdapter(Protocol):
    kind: Literal["hermes", "deepseek-harness"]

    def discover(self, request: DeploymentRequest) -> HostFacts: ...

    def plan_writes(
        self,
        request: DeploymentRequest,
        snapshots: tuple[SkillSnapshot, ...],
        persona: str,
        facts: HostFacts,
    ) -> tuple[WriteIntent, ...]: ...

    def apply(self, context: ApplyContext) -> HostApplyResult: ...

    def verify(self, context: VerifyContext) -> VerificationResult: ...

    def finalize(self, context: FinalizeContext) -> VerificationResult: ...


def guidance_host_facts(
    *,
    host: Literal["hermes", "deepseek-harness"],
    compatibility: Literal["missing", "unverified", "compatible_not_runnable"],
    version: str | None,
    target_root: Path | None,
    guidance: tuple[str, ...],
) -> HostFacts:
    """Represent a read-only discovery result that requires user preparation."""

    if not guidance or not all(type(item) is str and item.strip() for item in guidance):
        raise ValueError("guidance must contain nonblank steps")
    return HostFacts(
        host=host,
        compatibility=compatibility,
        version=version,
        target_root=str(target_root) if target_root is not None else None,
        facts={"guidance": list(guidance)},
    )


__all__ = [
    "ApplyContext",
    "FinalizeContext",
    "HostAdapter",
    "HostApplyResult",
    "VerifyContext",
    "guidance_host_facts",
]
