"""Optional test against installed, pinned upstream code, with no real Agent writes."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.dsh_web_gateway import DeepSeekHarnessWebGateway


def test_rc022_native_auth_and_rpc_schemas(tmp_path):
    entry = os.environ.get("DSH_NATIVE_TEST_ENTRY")
    if not entry or not shutil.which("node"):
        pytest.skip("Opt in with DSH_NATIVE_TEST_ENTRY pointing to installed 0.2.2-rc.1 lib/bin.js")
    package = tmp_path / "profiles/node_modules/@deepseek-ai/dsh/package.json"
    package.parent.mkdir(parents=True)
    package.write_text('{"version":"0.2.2-rc.1"}')
    process = subprocess.Popen(
        [shutil.which("node"), str(Path(__file__).with_name("dsh_native_fixture.mjs")), entry],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        # Bound startup read so native import failure cannot hang the test job.
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(1) as pool:
            line = pool.submit(process.stdout.readline)
            try:
                value = line.result(timeout=20)
            except TimeoutError:
                process.kill()
                raise AssertionError("Native transport fixture did not start") from None
        assert value, "Native fixture failed before startup (no user runtime was started)"
        config = json.loads(value)
        gateway = DeepSeekHarnessWebGateway(
            base_url=config["origin"], launch_token=config["token"], dsh_home=tmp_path,
            expected_version="0.2.2-rc.1", allowed_presets=("assistant-agent",),
            request_timeout_seconds=3, poll_interval_seconds=0, max_polls=1,
        )
        assert len(gateway.list_agents()) == 1
        assert gateway.list_sessions("assistant-agent")[0]["id"] == "synthetic-session"
        result = gateway.send_to_session(profile="assistant-agent", stored_session_id="synthetic-session", text="synthetic input")
        assert result.status == "complete" and result.text == "Synthetic native transport answer"
        assert gateway.cancel_session("synthetic-session") == "requested"
        assert gateway.create_session(profile="assistant-agent", idempotency_key="native-test").stored_session_id.startswith("xiaozhi-")
    finally:
        process.terminate()
        process.wait(timeout=5)
