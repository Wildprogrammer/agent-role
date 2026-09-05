from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_workflow_hub.specialized_agent_deployment.contracts import (
    DeploymentContractError,
    DeploymentManifest,
    DeploymentPlan,
    DeploymentRequest,
    EnablementCheck,
    EnablementResult,
    HostFacts,
    SkillFile,
    SkillSelection,
    SkillSnapshot,
    VerificationResult,
    WriteIntent,
    canonical_sha256,
    read_json_object,
)


def valid_selection(name: str = "requirements-analysis") -> dict[str, object]:
    return {
        "name": name,
        "source_kind": "hub-workflow",
        "source": f"workflows/{name}",
        "reason": "declared by the primary workflow",
    }


def valid_request(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "deployment_id": "20260830-test-agent",
        "agent_id": "test-agent-v1",
        "display_name": "Test Agent",
        "purpose": "Run one bounded workflow",
        "host": "hermes",
        "mode": "create",
        "primary_workflow": "automated-test-lifecycle",
        "related_workflows": [valid_selection()],
        "auxiliary_skills": [],
        "workdir": str(tmp_path.resolve()),
        "config_refs": [str((tmp_path / "lifecycle.ini").resolve())],
        "host_options": {"profile": "default", "enabled": True},
    }


def valid_snapshot(name: str = "requirements-analysis") -> SkillSnapshot:
    selection = SkillSelection.from_mapping(valid_selection(name))
    skill_file = SkillFile.from_mapping(
        {
            "relative_path": "SKILL.md",
            "size": 7,
            "sha256": "a" * 64,
        }
    )
    files = [skill_file.to_mapping()]
    return SkillSnapshot.from_mapping(
        {
            "selection": selection.to_mapping(),
            "files": files,
            "tree_sha256": canonical_sha256(files),
        }
    )


def test_snapshot_rejects_tree_digest_not_bound_to_files() -> None:
    with pytest.raises(DeploymentContractError):
        SkillSnapshot.from_mapping(
            {
                "selection": valid_selection(),
                "files": [
                    {
                        "relative_path": "SKILL.md",
                        "size": 7,
                        "sha256": "a" * 64,
                    }
                ],
                "tree_sha256": "b" * 64,
            }
        )


def valid_host_facts(tmp_path: Path) -> HostFacts:
    return HostFacts.from_mapping(
        {
            "host": "hermes",
            "compatibility": "verified",
            "version": "0.19.0",
            "target_root": str((tmp_path / "profile").resolve()),
            "facts": {"profile_exists": False},
        }
    )


def valid_write(tmp_path: Path) -> WriteIntent:
    return WriteIntent.from_mapping(
        {
            "target": str((tmp_path / "profile" / "AGENTS.md").resolve()),
            "action": "create",
            "content_sha256": "c" * 64,
            "size": 10,
            "description": "generated persona",
            "expected_before_sha256": None,
            "parameters": {"kind": "file"},
        }
    )


def test_write_intent_binds_finite_parameters_and_previous_digest(
    tmp_path: Path,
) -> None:
    intent = WriteIntent.from_mapping(
        {
            "target": str((tmp_path / "config.yaml").resolve()),
            "action": "config-set",
            "content_sha256": "a" * 64,
            "size": 3,
            "description": "set terminal cwd",
            "expected_before_sha256": "b" * 64,
            "parameters": {
                "argv": ["hermes", "config", "set", "terminal.cwd", "C:/work"],
                "config_key": "terminal.cwd",
            },
        }
    )
    assert WriteIntent.from_mapping(intent.to_mapping()) == intent
    with pytest.raises(DeploymentContractError):
        WriteIntent.from_mapping(
            intent.to_mapping() | {"parameters": {"bad": b"bytes"}}
        )


def test_request_round_trip_is_finite_and_canonical(tmp_path: Path) -> None:
    request = DeploymentRequest.from_mapping(valid_request(tmp_path))
    assert DeploymentRequest.from_mapping(request.to_mapping()) == request
    assert canonical_sha256(request.to_mapping()) == canonical_sha256(
        request.to_mapping()
    )
    json.dumps(request.to_mapping(), allow_nan=False)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("agent_id", ""),
        ("agent_id", "Upper"),
        ("agent_id", "a/b"),
        ("agent_id", "a..b"),
        ("deployment_id", "bad_id"),
        ("primary_workflow", "Bad Workflow"),
    ],
)
def test_request_rejects_unsafe_kebab_identifiers(
    tmp_path: Path, field: str, value: str
) -> None:
    with pytest.raises(DeploymentContractError):
        DeploymentRequest.from_mapping(valid_request(tmp_path) | {field: value})


