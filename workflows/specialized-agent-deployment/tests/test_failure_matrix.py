from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_workflow_hub.specialized_agent_deployment import filesystem
from agent_workflow_hub.specialized_agent_deployment.contracts import (
    DeploymentRequest,
    HostFacts,
    VerificationResult,
    WriteIntent,
)
from agent_workflow_hub.specialized_agent_deployment.filesystem import (
    MANAGED_MARKER,
    ManagedTargetError,
    ManagedWrite,
    TransactionApplyError,
    apply_managed_transaction,
    reconcile_uncertain_write,
    validate_managed_target,
)
from agent_workflow_hub.specialized_agent_deployment.hosts.base import (
    ApplyContext,
    HostApplyResult,
    VerifyContext,
)
from agent_workflow_hub.specialized_agent_deployment.hosts.deepseek_harness import (
    validate_web_behavior_evidence,
)
from agent_workflow_hub.specialized_agent_deployment.service import (
    DeploymentService,
    StaleDeploymentConfirmation,
)


def _write_skill(hub_root: Path) -> Path:
    root = hub_root / "workflows" / "primary-flow"
    root.mkdir(parents=True)
    skill = root / "SKILL.md"
    skill.write_text(
        "---\nname: primary-flow\ndescription: failure fixture\n---\n"
        "# Fixture\n\nUse fixture.\n",
        encoding="utf-8",
    )
    return skill


def _write_request(
    hub_root: Path,
    host_root: Path,
    *,
    host: str = "hermes",
    mode: str = "create",
    config_refs: list[str] | None = None,
) -> Path:
    path = (hub_root / "request.json").resolve()
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "deployment_id": "failure-matrix",
                "agent_id": "failure-agent",
                "display_name": "Failure Agent",
                "purpose": "exercise failures",
                "host": host,
                "mode": mode,
                "primary_workflow": "primary-flow",
                "related_workflows": [],
                "auxiliary_skills": [],
                "workdir": str((hub_root / "work").resolve()),
                "config_refs": config_refs or [],
                "host_options": {"fixture_root": str(host_root)},
            }
        ),
        encoding="utf-8",
    )
    return path


class MatrixAdapter:
    def __init__(self, request: DeploymentRequest, staging_root: Path, state: dict):
        self.kind = request.host
        self.request = request
        self.staging_root = staging_root
        self.state = state

    def discover(self, request: DeploymentRequest) -> HostFacts:
        return HostFacts(
            host=request.host,
            compatibility=self.state.get("compatibility", "verified"),
            version=self.state.get("version", "fixture-1"),
            target_root=str(Path(self.state["host_root"]).resolve()),
            facts={"fixture": True},
        )

    def plan_writes(self, request, snapshots, persona, facts):
        if facts.compatibility != "verified":
            return ()
        if self.state.get("target_conflict"):
            raise RuntimeError("target_conflict: create target appeared")
        content = persona.encode("utf-8")
        target = Path(self.state["host_root"]) / "AGENTS.md"
        return (
            WriteIntent(
                target=str(target.resolve()),
                action="create",
                content_sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
                description="matrix persona",
                parameters={
                    "kind": "file",
                    "payload_kind": "persona",
                    "staging_relative": "host-files/AGENTS.md",
                },
            ),
        )

    def apply(self, context: ApplyContext) -> HostApplyResult:
        self.state["apply_calls"] = self.state.get("apply_calls", 0) + 1
        target = Path(self.state["host_root"]) / "AGENTS.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            (context.staging_root / "host-files" / "AGENTS.md").read_bytes()
        )
        return HostApplyResult(status="applied", managed_paths=(target,), details={})

    def verify(self, context: VerifyContext) -> VerificationResult:
        status = self.state.get("verify_status", "verified")
        return VerificationResult(
            schema_version=context.manifest.schema_version,
            deployment_id=context.manifest.deployment_id,
            status=status,
            static={"status": "passed"},
            discovery={"status": "passed"},
            behavior={
                "status": "failed" if status == "failed" else "passed",
                "business_task_executed": False,
            },
            details=(),
        )


def _service_fixture(
    tmp_path: Path,
    *,
    host: str = "hermes",
    config_refs: list[str] | None = None,
):
    hub_root = (tmp_path / "hub").resolve()
    (hub_root / "workflows" / "specialized-agent-deployment").mkdir(parents=True)
    skill = _write_skill(hub_root)
    host_root = (tmp_path / "host").resolve()
    request = _write_request(
        hub_root,
        host_root,
        host=host,
        config_refs=config_refs,
    )
    state = {"host_root": host_root, "apply_calls": 0}

    def factory(parsed: DeploymentRequest, staging_root: Path):
        return MatrixAdapter(parsed, staging_root, state)

    return hub_root, skill, request, state, DeploymentService(adapter_factory=factory)


@pytest.mark.parametrize("drift", ["source", "host-version"])
def test_preview_drift_is_stale_before_host_write(tmp_path: Path, drift: str) -> None:
    hub_root, skill, request, state, service = _service_fixture(tmp_path)
    manifest = service.preview(hub_root, request)
    if drift == "source":
        skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    else:
        state["version"] = "fixture-2"
    manifest_path = (
        hub_root
        / "workflows/specialized-agent-deployment/outputs/failure-matrix/"
        "deployment-manifest.json"
    )
    with pytest.raises(StaleDeploymentConfirmation):
        service.apply(hub_root, manifest_path, manifest.plan_sha256)
    assert state["apply_calls"] == 0
    assert not Path(state["host_root"]).exists()


