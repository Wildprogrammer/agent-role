from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from agent_workflow_hub.specialized_agent_deployment.contracts import DeploymentRequest
from agent_workflow_hub.specialized_agent_deployment.contracts import DeploymentContractError
from agent_workflow_hub.specialized_agent_deployment.sources import snapshot_composition


def fixture_request(tmp_path):
    hub = tmp_path / "hub"
    skill = hub / "workflows/primary-flow/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: primary-flow\ndescription: fixture\n---\n# Use\n", encoding="utf-8")
    asset = skill.parent / "assets/template.txt"
    asset.parent.mkdir()
    asset.write_bytes(b"resource")
    source = hub / "src/agent_workflow_hub/__init__.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"VALUE = 1\n")
    (hub / "SKILL.md").write_text("root", encoding="utf-8")
    (hub / "pyproject.toml").write_text('[build-system]\nrequires=["setuptools>=75"]\nbuild-backend="setuptools.build_meta"\n[project]\nname="agent-workflow-hub"\nversion="0.1.0"\ndependencies=["mcp>=1"]\n', encoding="utf-8")
    for relative in ("src/agent_workflow_hub/__pycache__/bad.pyc", "workflows/primary-flow/outputs/private.ini", "workflows/primary-flow/tests/test_private.py", ".env", "docs/private.md"):
        p = hub / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"DO NOT SHIP")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "dependency-1-py3-none-any.whl").write_bytes(b"wheel fixture")
    request = DeploymentRequest.from_mapping({
        "schema_version": "1.0", "deployment_id": "fixture", "agent_id": "fixture-agent",
        "display_name": "Fixture", "purpose": "test", "host": "deepseek-harness", "mode": "create",
        "primary_workflow": "primary-flow", "related_workflows": [], "auxiliary_skills": [],
        "workdir": str(tmp_path), "config_refs": [], "host_options": {},
        "runtime": {"python": str(Path(sys._base_executable).resolve()), "wheelhouse": str(wheels), "destination": str(tmp_path / "releases/v1")},
    })
    return hub, request


def runtime_request(tmp_path, runtime):
    hub, original = fixture_request(tmp_path)
    data = original.to_mapping()
    data["runtime"] = runtime
    return hub, DeploymentRequest.from_mapping(data)


def test_system_source_runtime_uses_local_wheelhouse(tmp_path):
    _, request = runtime_request(tmp_path, {
        "mode": "system-source", "python": str(Path(sys._base_executable).resolve()),
        "wheelhouse": str(tmp_path / "wheels"),
        "destination": str(tmp_path / "system-release"),
    })
    assert request.runtime["mode"] == "system-source"
    assert request.runtime["wheelhouse"].endswith("wheels")


def test_legacy_wheelhouse_runtime_normalizes_to_isolated(tmp_path):
    _, request = fixture_request(tmp_path)
    assert request.runtime["mode"] == "isolated"


@pytest.mark.parametrize("runtime", [
    {"mode": "isolated", "python": str(Path(sys._base_executable).resolve()), "destination": "ABS"},
    {"mode": "system-source", "python": str(Path(sys._base_executable).resolve()), "destination": "ABS"},
])
def test_runtime_modes_have_closed_fields(tmp_path, runtime):
    runtime = {key: (str(tmp_path / value) if value == "ABS" else value) for key, value in runtime.items()}
    with pytest.raises(DeploymentContractError):
        runtime_request(tmp_path, runtime)


def test_runtime_input_roundtrips_without_changing_legacy_requests(tmp_path):
    _, request = fixture_request(tmp_path)
    assert request.to_mapping()["runtime"]["destination"].endswith("v1")
    data = request.to_mapping()
    del data["runtime"]
    assert "runtime" not in DeploymentRequest.from_mapping(data).to_mapping()


