from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path

import pytest

from agent_workflow_hub.mobile_device_automation import cli
from agent_workflow_hub.mobile_device_automation.agent_device_runtime import (
    AgentDeviceProbe,
    AndroidDevice,
)
from agent_workflow_hub.mobile_device_automation.models import ViewportSpec


VIEWPORT = ViewportSpec(width=300, height=500, orientation="portrait", density=2.0)


def command_names(parser: argparse.ArgumentParser) -> set[str]:
    action = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    return set(action.choices)


def test_parser_exposes_complete_entrypoint_set() -> None:
    parser = cli.build_parser()
    assert command_names(parser) == {
        "doctor",
        "devices",
        "capture",
        "locate-image",
        "click-image",
        "compile",
        "compose",
        "preview",
        "replay",
    }


def test_compose_loads_request_and_returns_generated_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    request_path = (tmp_path / "composition.json").resolve()
    request_path.write_text("{}", encoding="utf-8")
    output_root = (tmp_path / "outputs").resolve()
    recorded: dict[str, object] = {}

    def fake_load(path: Path):
        recorded["request_path"] = path
        return "request"

    def fake_compose(request: object, root: Path) -> Path:
        recorded["request"] = request
        recorded["output_root"] = root
        return root / "demo-v001"

    monkeypatch.setattr(cli, "load_composition_request", fake_load)
    monkeypatch.setattr(cli, "compose_bundle", fake_compose)
    monkeypatch.setattr(cli, "preview_bundle", lambda bundle: {"plan_sha256": "a" * 64})
    assert cli.main(
        [
            "compose",
            "--request",
            str(request_path),
            "--output-root",
            str(output_root),
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "status": "generated-unverified",
        "bundle": str(output_root / "demo-v001"),
        "plan_sha256": "a" * 64,
    }
    assert recorded == {
        "request_path": request_path,
        "request": "request",
        "output_root": output_root,
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ["--request", "relative.json", "--output-root", "C:/absolute-output"],
        ["--request", "C:/missing.json", "--output-root", "C:/absolute-output"],
        ["--request", "REQUEST", "--output-root", "relative-output"],
    ],
)
def test_compose_rejects_invalid_paths_without_creating_output(
    tmp_path: Path, arguments: list[str], capsys
) -> None:
    request = (tmp_path / "composition.json").resolve()
    request.write_text("{}", encoding="utf-8")
    resolved = [str(request) if value == "REQUEST" else value for value in arguments]
    assert cli.main(["compose", *resolved]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid"
    assert not (tmp_path / "relative-output").exists()


def test_replay_requires_absolute_existing_bundle(tmp_path: Path, capsys) -> None:
    assert cli.main(["replay", "--bundle", "relative"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "invalid"
    assert captured.err == ""


def test_destructive_replay_forwards_confirmed_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    bundle = (tmp_path / "bundle").resolve()
    bundle.mkdir()
    recorded = {}

    def fake_run_bundle(path, **kwargs):
        recorded["path"] = path
        recorded.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_bundle", fake_run_bundle)
    monkeypatch.setattr(cli, "_make_agent_runtime", lambda: object())
    monkeypatch.setattr(cli, "_make_airtest_runtime", lambda: object())
    assert (
        cli.main(
            [
                "replay",
                "--bundle",
                str(bundle),
                "--confirmed-plan-sha256",
                "a" * 64,
            ]
        )
        == 0
    )
    assert recorded["confirmed_plan_sha256"] == "a" * 64
    assert recorded["path"] == bundle
    assert json.loads(capsys.readouterr().out)["status"] == "replay-verified"


def test_replay_forwards_absolute_parameter_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = (tmp_path / "bundle").resolve()
    bundle.mkdir()
    parameters = (tmp_path / "parameters.yaml").resolve()
    parameters.write_text("query: demo\n", encoding="utf-8")
    recorded = {}

    def fake_run_bundle(path, **kwargs):
        recorded.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_bundle", fake_run_bundle)
    monkeypatch.setattr(cli, "_make_agent_runtime", lambda: object())
    monkeypatch.setattr(cli, "_make_airtest_runtime", lambda: object())
    assert (
        cli.main(
            [
                "replay",
                "--bundle",
                str(bundle),
                "--parameters",
                str(parameters),
            ]
        )
        == 0
    )
    assert recorded["parameters_path"] == parameters


def test_devices_serial_filters_exact_device(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    class Runtime:
        def devices(self):
            return (
                AndroidDevice("one", "One", "physical", None, VIEWPORT),
                AndroidDevice("two", "Two", "physical", None, VIEWPORT),
            )

    monkeypatch.setattr(cli, "_make_agent_runtime", Runtime)
    assert cli.main(["devices", "--serial", "two"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["devices"] == [
        {"serial": "two", "name": "Two", "kind": "physical", "claimed": False}
    ]


def test_capture_requires_complete_crop_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    output = (tmp_path / "crop.png").resolve()
    assert (
        cli.main(
            [
                "capture",
                "--serial",
                "emulator-5554",
                "--output",
                str(output),
                "--x",
                "1",
                "--y",
                "2",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_selected_viewport_falls_back_to_airtest_inspection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AgentRuntime:
        def devices(self):
            return (AndroidDevice("one", "One", "device", None, None),)

    class ImageRuntime:
        def inspect_viewport(self, serial: str):
            assert serial == "one"
            return VIEWPORT

    monkeypatch.setattr(cli, "_make_agent_runtime", AgentRuntime)
    monkeypatch.setattr(cli, "_make_airtest_runtime", ImageRuntime)

    assert cli._selected_viewport("one") == VIEWPORT


def test_capture_forwards_complete_crop_and_produces_one_json_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    output = (tmp_path / "crop.png").resolve()
    recorded = {}

    monkeypatch.setattr(cli, "_selected_viewport", lambda serial: VIEWPORT)

    def fake_capture(**kwargs):
        recorded["capture"] = kwargs
        Path(kwargs["output_path"]).write_bytes(b"screen")
        return {"status": "captured", "path": str(kwargs["output_path"])}

    def fake_crop(**kwargs):
        recorded["crop"] = kwargs
        Path(kwargs["output_path"]).write_bytes(b"crop")
        return {"status": "cropped", "path": str(kwargs["output_path"])}

    monkeypatch.setattr(cli, "capture_device", fake_capture)
    monkeypatch.setattr(cli, "crop_template", fake_crop)
    assert (
        cli.main(
            [
                "capture",
                "--serial",
                "emulator-5554",
                "--output",
                str(output),
                "--x",
                "10",
                "--y",
                "20",
                "--width",
                "30",
                "--height",
                "40",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert len(captured.out.strip().splitlines()) == 1
    assert captured.err == ""
    assert recorded["crop"]["crop_box"] == (10, 20, 30, 40)
    assert recorded["crop"]["source_screenshot"].name == "crop-source.png"


@pytest.mark.parametrize("threshold", ["0", "1.1", "nan", "inf"])
def test_image_commands_reject_invalid_threshold(
    tmp_path: Path, threshold: str, capsys
) -> None:
    template = (tmp_path / "icon.png").resolve()
    template.write_bytes(b"icon")
    assert (
        cli.main(
            [
                "locate-image",
                "--serial",
                "emulator-5554",
                "--template",
                str(template),
                "--threshold",
                threshold,
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["status"] == "invalid"


def test_click_forwards_absolute_exclusive_evidence_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    template = (tmp_path / "icon.png").resolve()
    template.write_bytes(b"icon")
    evidence = (tmp_path / "evidence").resolve()
    recorded = {}
    monkeypatch.setattr(cli, "_selected_viewport", lambda serial: VIEWPORT)

    def fake_click(**kwargs):
        recorded.update(kwargs)
        return {"status": "clicked", "position": [10, 20]}

    monkeypatch.setattr(cli, "click_image", fake_click)
    assert (
        cli.main(
            [
                "click-image",
                "--serial",
                "emulator-5554",
                "--template",
                str(template),
                "--evidence-dir",
                str(evidence),
                "--action-id",
                "open-settings",
            ]
        )
        == 0
    )
    assert recorded["evidence_dir"] == evidence
    assert recorded["action_id"] == "open-settings"
    assert json.loads(capsys.readouterr().out)["status"] == "clicked"


@pytest.mark.parametrize("mode", ["explore", "image", "full"])
def test_doctor_accepts_all_modes(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli, "_node_version", lambda: "22.12.0")
    monkeypatch.setattr(
        cli,
        "probe_agent_device",
        lambda executable: AgentDeviceProbe("ready", "0.21.19", executable),
    )
    monkeypatch.setattr(cli, "_airtest_ready", lambda: True)
    monkeypatch.setattr(
        cli,
        "_ime_helper_status",
        lambda serial: {"status": "installed", "version_code": 21019},
    )

    class Runtime:
        def devices(self):
            return (AndroidDevice("one", "One", "physical", None, VIEWPORT),)

    monkeypatch.setattr(cli, "_make_agent_runtime", Runtime)
    assert cli.main(["doctor", "--mode", mode]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready"
    assert result["ime_helper"] == {"status": "installed", "version_code": 21019}
    assert result["text_input_mode"] == "direct-ime"


@pytest.mark.parametrize("helper_status", ["missing", "wrong-version", "unverified"])
def test_doctor_uses_pinyin_fallback_when_ime_helper_is_unavailable(
    helper_status: str, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli, "_node_version", lambda: "22.12.0")
    monkeypatch.setattr(
        cli,
        "probe_agent_device",
        lambda executable: AgentDeviceProbe("ready", "0.21.19", executable),
    )
    monkeypatch.setattr(cli, "_airtest_ready", lambda: True)
    monkeypatch.setattr(
        cli,
        "_ime_helper_status",
        lambda serial: {"status": helper_status, "version_code": None},
    )

    class Runtime:
        def devices(self):
            return (AndroidDevice("one", "One", "physical", None, VIEWPORT),)

    monkeypatch.setattr(cli, "_make_agent_runtime", Runtime)
    assert cli.main(["doctor", "--mode", "full"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready"
    assert result["ime_helper"]["status"] == helper_status
    assert result["text_input_mode"] == "pinyin-fallback"


def test_completed_replay_failure_maps_to_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    bundle = (tmp_path / "bundle").resolve()
    bundle.mkdir()
    monkeypatch.setattr(cli, "run_bundle", lambda *args, **kwargs: 2)
    monkeypatch.setattr(cli, "_make_agent_runtime", lambda: object())
    monkeypatch.setattr(cli, "_make_airtest_runtime", lambda: object())
    assert cli.main(["replay", "--bundle", str(bundle)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "replay-failed"


def test_launcher_resolves_platform_private_python() -> None:
    launcher = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "mobile_device_automation.py"
    )
    spec = importlib.util.spec_from_file_location("mobile_launcher", launcher)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    root = Path("C:/hub")
    assert module.runtime_python(root, "nt") == root / "workspace/workflows/mobile-device-automation/runtime/Scripts/python.exe"
    assert module.runtime_python(root, "posix") == root / "workspace/workflows/mobile-device-automation/runtime/bin/python"


def test_mobile_runtime_profiles_are_hash_locked() -> None:
    references = Path(__file__).resolve().parents[1] / "references"
    assert (references / "runtime-mobile-py311.in").read_text("utf-8").splitlines() == [
        "airtest==1.4.3",
        "PyYAML==6.0.3",
    ]
    windows = (references / "runtime-mobile-windows-x64-py311.lock").read_text("utf-8")
    assert "airtest==1.4.3" in windows
    assert "pyyaml==6.0.3" in windows.casefold()
    assert "--hash=sha256:" in windows
    for filename in (
        "runtime-mobile-macos-arm64-py311.lock",
        "runtime-mobile-linux-x64-py311.lock",
    ):
        text = (references / filename).read_text("utf-8")
        assert "unverified-runtime-lock" in text
        assert "--hash=sha256:" not in text


def test_windows_runtime_profile_uses_sysconfig_when_machine_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli.platform, "machine", lambda: "")
    monkeypatch.setattr(cli.sysconfig, "get_platform", lambda: "win-amd64")

    assert cli._host_architecture() == "amd64"
