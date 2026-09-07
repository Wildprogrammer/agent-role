from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest
from PIL import Image

from agent_workflow_hub.desktop_client_automation import cli
from agent_workflow_hub.desktop_client_automation.airtest_runtime import RuntimeFailure


def test_doctor_parser_accepts_cua_and_preserves_computer_use_alias() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        [
            "doctor",
            "--host",
            "hermes",
            "--mode",
            "explore",
            "--cua-mcp",
            "available",
            "--native-computer-use",
            "unavailable",
        ]
    )
    legacy = parser.parse_args(
        ["doctor", "--host", "codex", "--computer-use", "available"]
    )

    assert args.mode == "explore"
    assert args.cua_mcp == "available"
    assert args.native_computer_use == "unavailable"
    assert legacy.native_computer_use == "available"


def test_doctor_reports_selected_cua_and_airtest_backends(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        cli,
        "probe_cua_driver",
        lambda: {
            "status": "ready",
            "path": "C:/Tools/cua-driver.exe",
            "version": "0.23.2",
            "health": {
                "schema_version": "1",
                "overall": "ok",
                "checks": [],
            },
            "error": None,
        },
    )
    airtest = {
        "ready": True,
        "versions": dict(cli.AIRTEST_LOCKED_VERSIONS),
        "interpreter": "C:/runtime/Scripts/python.exe",
        "expected_interpreter": "C:/runtime/Scripts/python.exe",
        "python_version": "3.11",
        "pointer_bits": 64,
    }
    monkeypatch.setattr(cli, "_airtest_runtime_status", lambda _: airtest)
    args = Namespace(
        host="hermes",
        mode="full",
        cua_mcp="available",
        native_computer_use="unavailable",
    )

    assert cli._doctor(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready"
    assert result["exploration_backend"] == "cua-driver-mcp"
    assert result["replay_backend"] == "airtest"
    assert result["cua"]["version"] == "0.23.2"


def test_airtest_status_requires_the_private_locked_runtime(tmp_path: Path) -> None:
    expected = (tmp_path / "runtime" / "Scripts" / "python.exe").resolve()
    versions = dict(cli.AIRTEST_LOCKED_VERSIONS)

    ready = cli._airtest_runtime_status(
        "Windows",
        versions=versions,
        executable=expected,
        expected_executable=expected,
        python_version=(3, 11),
        pointer_bits=64,
    )
    wrong_dependency = cli._airtest_runtime_status(
        "Windows",
        versions={**versions, "pywinauto": "0.6.9"},
        executable=expected,
        expected_executable=expected,
        python_version=(3, 11),
        pointer_bits=64,
    )
    wrong_interpreter = cli._airtest_runtime_status(
        "Windows",
        versions=versions,
        executable=(tmp_path / "python.exe").resolve(),
        expected_executable=expected,
        python_version=(3, 13),
        pointer_bits=64,
    )

    assert ready["ready"] is True
    assert wrong_dependency["ready"] is False
    assert wrong_interpreter["ready"] is False


def test_replay_reports_macos_backend_gap_before_running_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bundle = (tmp_path / "demo.air").resolve()
    bundle.mkdir()
    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        cli,
        "run_bundle",
        lambda _: pytest.fail("Windows Airtest runtime must not run on macOS"),
    )

    assert cli._replay(Namespace(bundle=str(bundle))) == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": "needs-replay-backend",
        "platform": "Darwin",
    }


def test_capture_uses_shared_uia_window_enumerator(
    tmp_path: Path, monkeypatch,
) -> None:
    recorded: dict[str, object] = {}
    events: list[str] = []

    class Window:
        def set_focus(self) -> None:
            events.append("focus")

        def capture_as_image(self) -> Image.Image:
            events.append("capture")
            return Image.new("RGB", (16, 12), "green")

        def window_text(self) -> str:
            return "Calculator"

        def is_active(self) -> bool:
            return True

    def windows(**criteria: object) -> list[Window]:
        recorded.update(criteria)
        return [Window()]

    monkeypatch.setattr(cli, "_uia_windows", windows)
    output = (tmp_path / "capture.png").resolve()
    args = Namespace(
        output=str(output),
        window_title_regex="^Calculator$",
        x=None,
        y=None,
        width=None,
        height=None,
    )

    assert cli._capture(args) == 0
    assert recorded == {"title_re": "^Calculator$", "visible_only": True}
    assert events == ["focus", "capture"]
    assert output.is_file()
    with Image.open(output) as image:
        assert image.size == (16, 12)


