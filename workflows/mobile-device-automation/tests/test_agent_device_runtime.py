from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_workflow_hub.mobile_device_automation.agent_device_runtime import (
    AgentDeviceRuntime,
    AndroidDevice,
    ProcessResult,
    RuntimeFailure,
    bind_android_device,
    list_android_devices,
    probe_agent_device,
    run_process,
)


class FakeRunner:
    def __init__(self) -> None:
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""
        self.json_result = None
        self.argv: list[str] = []
        self.timeout_seconds = 0.0

    def __call__(self, argv, *, timeout_seconds: float) -> ProcessResult:
        self.argv = list(argv)
        self.timeout_seconds = timeout_seconds
        stdout = self.stdout
        if self.json_result is not None:
            stdout = json.dumps(self.json_result)
        return ProcessResult(self.returncode, stdout, self.stderr)


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def executable(tmp_path: Path) -> Path:
    path = tmp_path / ("agent-device.cmd" if os.name == "nt" else "agent-device")
    path.write_text("placeholder", encoding="utf-8")
    return path.resolve()


def _device(serial: str, *, claimed_by: str | None = None) -> AndroidDevice:
    return AndroidDevice(
        serial=serial,
        name="Pixel",
        kind="physical",
        claimed_by=claimed_by,
    )


def test_probe_accepts_only_02119(fake_runner: FakeRunner, executable: Path) -> None:
    fake_runner.stdout = "agent-device 0.21.19\n"
    assert probe_agent_device(executable, runner=fake_runner).status == "ready"
    fake_runner.stdout = "agent-device 0.21.18\n"
    assert probe_agent_device(executable, runner=fake_runner).status == "wrong-version"


def test_probe_reports_missing_executable(tmp_path: Path, fake_runner: FakeRunner) -> None:
    probe = probe_agent_device(tmp_path / "missing", runner=fake_runner)
    assert probe.status == "missing"
    assert fake_runner.argv == []