@pytest.mark.parametrize("name", ["", "Upper", "a/b", "a..b", "bad_name"])
def test_selection_rejects_unsafe_name(name: str) -> None:
    with pytest.raises(DeploymentContractError):
        SkillSelection.from_mapping(valid_selection(name))


@pytest.mark.parametrize("host", ["", "codex", "Hermes"])
def test_request_rejects_unknown_host(tmp_path: Path, host: str) -> None:
    with pytest.raises(DeploymentContractError):
        DeploymentRequest.from_mapping(valid_request(tmp_path) | {"host": host})


@pytest.mark.parametrize("mode", ["", "delete", "CREATE"])
def test_request_rejects_unknown_mode(tmp_path: Path, mode: str) -> None:
    with pytest.raises(DeploymentContractError):
        DeploymentRequest.from_mapping(valid_request(tmp_path) | {"mode": mode})


def test_request_rejects_duplicate_or_primary_skills(tmp_path: Path) -> None:
    duplicate = valid_selection()
    cases = (
        {"related_workflows": [duplicate, duplicate]},
        {"auxiliary_skills": [duplicate]},
        {
            "auxiliary_skills": [
                valid_selection("automated-test-lifecycle")
            ]
        },
    )
    for change in cases:
        with pytest.raises(DeploymentContractError):
            DeploymentRequest.from_mapping(valid_request(tmp_path) | change)


def test_request_requires_absolute_paths(tmp_path: Path) -> None:
    cases = (
        {"workdir": "relative/work"},
        {"config_refs": ["relative.ini"]},
        {
            "auxiliary_skills": [
                {
                    "name": "helper-skill",
                    "source_kind": "external-skill",
                    "source": "relative/helper",
                    "reason": "selected by user",
                }
            ]
        },
    )
    for change in cases:
        with pytest.raises(DeploymentContractError):
            DeploymentRequest.from_mapping(valid_request(tmp_path) | change)


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), {"not-json"}, b"not-json"],
)
def test_request_rejects_non_finite_json(
    tmp_path: Path, bad_value: object
) -> None:
    value = valid_request(tmp_path)
    value["host_options"] = {"bad": bad_value}
    with pytest.raises(DeploymentContractError):
        DeploymentRequest.from_mapping(value)


@pytest.mark.parametrize(
    ("factory", "value"),
    [
        (SkillSelection.from_mapping, valid_selection() | {"extra": True}),
        (
            SkillFile.from_mapping,
            {
                "relative_path": "SKILL.md",
                "size": 1,
                "sha256": "a" * 64,
                "extra": True,
            },
        ),
    ],
)
def test_from_mapping_rejects_unknown_fields(factory, value) -> None:
    with pytest.raises(DeploymentContractError):
        factory(value)


def test_canonical_sha256_ignores_mapping_insertion_order() -> None:
    first = {"z": [1, {"b": True, "a": None}], "a": "text"}
    second = {"a": "text", "z": [1, {"a": None, "b": True}]}
    assert canonical_sha256(first) == canonical_sha256(second)


def test_read_json_object_rejects_non_object_and_non_finite(tmp_path: Path) -> None:
    array_path = tmp_path / "array.json"
    array_path.write_text("[]", encoding="utf-8")
    with pytest.raises(DeploymentContractError):
        read_json_object(array_path)

    nan_path = tmp_path / "nan.json"
    nan_path.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(DeploymentContractError):
        read_json_object(nan_path)


def test_read_json_object_accepts_a_valid_path(tmp_path: Path) -> None:
    object_path = tmp_path / "object.json"
    object_path.write_text('{"nested":{"value":1}}', encoding="utf-8")
    assert read_json_object(object_path) == {"nested": {"value": 1}}


def test_plan_digest_excludes_itself_and_timestamps(tmp_path: Path) -> None:
    request = DeploymentRequest.from_mapping(valid_request(tmp_path))
    snapshots = (
        valid_snapshot("automated-test-lifecycle"),
        valid_snapshot(),
    )
    host_facts = valid_host_facts(tmp_path)
    write = valid_write(tmp_path)
    common = {
        "schema_version": "1.0",
        "request": request.to_mapping(),
        "request_sha256": canonical_sha256(request.to_mapping()),
        "snapshots": [snapshot.to_mapping() for snapshot in snapshots],
        "persona": "persona\n",
        "persona_sha256": hashlib.sha256(b"persona\n").hexdigest(),
        "host_facts": host_facts.to_mapping(),
        "writes": [write.to_mapping()],
    }
    first = DeploymentPlan.from_mapping(common | {"generated_at": "first"})
    second = DeploymentPlan.from_mapping(common | {"generated_at": "second"})
    assert first.plan_sha256 == second.plan_sha256
    assert DeploymentPlan.from_mapping(first.to_mapping()) == first


