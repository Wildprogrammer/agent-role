from __future__ import annotations

import hashlib
from pathlib import Path

from agent_workflow_hub.specialized_agent_deployment.contracts import (
    DeploymentRequest,
    HostFacts,
    SkillFile,
    SkillSelection,
    SkillSnapshot,
    WriteIntent,
    canonical_sha256,
)
from agent_workflow_hub.specialized_agent_deployment.planning import (
    build_deployment_plan,
    planned_manifest,
)
from agent_workflow_hub.specialized_agent_deployment.rendering import (
    render_deployment_preview,
    render_persona,
)


def make_selection(name: str) -> SkillSelection:
    return SkillSelection(
        name=name,
        source_kind="hub-workflow",
        source=f"workflows/{name}",
        reason="declared by primary workflow",
    )


def make_snapshot(name: str, marker: str) -> SkillSnapshot:
    content = f"{name}:{marker}".encode()
    file = SkillFile(
        relative_path="SKILL.md",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    files = (file,)
    return SkillSnapshot(
        selection=make_selection(name),
        files=files,
        tree_sha256=canonical_sha256([item.to_mapping() for item in files]),
    )


def make_request(tmp_path: Path) -> DeploymentRequest:
    return DeploymentRequest(
        schema_version="1.0",
        deployment_id="fixture-deployment",
        agent_id="fixture-agent",
        display_name="自动化测试需求 Agent",
        purpose="澄清测试需求并协调自动化测试全链路",
        host="hermes",
        mode="create",
        primary_workflow="automated-test-lifecycle",
        related_workflows=(make_selection("requirements-analysis"),),
        auxiliary_skills=(),
        workdir=str((tmp_path / "workspace").resolve()),
        config_refs=(str((tmp_path / "lifecycle.ini").resolve()),),
        host_options={},
    )


def make_facts(tmp_path: Path, *, version: str = "0.19.0") -> HostFacts:
    return HostFacts(
        host="hermes",
        compatibility="verified",
        version=version,
        target_root=str((tmp_path / "profiles" / "fixture-agent").resolve()),
        facts={"profile_exists": False},
    )


def make_writes(tmp_path: Path, *, marker: str = "a") -> tuple[WriteIntent, ...]:
    content = f"persona:{marker}".encode()
    return (
        WriteIntent(
            target=str(
                (tmp_path / "profiles" / "fixture-agent" / "AGENTS.md").resolve()
            ),
            action="create",
            content_sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            description="generated minimal persona",
        ),
    )


def make_snapshots(marker: str = "a") -> tuple[SkillSnapshot, ...]:
    return (
        make_snapshot("automated-test-lifecycle", marker),
        make_snapshot("requirements-analysis", marker),
    )


def test_persona_routes_without_copying_workflow_logic(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    text = render_persona(request, make_snapshots())
    assert request.display_name in text
    assert request.purpose in text
    assert "automated-test-lifecycle" in text
    assert "完整加载" in text
    assert "主 Skill 明确声明" in text
    assert request.workdir in text
    assert request.config_refs[0] in text
    for forbidden in (
        "requirements_review",
        "integration_review",
        "force",
        "Jenkins",
        "ZenTao",
        "失败四选一",
        "步骤 1",
    ):
        assert forbidden not in text


def test_persona_rejects_snapshot_composition_drift(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    try:
        render_persona(request, make_snapshots()[1:])
    except ValueError as exc:
        assert "snapshot" in str(exc).lower()
    else:
        raise AssertionError("missing primary snapshot was accepted")


def test_preview_has_one_fixed_gate_and_complete_review_facts(
    tmp_path: Path,
) -> None:
    plan = build_deployment_plan(
        make_request(tmp_path),
        make_snapshots(),
        make_facts(tmp_path),
        make_writes(tmp_path),
    )
    preview = render_deployment_preview(plan)
    assert preview.count("deployment_review") == 1
    assert plan.request.agent_id in preview
    assert plan.request.primary_workflow in preview
    assert all(item.tree_sha256 in preview for item in plan.snapshots)
    assert plan.host_facts.version in preview
    assert all(item.target in preview for item in plan.writes)
    assert "模型调用" in preview
    assert "会话记录" in preview
    assert "备份" in preview
    assert "回滚" in preview
    assert plan.plan_sha256 in preview


def test_plan_sha_binds_source_host_persona_and_writes(tmp_path: Path) -> None:
    request = make_request(tmp_path)

    def digest(
        snapshots=make_snapshots(),
        facts=make_facts(tmp_path),
        writes=make_writes(tmp_path),
    ) -> str:
        return build_deployment_plan(
            request, snapshots, facts, writes
        ).plan_sha256

    original = digest()
    assert digest(snapshots=make_snapshots("changed")) != original
    assert digest(facts=make_facts(tmp_path, version="0.19.1")) != original
    assert digest(writes=make_writes(tmp_path, marker="changed")) != original

    changed_request = DeploymentRequest.from_mapping(
        request.to_mapping() | {"purpose": "changed purpose"}
    )
    assert build_deployment_plan(
        changed_request,
        make_snapshots(),
        make_facts(tmp_path),
        make_writes(tmp_path),
    ).plan_sha256 != original


def test_repeated_generation_is_byte_deterministic(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    snapshots = make_snapshots()
    facts = make_facts(tmp_path)
    writes = make_writes(tmp_path)
    first = build_deployment_plan(request, snapshots, facts, writes)
    second = build_deployment_plan(request, snapshots, facts, writes)
    assert render_persona(request, snapshots) == render_persona(request, snapshots)
    assert first.to_mapping() == second.to_mapping()
    assert render_deployment_preview(first) == render_deployment_preview(second)
    assert planned_manifest(first).to_mapping() == planned_manifest(second).to_mapping()


def test_planned_manifest_binds_plan_and_exact_managed_paths(
    tmp_path: Path,
) -> None:
    plan = build_deployment_plan(
        make_request(tmp_path),
        make_snapshots(),
        make_facts(tmp_path),
        make_writes(tmp_path),
    )
    manifest = planned_manifest(plan)
    assert manifest.status == "planned"
    assert manifest.plan_sha256 == plan.plan_sha256
    assert manifest.request_sha256 == canonical_sha256(plan.request.to_mapping())
    assert manifest.skill_tree_sha256s == {
        item.selection.name: item.tree_sha256 for item in plan.snapshots
    }
    assert manifest.managed_paths == tuple(item.target for item in plan.writes)


def test_guidance_only_plan_has_no_writes_or_confirmation(
    tmp_path: Path,
) -> None:
    facts = HostFacts(
        host="hermes",
        compatibility="compatible_not_runnable",
        version="0.19.0",
        target_root=str((tmp_path / "profiles" / "fixture-agent").resolve()),
        facts={"guidance": ["prepare the existing host runtime"]},
    )
    plan = build_deployment_plan(
        make_request(tmp_path),
        make_snapshots(),
        facts,
        (),
    )
    manifest = planned_manifest(plan)
    preview = render_deployment_preview(plan)
    assert manifest.status == "guidance_only"
    assert manifest.managed_paths == ()
    assert "无宿主写入" in preview
    assert "deployment_review" not in preview
