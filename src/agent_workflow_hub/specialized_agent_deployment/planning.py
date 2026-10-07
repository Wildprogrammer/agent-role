"""Build deterministic plans and planned deployment manifests."""

from __future__ import annotations

import hashlib

from .contracts import (
    DeploymentManifest,
    DeploymentPlan,
    DeploymentRequest,
    HostFacts,
    SkillSnapshot,
    WriteIntent,
    canonical_sha256,
)
from .rendering import render_persona


class DeploymentPlanningError(ValueError):
    """Raised when deployment planning inputs are inconsistent."""


def build_deployment_plan(
    request: DeploymentRequest,
    snapshots: tuple[SkillSnapshot, ...],
    host_facts: HostFacts,
    writes: tuple[WriteIntent, ...],
) -> DeploymentPlan:
    """Bind the request, frozen sources, persona, host facts, and writes."""

    if type(request) is not DeploymentRequest:
        raise DeploymentPlanningError("request must be a DeploymentRequest")
    snapshots = tuple(snapshots)
    writes = tuple(writes)
    if type(host_facts) is not HostFacts:
        raise DeploymentPlanningError("host_facts must be HostFacts")
    persona = render_persona(request, snapshots)
    return DeploymentPlan(
        schema_version=request.schema_version,
        request=request,
        request_sha256=canonical_sha256(request.to_mapping()),
        snapshots=snapshots,
        persona=persona,
        persona_sha256=hashlib.sha256(persona.encode("utf-8")).hexdigest(),
        host_facts=host_facts,
        writes=writes,
        generated_at=None,
    )


def planned_manifest(plan: DeploymentPlan) -> DeploymentManifest:
    """Create the deterministic manifest payload shown during review."""

    if type(plan) is not DeploymentPlan:
        raise DeploymentPlanningError("plan must be a DeploymentPlan")
    status = "planned" if plan.writes else "guidance_only"
    managed_paths = tuple(
        dict.fromkeys(
            item.target for item in plan.writes if item.action != "command"
        )
    )
    return DeploymentManifest(
        schema_version=plan.schema_version,
        deployment_id=plan.request.deployment_id,
        agent_id=plan.request.agent_id,
        request=plan.request,
        request_sha256=plan.request_sha256,
        plan_sha256=plan.plan_sha256,
        skill_tree_sha256s={
            item.selection.name: item.tree_sha256 for item in plan.snapshots
        },
        host_facts=plan.host_facts,
        managed_paths=managed_paths,
        status=status,
        updated_at=None,
        previous_manifest=None,
    )


__all__ = [
    "DeploymentPlanningError",
    "build_deployment_plan",
    "planned_manifest",
]