def test_plan_requires_every_requested_snapshot_in_order(tmp_path: Path) -> None:
    request = DeploymentRequest.from_mapping(valid_request(tmp_path))
    primary = valid_snapshot("automated-test-lifecycle")
    related = valid_snapshot()
    base = {
        "schema_version": "1.0",
        "request": request.to_mapping(),
        "request_sha256": canonical_sha256(request.to_mapping()),
        "persona": "persona\n",
        "persona_sha256": hashlib.sha256(b"persona\n").hexdigest(),
        "host_facts": valid_host_facts(tmp_path).to_mapping(),
        "writes": [valid_write(tmp_path).to_mapping()],
        "generated_at": "now",
    }
    for snapshots in (
        [related.to_mapping()],
        [related.to_mapping(), primary.to_mapping()],
    ):
        with pytest.raises(DeploymentContractError):
            DeploymentPlan.from_mapping(base | {"snapshots": snapshots})


def test_plan_persona_digest_is_raw_utf8_bytes(tmp_path: Path) -> None:
    request = DeploymentRequest.from_mapping(valid_request(tmp_path))
    with pytest.raises(DeploymentContractError):
        DeploymentPlan.from_mapping(
            {
                "schema_version": "1.0",
                "request": request.to_mapping(),
                "request_sha256": canonical_sha256(request.to_mapping()),
                "snapshots": [
                    valid_snapshot("automated-test-lifecycle").to_mapping(),
                    valid_snapshot().to_mapping(),
                ],
                "persona": "persona\n",
                "persona_sha256": canonical_sha256("persona\n"),
                "host_facts": valid_host_facts(tmp_path).to_mapping(),
                "writes": [valid_write(tmp_path).to_mapping()],
                "generated_at": "now",
            }
        )


def test_manifest_records_bound_facts_and_managed_paths(tmp_path: Path) -> None:
    request = DeploymentRequest.from_mapping(valid_request(tmp_path))
    host_facts = valid_host_facts(tmp_path)
    target = str((tmp_path / "profile" / "AGENTS.md").resolve())
    manifest = DeploymentManifest.from_mapping(
        {
            "schema_version": "1.0",
            "deployment_id": request.deployment_id,
            "agent_id": request.agent_id,
            "request": request.to_mapping(),
            "request_sha256": canonical_sha256(request.to_mapping()),
            "plan_sha256": "d" * 64,
            "skill_tree_sha256s": {
                "automated-test-lifecycle": "a" * 64,
                "requirements-analysis": "b" * 64,
            },
            "host_facts": host_facts.to_mapping(),
            "managed_paths": [target],
            "status": "planned",
            "updated_at": "2026-08-30T00:00:00Z",
            "previous_manifest": None,
        }
    )
    assert manifest.managed_paths == (target,)
    assert DeploymentManifest.from_mapping(manifest.to_mapping()) == manifest


def test_manifest_requires_exact_skill_tree_digest_set(tmp_path: Path) -> None:
    request = DeploymentRequest.from_mapping(valid_request(tmp_path))
    base = {
        "schema_version": "1.0",
        "deployment_id": request.deployment_id,
        "agent_id": request.agent_id,
        "request": request.to_mapping(),
        "request_sha256": canonical_sha256(request.to_mapping()),
        "plan_sha256": "d" * 64,
        "host_facts": valid_host_facts(tmp_path).to_mapping(),
        "managed_paths": [str((tmp_path / "profile" / "AGENTS.md").resolve())],
        "status": "planned",
        "updated_at": "2026-08-30T00:00:00Z",
        "previous_manifest": None,
    }
    for digests in (
        {"requirements-analysis": "b" * 64},
        {
            "automated-test-lifecycle": "a" * 64,
            "unrelated-skill": "b" * 64,
        },
    ):
        with pytest.raises(DeploymentContractError):
            DeploymentManifest.from_mapping(
                base | {"skill_tree_sha256s": digests}
            )