def test_runtime_snapshot_has_code_resources_and_wheels_not_private_outputs(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import plan_runtime
    hub, request = fixture_request(tmp_path)
    plan = plan_runtime(hub, request, snapshot_composition(hub, request))
    paths = {record["path"] for record in plan["files"]}
    assert "hub/src/agent_workflow_hub/__init__.py" in paths
    assert "hub/workflows/primary-flow/assets/template.txt" in paths
    assert "wheelhouse/dependency-1-py3-none-any.whl" in paths
    assert not any("private" in path or "__pycache__" in path or path.endswith(".env") for path in paths)
    assert not Path(request.runtime["destination"]).exists()


def test_runtime_source_or_wheel_change_changes_plan(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import plan_runtime
    hub, request = fixture_request(tmp_path)
    snapshots = snapshot_composition(hub, request)
    first = plan_runtime(hub, request, snapshots)
    (hub / "src/agent_workflow_hub/__init__.py").write_bytes(b"VALUE = 2\n")
    second = plan_runtime(hub, request, snapshots)
    assert first["sha256"] != second["sha256"]
    wheel = next(Path(request.runtime["wheelhouse"]).glob("*.whl"))
    wheel.write_bytes(b"changed")
    assert plan_runtime(hub, request, snapshots)["sha256"] != second["sha256"]


def test_runtime_never_takes_over_an_existing_directory(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import plan_runtime, RuntimeBundleError
    hub, request = fixture_request(tmp_path)
    target = Path(request.runtime["destination"])
    target.mkdir(parents=True)
    (target / "user.txt").write_bytes(b"keep")
    with pytest.raises(RuntimeBundleError, match="existing"):
        plan_runtime(hub, request, snapshot_composition(hub, request))
    assert (target / "user.txt").read_bytes() == b"keep"


def test_runtime_commands_install_offline_without_editable_or_shared_site_packages(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import runtime_commands
    hub, request = fixture_request(tmp_path)
    commands = runtime_commands(request.runtime, ["setuptools>=75"])
    assert "--copies" in commands[0] and "venv" in commands[0]
    installs = [argv for argv in commands if "install" in argv]
    assert installs and all("--no-index" in argv for argv in installs)
    assert not any("--system-site-packages" in argv or "-e" in argv for argv in commands)
    assert all(str(hub) not in arg for argv in installs for arg in argv)


def test_system_plan_has_launcher_local_packages_and_no_venv(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import plan_runtime
    hub, request = runtime_request(tmp_path, {
        "mode": "system-source", "python": str(Path(sys._base_executable).resolve()),
        "wheelhouse": str(tmp_path / "wheels"),
        "destination": str(tmp_path / "system-release"),
    })
    plan = plan_runtime(hub, request, snapshot_composition(hub, request))
    assert plan["mode"] == "system-source"
    assert any(item["path"] == "run-workflow-hub.py" for item in plan["files"])
    assert any(item["path"].startswith("wheelhouse/") for item in plan["files"])
    install = next(command for command in plan["commands"] if "install" in command)
    assert "--target" in install
    assert str(Path(request.runtime["destination"]) / "packages") in install
    assert "--no-index" in install and "--ignore-installed" in install
    assert "mcp>=1" in install
    assert not any("venv" in arg for command in plan["commands"] for arg in command)


def test_system_launcher_prioritizes_deployed_source():
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import launcher_bytes
    launcher = launcher_bytes()
    assert b"sys.path.insert(0" in launcher
    assert b'ROOT / "packages"' in launcher
    assert b'ROOT / "hub" / "src"' in launcher
    assert b"agent_workflow_hub.cli" in launcher


def test_runtime_install_failure_does_not_create_ready_receipt(tmp_path, monkeypatch):
    from agent_workflow_hub.specialized_agent_deployment import runtime_bundle as module
    hub, request = fixture_request(tmp_path)
    plan = module.plan_runtime(hub, request, snapshot_composition(hub, request))
    def fail(*args):
        raise module.RuntimeBundleError("offline install failed")
    monkeypatch.setattr(module, "_run", fail)
    with pytest.raises(module.RuntimeBundleError, match="offline install"):
        module.prepare_runtime(plan)
    root = Path(request.runtime["destination"])
    assert (root / "hub/src/agent_workflow_hub/__init__.py").is_file()
    assert not (root / "runtime-ready.json").exists()


def test_source_drift_rejected_before_release_creation(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment import runtime_bundle as module
    hub, request = fixture_request(tmp_path)
    plan = module.plan_runtime(hub, request, snapshot_composition(hub, request))
    (hub / "src/agent_workflow_hub/__init__.py").write_bytes(b"changed")
    with pytest.raises(module.RuntimeBundleError, match="source changed"):
        module.prepare_runtime(plan)
    assert not Path(request.runtime["destination"]).exists()


def test_runtime_only_retargets_hub_mcp_and_keeps_external_ini(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import effective_mcp_servers, runtime_python
    _, request = fixture_request(tmp_path)
    ini = str(tmp_path / "outside.ini")
    server = {"workflow": "primary-flow", "server_name": "fixture", "command": sys.executable,
              "args": ["-m", "agent_workflow_hub.cli", "jenkins-mcp", ini], "cwd": str(tmp_path)}
    other = {**server, "server_name": "third-party", "args": ["-m", "third_party"]}
    updated, unchanged = effective_mcp_servers(request, (server, other))
    assert updated["command"] == runtime_python(request.runtime)
    assert updated["args"] == ["-I", *server["args"]]
    assert updated["args"][-1] == ini
    assert unchanged == other


def test_system_runtime_retargets_hub_mcp_to_source_launcher(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import effective_mcp_servers
    _, request = runtime_request(tmp_path, {
        "mode": "system-source", "python": str(Path(sys._base_executable).resolve()),
        "wheelhouse": str(tmp_path / "wheels"),
        "destination": str(tmp_path / "system-release"),
    })
    ini = str(tmp_path / "outside.ini")
    server = {"workflow": "primary-flow", "server_name": "fixture", "command": sys.executable,
              "args": ["-m", "agent_workflow_hub.cli", "jenkins-mcp", ini], "cwd": str(tmp_path)}
    updated = effective_mcp_servers(request, (server,))[0]
    assert updated["command"] == request.runtime["python"]
    assert updated["args"] == ["-I", str(Path(request.runtime["destination"]) / "run-workflow-hub.py"),
                                "jenkins-mcp", ini]
    assert updated["cwd"] == str(Path(request.runtime["destination"]) / "hub")


def test_service_prepares_runtime_before_host_apply_and_stops_on_failure(tmp_path, monkeypatch):
    from dataclasses import replace
    from agent_workflow_hub.specialized_agent_deployment import service as module
    from agent_workflow_hub.specialized_agent_deployment.contracts import HostFacts, WriteIntent
    hub, request = fixture_request(tmp_path)
    events = []
    class Adapter:
        def discover(self, request):
            return HostFacts(host=request.host, compatibility="verified", version="fixture",
                             target_root=str(tmp_path / "host"), facts={})
        def plan_writes(self, request, snapshots, persona, facts):
            content = persona.encode()
            return (WriteIntent(target=str(tmp_path / "host/AGENTS.md"), action="create",
                content_sha256=hashlib.sha256(content).hexdigest(), size=len(content),
                description="fixture", parameters={"kind": "file", "payload_kind": "persona",
                "staging_relative": "host-files/AGENTS.md"}),)
        def apply(self, context):
            events.append("host apply")
    adapter = Adapter()
    service = module.DeploymentService(adapter_factory=lambda *args: adapter)
    request_path = hub / "request.json"
    request_path.write_text(json.dumps(request.to_mapping()), encoding="utf-8")
    manifest = service.preview(hub, request_path)
    def fail(plan):
        events.append("prepare")
        raise RuntimeError("fixture install failure")
    monkeypatch.setattr(module, "prepare_runtime", fail)
    manifest_path = hub / "workflows/specialized-agent-deployment/outputs/fixture/deployment-manifest.json"
    with pytest.raises(module.DeploymentServiceError, match="install failure"):
        service.apply(hub, manifest_path, manifest.plan_sha256)
    assert events == ["prepare"]
    assert not (tmp_path / "host").exists()