def test_capture_rejects_window_that_did_not_become_foreground(
    tmp_path: Path, monkeypatch,
) -> None:
    events: list[str] = []

    class Window:
        def set_focus(self) -> None:
            events.append("focus")

        def is_active(self) -> bool:
            return False

        def capture_as_image(self) -> Image.Image:
            pytest.fail("an occluded window must not be captured")

    monkeypatch.setattr(cli, "_uia_windows", lambda **criteria: [Window()])
    args = Namespace(
        output=str((tmp_path / "capture.png").resolve()),
        window_title_regex="^Calculator$",
        x=None,
        y=None,
        width=None,
        height=None,
    )

    with pytest.raises(RuntimeFailure, match="foreground"):
        cli._capture(args)

    assert events == ["focus"]


def test_parser_exposes_one_shot_image_commands() -> None:
    parser = cli.build_parser()

    locate = parser.parse_args(
        [
            "locate-image",
            "--window-title-regex",
            "BambuStudio",
            "--template",
            "C:/templates/online-model.png",
        ]
    )
    click = parser.parse_args(
        [
            "click-image",
            "--window-title-regex",
            "BambuStudio",
            "--template",
            "C:/templates/favorite.png",
            "--evidence-dir",
            "C:/evidence",
            "--action-id",
            "open-favorite",
        ]
    )

    assert locate.handler is cli._locate_image
    assert locate.threshold == 0.8
    assert locate.timeout_seconds == 10.0
    assert click.handler is cli._click_image
    assert click.threshold == 0.8
    assert click.timeout_seconds == 10.0


def test_locate_image_cli_prints_result_and_maps_status_to_exit_code(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    template = (tmp_path / "target.png").resolve()
    recorded: dict[str, object] = {}

    def locate_image(**kwargs: object) -> dict[str, object]:
        recorded.update(kwargs)
        return {
            "status": "not-found",
            "window": "BambuStudio",
            "template": str(template),
            "threshold": 0.92,
            "position": None,
        }

    monkeypatch.setattr(cli, "locate_image", locate_image)
    args = Namespace(
        window_title_regex="BambuStudio",
        template=str(template),
        threshold=0.92,
        timeout_seconds=4.0,
    )

    assert cli._locate_image(args) == 1
    assert recorded == {
        "window_title_regex": "BambuStudio",
        "template_path": template,
        "threshold": 0.92,
        "timeout_seconds": 4.0,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "not-found"


def test_click_image_cli_prints_clicked_result(
    tmp_path: Path, monkeypatch, capsys,
) -> None:
    template = (tmp_path / "target.png").resolve()
    evidence_dir = (tmp_path / "evidence").resolve()
    recorded: dict[str, object] = {}

    def click_image(**kwargs: object) -> dict[str, object]:
        recorded.update(kwargs)
        return {
            "status": "clicked",
            "window": "BambuStudio",
            "template": str(template),
            "threshold": 0.85,
            "position": [100, 200],
            "before": str(evidence_dir / "open-favorite-before.png"),
            "after": str(evidence_dir / "open-favorite-after.png"),
        }

    monkeypatch.setattr(cli, "click_image", click_image)
    args = Namespace(
        window_title_regex="BambuStudio",
        template=str(template),
        evidence_dir=str(evidence_dir),
        action_id="open-favorite",
        threshold=0.85,
        timeout_seconds=6.0,
    )

    assert cli._click_image(args) == 0
    assert recorded == {
        "window_title_regex": "BambuStudio",
        "template_path": template,
        "evidence_dir": evidence_dir,
        "action_id": "open-favorite",
        "threshold": 0.85,
        "timeout_seconds": 6.0,
    }
    assert json.loads(capsys.readouterr().out)["status"] == "clicked"