def test_devices_normalizes_android_json(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.json_result = {
        "success": True,
        "data": {
            "devices": [
                {
                    "platform": "android",
                    "target": "mobile",
                    "kind": "device",
                    "id": "R58M123",
                    "name": "Pixel",
                    "booted": True,
                }
            ]
        },
    }
    devices = list_android_devices(executable, runner=fake_runner)
    assert devices[0].serial == "R58M123"
    assert fake_runner.argv == [str(executable), "devices", "--platform", "android", "--json"]


def test_devices_ignore_non_android_offline_and_unbooted(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.json_result = {
        "devices": [
            {
                "platform": "ios",
                "target": "mobile",
                "booted": True,
                "identifiers": {"serial": "ios-1"},
            },
            {
                "platform": "android",
                "target": "mobile",
                "booted": False,
                "identifiers": {"serial": "sleeping"},
            },
            {
                "platform": "android",
                "target": "mobile",
                "booted": True,
                "status": "offline",
                "identifiers": {"serial": "offline"},
            },
            {
                "platform": "android",
                "target": "desktop",
                "booted": True,
                "identifiers": {"serial": "desktop"},
            },
        ]
    }
    assert list_android_devices(executable, runner=fake_runner) == ()


def test_devices_reject_invalid_json(fake_runner: FakeRunner, executable: Path) -> None:
    fake_runner.stdout = "not-json"
    with pytest.raises(RuntimeFailure, match="agent_device_invalid_json"):
        list_android_devices(executable, runner=fake_runner)


def test_devices_reject_upstream_failure_envelope(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.json_result = {
        "success": False,
        "error": {"code": "DEVICE_QUERY_FAILED"},
    }
    with pytest.raises(RuntimeFailure, match="DEVICE_QUERY_FAILED"):
        list_android_devices(executable, runner=fake_runner)


def test_bind_refuses_ambiguous_devices() -> None:
    with pytest.raises(RuntimeFailure, match="ambiguous_device"):
        bind_android_device((_device("one"), _device("two")), requested_serial=None)


def test_bind_reports_zero_or_missing_requested_device() -> None:
    with pytest.raises(RuntimeFailure, match="device_not_found"):
        bind_android_device((), requested_serial=None)
    with pytest.raises(RuntimeFailure, match="device_not_found"):
        bind_android_device((_device("one"),), requested_serial="two")


def test_bind_rejects_foreign_claim() -> None:
    with pytest.raises(RuntimeFailure, match="target_identity_changed"):
        bind_android_device(
            (_device("one", claimed_by="foreign-session"),),
            requested_serial="one",
        )


def test_bind_preserves_wireless_serial_exactly() -> None:
    device = _device("192.0.2.10:5555")
    assert bind_android_device((device,), requested_serial=None).serial == "192.0.2.10:5555"


def test_replay_uses_explicit_serial_session_and_parameter_argv(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.json_result = {"status": "ok"}
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    runtime.replay_ad(
        Path("C:/bundle/flow.ad"),
        serial="R58M123",
        session="mobile-run-1",
        parameters={"QUERY": "hello"},
    )
    assert fake_runner.argv == [
        str(executable),
        "replay",
        "C:/bundle/flow.ad",
        "--platform",
        "android",
        "--serial",
        "R58M123",
        "--session",
        "mobile-run-1",
        "--keep-session",
        "-e",
        "QUERY=hello",
        "--json",
    ]


def test_replay_materializes_lowercase_parameters_as_agent_device_env(
    fake_runner: FakeRunner, executable: Path, tmp_path: Path
) -> None:
    script = (tmp_path / "flow.ad").resolve()
    script.write_text(
        'context platform=android\nfill "id=search" "${query}"\n',
        encoding="utf-8",
    )
    state_dir = (tmp_path / "state").resolve()
    state_dir.mkdir()
    fake_runner.json_result = {"status": "ok"}

    AgentDeviceRuntime(executable=executable, runner=fake_runner).replay_ad(
        script,
        serial="R58M123",
        session="mobile-run-1",
        parameters={"query": "hello"},
        state_dir=state_dir,
    )

    replay_script = Path(fake_runner.argv[2])
    assert replay_script != script
    assert replay_script.read_text("utf-8") == (
        'context platform=android\nfill "id=search" "${QUERY}"\n'
    )
    assert fake_runner.argv[-3:] == ["-e", "QUERY=hello", "--json"]


def test_replay_rejects_runtime_parameter_name_collisions(
    fake_runner: FakeRunner, executable: Path
) -> None:
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    with pytest.raises(RuntimeFailure, match="agent_device_parameter_collision"):
        runtime.replay_ad(
            Path("C:/bundle/flow.ad"),
            serial="R58M123",
            session="mobile-run-1",
            parameters={"foo-bar": "one", "foo_bar": "two"},
        )
    assert fake_runner.argv == []


def test_replay_extracts_isolated_state_dir_from_pinned_response(
    fake_runner: FakeRunner, executable: Path, tmp_path: Path
) -> None:
    state_dir = (tmp_path / "replay-state").resolve()
    state_dir.mkdir()
    fake_runner.json_result = {
        "success": True,
        "data": {
            "sessionActive": True,
            "hint": (
                "Pass --state-dir '"
                + str(state_dir)
                + "' --session mobile-run-1 on the next command."
            ),
        },
    }
    result = AgentDeviceRuntime(executable=executable, runner=fake_runner).replay_ad(
        Path("C:/bundle/flow.ad"),
        serial="R58M123",
        session="mobile-run-1",
        parameters={},
    )
    assert result["_state_dir"] == str(state_dir)


def test_replay_reuses_explicit_state_dir_without_response_hint(
    fake_runner: FakeRunner, executable: Path, tmp_path: Path
) -> None:
    state_dir = (tmp_path / "replay-state").resolve()
    state_dir.mkdir()
    fake_runner.json_result = {
        "success": True,
        "data": {"sessionActive": True},
    }
    result = AgentDeviceRuntime(executable=executable, runner=fake_runner).replay_ad(
        Path("C:/bundle/flow.ad"),
        serial="R58M123",
        session="mobile-run-1",
        parameters={},
        state_dir=state_dir,
    )
    assert result["_state_dir"] == str(state_dir)
    assert fake_runner.argv[-3:] == ["--state-dir", str(state_dir), "--json"]


def test_active_replay_rejects_missing_state_dir_hint(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.json_result = {"success": True, "data": {"sessionActive": True}}
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    with pytest.raises(RuntimeFailure, match="agent_device_invalid_state_dir"):
        runtime.replay_ad(
            Path("C:/bundle/flow.ad"),
            serial="R58M123",
            session="mobile-run-1",
            parameters={},
        )


def test_wait_and_close_use_closed_command_shapes(
    fake_runner: FakeRunner, executable: Path, tmp_path: Path
) -> None:
    fake_runner.json_result = {"status": "visible"}
    state_dir = (tmp_path / "replay-state").resolve()
    state_dir.mkdir()
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    runtime.wait_selector(
        "text=Settings",
        serial="R58M123",
        session="mobile-run-1",
        timeout_seconds=2.5,
        state_dir=state_dir,
    )
    assert fake_runner.argv == [
        str(executable),
        "wait",
        "text=Settings",
        "2500",
        "--platform",
        "android",
        "--serial",
        "R58M123",
        "--session",
        "mobile-run-1",
        "--state-dir",
        str(state_dir),
        "--json",
    ]
    runtime.close_session(
        "mobile-run-1", serial="R58M123", state_dir=state_dir
    )
    assert fake_runner.argv == [
        str(executable),
        "close",
        "--platform",
        "android",
        "--serial",
        "R58M123",
        "--session",
        "mobile-run-1",
        "--state-dir",
        str(state_dir),
        "--json",
    ]


def test_close_is_idempotent_when_upstream_reports_session_not_found(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.returncode = 1
    fake_runner.json_result = {
        "success": False,
        "error": {"code": "SESSION_NOT_FOUND", "message": "No active session"},
    }
    AgentDeviceRuntime(executable=executable, runner=fake_runner).close_session(
        "mobile-run-1", serial="R58M123"
    )


def test_close_does_not_hide_other_upstream_failures(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.returncode = 1
    fake_runner.json_result = {
        "success": False,
        "error": {"code": "DEVICE_IN_USE", "message": "Device is claimed"},
    }
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    with pytest.raises(RuntimeFailure, match="DEVICE_IN_USE"):
        runtime.close_session("mobile-run-1", serial="R58M123")


def test_success_false_envelope_is_rejected_even_with_zero_exit_code(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.json_result = {
        "success": False,
        "error": {"code": "SELECTOR_NOT_FOUND", "message": "No match"},
    }
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    with pytest.raises(RuntimeFailure, match="^selector_not_found$"):
        runtime.wait_selector(
            "text=Settings",
            serial="R58M123",
            session="mobile-run-1",
            timeout_seconds=2.5,
        )


def test_wait_timeout_command_failed_normalizes_to_selector_not_found(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.returncode = 1
    fake_runner.json_result = {
        "success": False,
        "error": {
            "code": "COMMAND_FAILED",
            "message": 'wait timed out for selector: role="textview" label="missing"',
        },
    }
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    with pytest.raises(RuntimeFailure, match="^selector_not_found$"):
        runtime.wait_selector(
            'role="textview" label="missing"',
            serial="R58M123",
            session="mobile-run-1",
            timeout_seconds=1,
        )


def test_wait_other_command_failed_remains_runtime_failure(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.returncode = 1
    fake_runner.json_result = {
        "success": False,
        "error": {"code": "COMMAND_FAILED", "message": "snapshot helper crashed"},
    }
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    with pytest.raises(RuntimeFailure, match="agent_device_command_failed"):
        runtime.wait_selector(
            "text=Settings",
            serial="R58M123",
            session="mobile-run-1",
            timeout_seconds=1,
        )


def test_replay_divergence_uses_stable_failure_code(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.returncode = 1
    fake_runner.json_result = {
        "success": False,
        "error": {"code": "REPLAY_DIVERGENCE", "message": "Target missing"},
    }
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    with pytest.raises(RuntimeFailure, match="^replay_diverged"):
        runtime.replay_ad(
            Path("C:/bundle/flow.ad"),
            serial="R58M123",
            session="mobile-run-1",
            parameters={},
        )


def test_failed_command_redacts_bounded_stderr(
    fake_runner: FakeRunner, executable: Path
) -> None:
    fake_runner.returncode = 9
    fake_runner.stderr = "password=secret token=abc123 " + "x" * 5000
    runtime = AgentDeviceRuntime(executable=executable, runner=fake_runner)
    with pytest.raises(RuntimeFailure) as caught:
        runtime.close_session("mobile-run-1", serial="R58M123")
    message = str(caught.value)
    assert "agent_device_command_failed" in message
    assert "upstream_code=9" in message
    assert "secret" not in message and "abc123" not in message
    assert len(message) < 1200


def test_run_process_removes_node_injection_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setenv("NODE_OPTIONS", "--require malicious.js")
    monkeypatch.setenv("NODE_PATH", "C:/malicious")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert run_process(["agent-device", "--version"], timeout_seconds=1).stdout == "ok"
    assert "NODE_OPTIONS" not in captured["env"]
    assert "NODE_PATH" not in captured["env"]
    assert captured["shell"] is False


def test_run_process_rejects_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeFailure, match="agent_device_timeout"):
        run_process(["agent-device", "devices"], timeout_seconds=1)


def test_run_process_rejects_output_over_one_mib(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "x" * (1024 * 1024 + 1), "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeFailure, match="agent_device_output_too_large"):
        run_process(["agent-device", "devices"], timeout_seconds=1)