def test_create_target_appearing_before_apply_is_a_non_overwrite_conflict(
    tmp_path: Path,
) -> None:
    hub_root, _, request, state, service = _service_fixture(tmp_path)
    manifest = service.preview(hub_root, request)
    state["target_conflict"] = True
    manifest_path = (
        hub_root
        / "workflows/specialized-agent-deployment/outputs/failure-matrix/"
        "deployment-manifest.json"
    )
    with pytest.raises(StaleDeploymentConfirmation, match="target_conflict"):
        service.apply(hub_root, manifest_path, manifest.plan_sha256)
    assert state["apply_calls"] == 0


@pytest.mark.parametrize("marker", [None, {"deployment_id": "other"}])
def test_update_rejects_missing_or_mismatched_marker(
    tmp_path: Path,
    marker: dict[str, str] | None,
) -> None:
    target = (tmp_path / "managed").resolve()
    target.mkdir()
    if marker is not None:
        (target / MANAGED_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    expected = {
        "schema_version": "1.0",
        "deployment_id": "failure-matrix",
        "agent_id": "failure-agent",
        "host": "hermes",
    }
    with pytest.raises(ManagedTargetError):
        validate_managed_target(target, mode="update", expected_marker=expected)


def test_create_failure_restores_only_current_transaction_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep", encoding="utf-8")
    targets = tuple((tmp_path / f"created-{index}.txt").resolve() for index in range(3))
    writes = tuple(
        ManagedWrite(
            target=target,
            content=b"new",
            expected_before_sha256=None,
        )
        for target in targets
    )
    real = filesystem._atomic_replace
    calls = 0

    def fail_second(target: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected create failure")
        real(target, content)

    monkeypatch.setattr(filesystem, "_atomic_replace", fail_second)
    with pytest.raises(TransactionApplyError):
        apply_managed_transaction(writes, backup_root=(tmp_path / "backups").resolve())
    assert not any(target.exists() for target in targets)
    assert unrelated.read_text(encoding="utf-8") == "keep"


def test_update_failure_restores_managed_files_and_keeps_unknown_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = (tmp_path / "first.txt").resolve()
    second = (tmp_path / "second.txt").resolve()
    unknown = tmp_path / "unknown.txt"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    unknown.write_bytes(b"keep")
    writes = (
        ManagedWrite(
            target=first,
            content=b"new-one",
            expected_before_sha256=hashlib.sha256(b"one").hexdigest(),
        ),
        ManagedWrite(
            target=second,
            content=b"new-two",
            expected_before_sha256=hashlib.sha256(b"two").hexdigest(),
        ),
    )
    real = filesystem._atomic_replace
    calls = 0

    def fail_second(target: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected update failure")
        real(target, content)

    monkeypatch.setattr(filesystem, "_atomic_replace", fail_second)
    with pytest.raises(TransactionApplyError):
        apply_managed_transaction(writes, backup_root=(tmp_path / "backups").resolve())
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"
    assert unknown.read_bytes() == b"keep"


@pytest.mark.parametrize(
    ("actual", "expected"),
    [(b"intended", "applied"), (b"different", "outcome_unknown")],
)
def test_timeout_reconciliation_never_blindly_replays(
    tmp_path: Path,
    actual: bytes,
    expected: str,
) -> None:
    target = (tmp_path / "timeout.txt").resolve()
    target.write_bytes(actual)
    digest = hashlib.sha256(b"intended").hexdigest()
    assert reconcile_uncertain_write(target, digest) == expected


def test_failed_behavior_is_recorded_without_running_a_business_task(tmp_path: Path) -> None:
    hub_root, _, request, state, service = _service_fixture(tmp_path)
    state["verify_status"] = "failed"
    manifest = service.preview(hub_root, request)
    manifest_path = (
        hub_root
        / "workflows/specialized-agent-deployment/outputs/failure-matrix/"
        "deployment-manifest.json"
    )
    applied = service.apply(hub_root, manifest_path, manifest.plan_sha256)
    evidence = json.loads(
        (manifest_path.parent / "verification.json").read_text(encoding="utf-8")
    )
    assert applied.status == "applied"
    assert evidence["status"] == "failed"
    assert evidence["behavior"]["business_task_executed"] is False


def test_dsh_missing_web_evidence_is_partially_verified(tmp_path: Path) -> None:
    hub_root, _, request, _, service = _service_fixture(
        tmp_path,
        host="deepseek-harness",
    )
    manifest = service.preview(hub_root, request)
    result = validate_web_behavior_evidence(None, manifest)
    assert result.status == "partially_verified"


def test_unknown_dsh_runtime_is_guidance_only_with_zero_host_writes(
    tmp_path: Path,
) -> None:
    hub_root, _, request, state, service = _service_fixture(
        tmp_path,
        host="deepseek-harness",
    )
    state["compatibility"] = "unverified"
    state["version"] = "unknown"
    manifest = service.preview(hub_root, request)
    assert manifest.status == "guidance_only"
    assert state["apply_calls"] == 0
    assert not Path(state["host_root"]).exists()


def test_config_reference_records_path_without_reading_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = (tmp_path / "private.ini").resolve()
    config.write_text("password=<SECRET>", encoding="utf-8")
    hub_root, _, request, _, service = _service_fixture(
        tmp_path,
        config_refs=[str(config)],
    )
    real_read_text = Path.read_text

    def guarded_read(path: Path, *args, **kwargs):
        if path.resolve(strict=False) == config:
            raise AssertionError("config content was read")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    manifest = service.preview(hub_root, request)
    assert manifest.request.config_refs == (str(config),)
