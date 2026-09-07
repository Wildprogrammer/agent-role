from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from agent_workflow_hub.desktop_client_automation import airtest_runtime
from agent_workflow_hub.desktop_client_automation.airtest_runtime import RuntimeFailure, _resolve_text


def test_resolve_text_uses_named_parameters() -> None:
    assert _resolve_text("order-${order_id}", {"order_id": "123"}) == "order-123"


def test_resolve_text_rejects_missing_parameter() -> None:
    with pytest.raises(RuntimeFailure, match="missing runtime parameter"):
        _resolve_text("${missing}", {})


@pytest.mark.parametrize("method_name", ["send_keys", "SendKeys"])
def test_hotkey_supports_current_and_pywinauto_063_keyboard_apis(
    method_name: str,
) -> None:
    recorded: list[tuple[str, float]] = []

    class Keyboard:
        pass

    keyboard = Keyboard()
    setattr(
        keyboard,
        method_name,
        lambda keys, pause: recorded.append((keys, pause)),
    )
    airtest_runtime._hotkey({"keyboard": keyboard}, ["CTRL", "L"])

    assert recorded == [("^l", 0.02)]


def test_launch_uses_separate_launcher_when_declared(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    launcher = tmp_path / "launcher.exe"
    launcher.write_bytes(b"")
    recorded: list[list[str]] = []

    monkeypatch.setattr(
        airtest_runtime.subprocess,
        "Popen",
        lambda command: recorded.append(command) or object(),
    )
    airtest_runtime._launch({
        "alias": "demo",
        "executable": str(tmp_path / "app.exe"),
        "launch_executable": str(launcher),
        "launch_args": ["--demo"],
    })

    assert recorded == [[str(launcher), "--demo"]]


def test_optional_step_only_suppresses_missing_target(monkeypatch: pytest.MonkeyPatch) -> None:
    class MissingTarget(Exception):
        pass

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected runtime failure")

    monkeypatch.setattr(airtest_runtime, "_run_step", fail)
    manifest = {
        "steps": [{"id": "optional", "optional": True, "retries": 0}]
    }
    with pytest.raises(RuntimeError, match="unexpected runtime failure"):
        airtest_runtime._run_steps(
            manifest, None, {}, {}, {"TargetNotFoundError": MissingTarget}  # type: ignore[arg-type]
        )


def test_optional_step_retries_before_skipping_missing_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingTarget(Exception):
        pass

    attempts = 0

    def miss(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        attempts += 1
        raise MissingTarget

    monkeypatch.setattr(airtest_runtime, "_run_step", miss)
    manifest = {
        "steps": [{"id": "optional", "optional": True, "retries": 2}]
    }
    airtest_runtime._run_steps(
        manifest, None, {}, {}, {"TargetNotFoundError": MissingTarget}  # type: ignore[arg-type]
    )
    assert attempts == 3


def test_uia_windows_uses_pywinauto_063_supported_enumerator() -> None:
    recorded: dict[str, object] = {}
    elements = [object(), object()]

    def find_elements(**criteria: object) -> list[object]:
        recorded.update(criteria)
        return elements

    class Window:
        def __init__(self, element: object) -> None:
            self.element = element

    windows = airtest_runtime._uia_windows(
        _find_elements=find_elements,
        _wrapper_class=Window,
        title_re="^Demo$",
        visible_only=True,
    )

    assert [window.element for window in windows] == elements
    assert recorded == {
        "backend": "uia",
        "top_level_only": True,
        "title_re": "^Demo$",
        "visible_only": True,
    }


def test_stop_unique_instance_refuses_to_kill_when_uia_discovery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class TimeoutExpired(Exception):
        pass

    class Process:
        pid = 42

        def wait(self, timeout: float) -> None:
            raise TimeoutExpired

        def kill(self) -> None:
            events.append("kill")

    class Psutil:
        pass

    Psutil.TimeoutExpired = TimeoutExpired
    monkeypatch.setattr(
        airtest_runtime,
        "_matching_processes",
        lambda psutil, executable: [Process()],
    )
    monkeypatch.setattr(
        airtest_runtime,
        "_uia_windows",
        lambda **criteria: (_ for _ in ()).throw(RuntimeError("UIA unavailable")),
    )

    with pytest.raises(RuntimeFailure, match="window discovery failed"):
        airtest_runtime._stop_unique_instance(
            {
                "alias": "demo",
                "executable": "C:\\Apps\\demo.exe",
                "shutdown_timeout_seconds": 1,
                "force_terminate": True,
            },
            {"psutil": Psutil},
        )

    assert events == []


def test_find_window_limits_application_match_to_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, object] = {}

    class Window:
        handle = 123

    def windows(**criteria: object) -> list[Window]:
        recorded.update(criteria)
        return [Window()]

    monkeypatch.setattr(airtest_runtime, "_uia_windows", windows)

    app = {
        "alias": "demo",
        "window_title_regex": "^Demo$",
        "startup_timeout_seconds": 1,
    }
    window = airtest_runtime._find_window(app, {}, pid=42)
    assert window.handle == 123
    assert recorded["process"] == 42


def test_find_window_allows_multiple_app_processes_when_window_is_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Window:
        def __init__(self, handle: int, pid: int) -> None:
            self.handle = handle
            self._pid = pid

        def process_id(self) -> int:
            return self._pid

    monkeypatch.setattr(
        airtest_runtime,
        "_uia_windows",
        lambda **criteria: [Window(123, 42), Window(456, 99)],
    )

    app = {
        "alias": "demo",
        "window_title_regex": "^Demo$",
        "startup_timeout_seconds": 1,
    }
    window = airtest_runtime._find_window(app, {}, pids={42})
    assert window.handle == 123


def test_prepare_apps_uses_unique_window_across_multiple_reuse_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    class Window:
        handle = 123

    recorded: dict[str, object] = {}
    monkeypatch.setattr(
        airtest_runtime,
        "_matching_processes",
        lambda psutil, executable: [Process(42), Process(99)],
    )
    monkeypatch.setattr(
        airtest_runtime,
        "_wait_processes",
        lambda app, deps: [Process(42), Process(99)],
    )

    def find_window(
        app: dict[str, object], deps: dict[str, object],
        pid: int | None = None, pids: set[int] | None = None,
    ) -> Window:
        recorded["pid"] = pid
        recorded["pids"] = pids
        return Window()

    monkeypatch.setattr(airtest_runtime, "_find_window", find_window)
    initialized: list[tuple[str, str]] = []
    manifest = {
        "apps": [{
            "alias": "explorer",
            "target_kind": "application",
            "lifecycle": "reuse",
            "executable": "C:\\Windows\\explorer.exe",
        }]
    }
    indexes = airtest_runtime._prepare_apps(
        manifest,
        {
            "psutil": object(),
            "init_device": lambda *, platform, uuid: initialized.append((platform, uuid)),
        },
    )

    assert recorded == {"pid": None, "pids": {42, 99}}
    assert initialized == [("Windows", "123")]
    assert indexes == {"explorer": 0}


def test_prepare_apps_can_bind_window_host_separately_from_lifecycle_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    class Window:
        handle = 456

    lifecycle_process = Process(42)
    window_host = Process(99)
    monkeypatch.setattr(
        airtest_runtime,
        "_matching_processes",
        lambda psutil, executable: [lifecycle_process],
    )

    def wait_processes(
        app: dict[str, object], deps: dict[str, object],
        executable: str | None = None,
    ) -> list[Process]:
        return [window_host] if executable else [lifecycle_process]

    monkeypatch.setattr(airtest_runtime, "_wait_processes", wait_processes)
    recorded: dict[str, object] = {}

    def find_window(
        app: dict[str, object], deps: dict[str, object],
        pid: int | None = None, pids: set[int] | None = None,
    ) -> Window:
        recorded["pid"] = pid
        recorded["pids"] = pids
        return Window()

    monkeypatch.setattr(airtest_runtime, "_find_window", find_window)
    manifest = {
        "apps": [{
            "alias": "calculator",
            "target_kind": "application",
            "lifecycle": "reuse",
            "executable": "C:\\Apps\\CalculatorApp.exe",
            "window_process_executable": "C:\\Windows\\System32\\ApplicationFrameHost.exe",
        }]
    }
    initialized: list[tuple[str, str]] = []
    indexes = airtest_runtime._prepare_apps(
        manifest,
        {
            "psutil": object(),
            "init_device": lambda *, platform, uuid: initialized.append((platform, uuid)),
        },
    )

    assert recorded == {"pid": None, "pids": {99}}
    assert initialized == [("Windows", "456")]
    assert indexes == {"calculator": 0}


def test_activate_switches_airtest_device_and_focuses_bound_window() -> None:
    events: list[tuple[str, object]] = []

    class Window:
        def set_focus(self) -> None:
            events.append(("focus", "explorer"))

        def is_active(self) -> bool:
            return True

    airtest_runtime._activate(
        {
            "set_current": lambda index: events.append(("device", index)),
            "windows": {"explorer": Window()},
        },
        {"explorer": 2},
        "explorer",
    )

    assert events == [("device", 2), ("focus", "explorer")]


def test_activate_uses_foreground_verifying_focus_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class Window:
        pass

    monkeypatch.setattr(
        airtest_runtime,
        "_focus_window",
        lambda window: events.append(("verified-focus", window)),
    )
    window = Window()
    airtest_runtime._activate(
        {
            "set_current": lambda index: events.append(("device", index)),
            "windows": {"bambu": window},
        },
        {"bambu": 0},
        "bambu",
    )

    assert events == [("device", 0), ("verified-focus", window)]


def test_run_bundle_saves_success_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    bundle = tmp_path / "demo.air"
    bundle.mkdir()
    manifest = {
        "parameters": [],
        "steps": [],
        "success_assertion": {},
        "aggregate_effect": "read",
    }

    def snapshot(*, filename: str) -> None:
        Path(filename).write_bytes(b"png")

    dependencies = {"win32clipboard": object(), "snapshot": snapshot}
    monkeypatch.setattr(airtest_runtime, "_read_json", lambda path, label: manifest)
    monkeypatch.setattr(airtest_runtime, "_dependencies", lambda: dependencies)
    monkeypatch.setattr(airtest_runtime, "_parameters", lambda manifest, bundle: {})
    monkeypatch.setattr(airtest_runtime, "_prepare_apps", lambda manifest, deps: {})
    monkeypatch.setattr(
        airtest_runtime, "_preserve_text_clipboard", lambda clipboard: nullcontext()
    )
    monkeypatch.setattr(
        airtest_runtime, "_run_steps", lambda manifest, bundle, parameters, indexes, deps: None
    )
    monkeypatch.setattr(
        airtest_runtime, "_assert_success", lambda assertion, bundle, indexes, deps: None
    )

    assert airtest_runtime.run_bundle(bundle) == 0
    evidence = bundle / "replay-success.png"
    result = json.loads((bundle / "run-result.json").read_text(encoding="utf-8"))
    saved_manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert evidence.read_bytes() == b"png"
    assert result["status"] == "replay-verified"
    assert result["evidence"] == str(evidence.resolve())
    assert saved_manifest["status"] == "replay-verified"
