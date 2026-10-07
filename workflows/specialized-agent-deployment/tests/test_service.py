from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_workflow_hub.specialized_agent_deployment.contracts import (
    DeploymentManifest,
    DeploymentRequest,
    HostFacts,
    VerificationResult,
    WriteIntent,
)
from agent_workflow_hub.specialized_agent_deployment.hosts.base import (
    ApplyContext,
    HostApplyResult,
    VerifyContext,
)
from agent_workflow_hub.specialized_agent_deployment.filesystem import (
    TransactionOutcomeUnknown,
)
from agent_workflow_hub.specialized_agent_deployment.service import (
    DeploymentService,
    DeploymentServiceError,
    StaleDeploymentConfirmation,
)


def write_skill(hub_root: Path, name: str) -> Path:
    root = hub_root / "workflows" / name
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: service fixture\n"
        "---\n"
        "# Use\n\nUse fixture.\n",
        encoding="utf-8",
    )
    return root


def write_request(hub_root: Path, host_root: Path) -> Path:
    path = (hub_root / "request.json").resolve()
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "deployment_id": "service-fixture",
                "agent_id": "fixture-agent",
                "display_name": "Fixture Agent",
                "purpose": "test service",
                "host": "hermes",
                "mode": "create",
                "primary_workflow": "primary-flow",
                "related_workflows": [],
                "auxiliary_skills": [],
                "workdir": str((hub_root / "work").resolve()),
                "config_refs": [],
                "host_options": {"fake_host_root": str(host_root)},
            }
        ),
        encoding="utf-8",
    )
    return path


class FakeAdapter:
    kind = "hermes"

    def __init__(self, host_root: Path, staging_root: Path) -> None:
        self.host_root = host_root
        self.staging_root = staging_root
        self.apply_calls = 0
        self.verify_calls = 0
        self.finalize_calls = 0
        self.events: list[str] = []

    def discover(self, request: DeploymentRequest) -> HostFacts:
        return HostFacts(
            host="hermes",
            compatibility="verified",
            version="fake-1",
            target_root=str(self.host_root),
            facts={"profile_exists": False},
        )

    def plan_writes(self, request, snapshots, persona, facts):
        content = persona.encode()
        return (
            WriteIntent(
                target=str((self.host_root / "AGENTS.md").resolve()),
                action="create",
                content_sha256=hashlib.sha256(content).hexdigest(),
                size=len(content),
                description="fake persona",
                parameters={
                    "kind": "file",
                    "payload_kind": "persona",
                    "staging_relative": "host-files/AGENTS.md",
                },
            ),
        )

    def apply(self, context: ApplyContext) -> HostApplyResult:
        self.apply_calls += 1
        self.events.append("apply")
        self.host_root.mkdir(parents=True, exist_ok=True)
        staged = context.staging_root / "host-files/AGENTS.md"
        target = self.host_root / "AGENTS.md"
        target.write_bytes(staged.read_bytes())
        return HostApplyResult(
            status="applied",
            managed_paths=(target,),
            details={},
        )

    def verify(self, context: VerifyContext) -> VerificationResult:
        self.verify_calls += 1
        self.events.append("verify")
        return VerificationResult(
            schema_version=context.manifest.schema_version,
            deployment_id=context.manifest.deployment_id,
            status="verified",
            static={"status": "passed"},
            discovery={"status": "passed"},
            behavior={"status": "passed"},
            details=(),
        )

    def finalize(self, context):
        self.finalize_calls += 1
        self.events.append("finalize")
        return context.verification


@pytest.fixture
def service_fixture(tmp_path: Path):
    hub_root = (tmp_path / "hub").resolve()
    (hub_root / "workflows/specialized-agent-deployment").mkdir(parents=True)
    write_skill(hub_root, "primary-flow")
    host_root = (tmp_path / "host-profile").resolve()
    request_path = write_request(hub_root, host_root)
    adapters: list[FakeAdapter] = []

    def factory(request: DeploymentRequest, staging_root: Path):
        adapter = FakeAdapter(host_root, staging_root)
        adapters.append(adapter)
        return adapter

    return hub_root, host_root, request_path, adapters, DeploymentService(
        adapter_factory=factory
    )