def test_every_contract_rejects_unknown_fields(tmp_path: Path) -> None:
    request = DeploymentRequest.from_mapping(valid_request(tmp_path))
    snapshots = (
        valid_snapshot("automated-test-lifecycle"),
        valid_snapshot(),
    )
    host = valid_host_facts(tmp_path)
    write = valid_write(tmp_path)
    plan = DeploymentPlan.from_mapping(
        {
            "schema_version": "1.0",
            "request": request.to_mapping(),
            "request_sha256": canonical_sha256(request.to_mapping()),
            "snapshots": [snapshot.to_mapping() for snapshot in snapshots],
            "persona": "persona\n",
            "persona_sha256": hashlib.sha256(b"persona\n").hexdigest(),
            "host_facts": host.to_mapping(),
            "writes": [write.to_mapping()],
            "generated_at": "now",
        }
    )
    manifest = {
        "schema_version": "1.0",
        "deployment_id": request.deployment_id,
        "agent_id": request.agent_id,
        "request": request.to_mapping(),
        "request_sha256": canonical_sha256(request.to_mapping()),
        "plan_sha256": plan.plan_sha256,
        "skill_tree_sha256s": {
            "automated-test-lifecycle": "a" * 64,
            "requirements-analysis": "b" * 64,
        },
        "host_facts": host.to_mapping(),
        "managed_paths": [write.target],
        "status": "planned",
        "updated_at": "now",
        "previous_manifest": None,
    }
    verification = {
        "schema_version": "1.0",
        "deployment_id": request.deployment_id,
        "status": "verified",
        "static": {},
        "discovery": {},
        "behavior": {},
        "details": [],
    }
    cases = (
        (DeploymentRequest.from_mapping, request.to_mapping()),
        (SkillSnapshot.from_mapping, snapshots[0].to_mapping()),
        (HostFacts.from_mapping, host.to_mapping()),
        (WriteIntent.from_mapping, write.to_mapping()),
        (DeploymentPlan.from_mapping, plan.to_mapping()),
        (DeploymentManifest.from_mapping, manifest),
        (VerificationResult.from_mapping, verification),
    )
    for factory, mapping in cases:
        with pytest.raises(DeploymentContractError):
            factory(mapping | {"unexpected": True})


@pytest.mark.parametrize(
    "status",
    ["verified", "partially_verified", "failed", "outcome_unknown"],
)
def test_verification_accepts_only_documented_statuses(
    status: str,
) -> None:
    result = VerificationResult.from_mapping(
        {
            "schema_version": "1.0",
            "deployment_id": "20260830-test-agent",
            "status": status,
            "static": {"status": "passed"},
            "discovery": {"status": "passed"},
            "behavior": {"status": "not-run"},
            "details": [],
        }
    )
    assert result.status == status

    with pytest.raises(DeploymentContractError):
        VerificationResult.from_mapping(
            result.to_mapping() | {"status": "previewed"}
        )


def test_verification_enablement_round_trips_without_secret_values() -> None:
    enablement = EnablementResult(
        requested=True,
        platforms=("telegram", "weixin"),
        status="partially_ready",
        checks=(
            EnablementCheck(
                name="gateway",
                status="passed",
                details={"profile": "fixture-agent"},
            ),
            EnablementCheck(
                name="external_resources",
                status="not_checked",
                details={"reason": "no read-only entrypoint"},
            ),
        ),
        details={"source_profile": "default"},
    )
    result = VerificationResult.from_mapping(
        {
            "schema_version": "1.0",
            "deployment_id": "20260830-test-agent",
            "status": "partially_verified",
            "static": {"status": "passed"},
            "discovery": {"status": "passed"},
            "behavior": {"status": "passed"},
            "details": [],
            "enablement": enablement.to_mapping(),
        }
    )

    assert result.enablement == enablement
    assert VerificationResult.from_mapping(result.to_mapping()) == result


def test_verification_legacy_mapping_defaults_to_not_requested_enablement() -> None:
    result = VerificationResult.from_mapping(
        {
            "schema_version": "1.0",
            "deployment_id": "20260830-test-agent",
            "status": "verified",
            "static": {},
            "discovery": {},
            "behavior": {},
            "details": [],
        }
    )

    assert result.enablement == EnablementResult.not_requested()
    assert result.to_mapping()["enablement"] == EnablementResult.not_requested().to_mapping()


@pytest.mark.parametrize("status", ["partially_verified", "failed", "previewed"])
def test_enablement_rejects_top_level_status_names(status: str) -> None:
    with pytest.raises(DeploymentContractError):
        EnablementResult(
            requested=True,
            platforms=("weixin",),
            status=status,  # type: ignore[arg-type]
            checks=(),
            details={},
        )
