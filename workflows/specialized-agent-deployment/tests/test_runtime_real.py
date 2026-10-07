"""Opt-in offline integration: real wheel install, independent import and MCP handshake."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from dataclasses import replace

import pytest

from agent_workflow_hub.specialized_agent_deployment.contracts import DeploymentRequest
from agent_workflow_hub.specialized_agent_deployment.sources import snapshot_composition
from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import plan_runtime, prepare_runtime, verify_runtime, _run, runtime_python


@pytest.mark.skipif(not os.environ.get("HUB_RUNTIME_SMOKE_REQUEST"), reason="requires prepared local wheels and explicit smoke request")
def test_real_standalone_runtime(tmp_path, monkeypatch):
    hub = Path(__file__).resolve().parents[3]
    data = json.loads(Path(os.environ["HUB_RUNTIME_SMOKE_REQUEST"]).read_text(encoding="utf-8"))
    original = DeploymentRequest.from_mapping(data)
    release = tmp_path / "runtime"
    request = replace(original, runtime={**original.runtime, "destination": str(release)})
    plan = plan_runtime(hub, request, snapshot_composition(hub, request))
    result = prepare_runtime(plan)
    assert result["status"] == "verified"
    if request.runtime["mode"] == "system-source":
        assert Path(result["package"]).is_relative_to(release / "hub" / "src")
        assert Path(result["dependency"]).is_relative_to(release / "packages")
        assert "site-packages" not in result["package"].casefold()
    else:
        assert Path(result["package"]).is_relative_to(release / "venv")
    # Poison development import paths; verification must still use the deployed copy.
    monkeypatch.setenv("PYTHONPATH", str(hub / "src"))
    assert verify_runtime(plan)["status"] == "verified"
    if request.runtime["mode"] == "system-source":
        command = [request.runtime["python"], "-I", str(release / "run-workflow-hub.py")]
    else:
        command = [runtime_python(plan), "-I", "-m", "agent_workflow_hub.cli"]
    _run([*command, "validate", str(release / "hub")], release)
    if original.host_options.get("mcp_servers"):
        assert result["mcp"] and result["mcp"][0]["tools"]
        assert result["mcp"][0]["server_info"]["name"] == "jenkins-operations"
        assert len(result["mcp"][0]["tools"]) == 17
    print(json.dumps({"release": str(release), **result}, ensure_ascii=False))