def output_paths(hub_root: Path) -> tuple[Path, Path]:
    output = hub_root / "workflows/specialized-agent-deployment/outputs/service-fixture"
    staging = hub_root / "workspace/workflows/specialized-agent-deployment/service-fixture"
    return output, staging


def test_preview_writes_only_hub_outputs_and_staging(service_fixture) -> None:
    hub_root, host_root, request_path, adapters, service = service_fixture
    manifest = service.preview(hub_root, request_path)
    output, staging = output_paths(hub_root)
    assert manifest.status == "planned"
    assert sorted(path.name for path in output.iterdir()) == [
        "deployment-manifest.json",
        "deployment-preview.md",
    ]
    assert (staging / "host-files/AGENTS.md").is_file()
    assert not host_root.exists()
    assert adapters[-1].apply_calls == 0


def test_apply_requires_exact_confirmed_plan_sha(service_fixture) -> None:
    hub_root, host_root, request_path, adapters, service = service_fixture
    manifest = service.preview(hub_root, request_path)
    output, _ = output_paths(hub_root)
    manifest_path = output / "deployment-manifest.json"
    with pytest.raises(StaleDeploymentConfirmation):
        service.apply(hub_root, manifest_path, "0" * 64)
    assert not host_root.exists()
    assert all(adapter.apply_calls == 0 for adapter in adapters)

    applied = service.apply(
        hub_root,
        manifest_path,
        manifest.plan_sha256,
    )
    assert applied.status == "verified"
    assert (host_root / "AGENTS.md").is_file()
    assert (output / "verification.json").is_file()
    assert adapters[-1].events == ["apply", "verify", "finalize"]
    assert adapters[-1].finalize_calls == 1


def test_apply_rejects_source_drift_before_host_write(service_fixture) -> None:
    hub_root, host_root, request_path, adapters, service = service_fixture
    manifest = service.preview(hub_root, request_path)
    output, _ = output_paths(hub_root)
    skill = hub_root / "workflows/primary-flow/SKILL.md"
    skill.write_text(skill.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
    with pytest.raises(StaleDeploymentConfirmation):
        service.apply(
            hub_root,
            output / "deployment-manifest.json",
            manifest.plan_sha256,
        )
    assert not host_root.exists()
    assert all(adapter.apply_calls == 0 for adapter in adapters)


def test_apply_preserves_unknown_transaction_outcome(service_fixture) -> None:
    hub_root, host_root, request_path, _, _ = service_fixture

    class UnknownAdapter(FakeAdapter):
        def apply(self, context: ApplyContext) -> HostApplyResult:
            self.apply_calls += 1
            raise TransactionOutcomeUnknown("write result cannot be reconciled")

    adapters: list[UnknownAdapter] = []

    def factory(request: DeploymentRequest, staging_root: Path):
        adapter = UnknownAdapter(host_root, staging_root)
        adapters.append(adapter)
        return adapter

    service = DeploymentService(adapter_factory=factory)
    manifest = service.preview(hub_root, request_path)
    output, _ = output_paths(hub_root)
    result = service.apply(
        hub_root,
        output / "deployment-manifest.json",
        manifest.plan_sha256,
    )
    assert result.status == "outcome_unknown"
    assert adapters[-1].apply_calls == 1
    assert not host_root.exists()


def test_verify_does_not_call_apply(service_fixture) -> None:
    hub_root, _, request_path, adapters, service = service_fixture
    manifest = service.preview(hub_root, request_path)
    output, _ = output_paths(hub_root)
    result = service.verify(
        hub_root,
        output / "deployment-manifest.json",
        None,
    )
    assert result.status == "verified"
    assert adapters[-1].verify_calls == 1
    assert adapters[-1].apply_calls == 0
    assert (output / "verification.json").is_file()


def test_service_has_no_second_confirmation_store() -> None:
    import inspect
    import agent_workflow_hub.specialized_agent_deployment.service as service_module

    source = inspect.getsource(service_module)
    assert "ConfirmationStore" not in source
    assert "confirmation_id" not in source


def test_paths_must_be_absolute(service_fixture) -> None:
    _, _, request_path, _, service = service_fixture
    with pytest.raises(DeploymentServiceError):
        service.preview(Path("relative"), request_path)
