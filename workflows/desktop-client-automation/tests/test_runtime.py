from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

from agent_workflow_hub.desktop_client_automation import airtest_runtime
from agent_workflow_hub.desktop_client_automation.airtest_runtime import RuntimeFailure, _resolve_text


@dataclass(frozen=True)
class RuntimeBundleFixture:
    path: Path
    plan_sha: str


_composition_fixtures = import_module(
    "workflows.desktop-client-automation.tests.composition_fixtures"
)
desktop_catalog = _composition_fixtures.desktop_catalog


@pytest.fixture
def runtime_bundle(desktop_catalog: object, tmp_path: Path) -> RuntimeBundleFixture:
    import yaml

    from agent_workflow_hub.desktop_client_automation.composition import compose_bundle
    from agent_workflow_hub.desktop_client_automation.composition_contracts import (
        load_composition_request,
    )

    component_path = desktop_catalog.component_path
    component = yaml.safe_load(component_path.read_text(encoding="utf-8"))
    component["already_complete"] = {
        "kind": "image-visible",
        "template": "results-state.png",
        "threshold": 0.85,
        "timeout_seconds": 1,
        "stable_seconds": 0.01,
    }
    component["recovery"] = {
        "steps": [{"action": "click-image", "template": "search-button.png"}],
        "postcondition": {
            "kind": "image-visible",
            "template": "favorites-state.png",
            "threshold": 0.85,
            "timeout_seconds": 1,
            "stable_seconds": 0.01,
        },
    }
    component_path.write_text(
        yaml.safe_dump(component, sort_keys=False), encoding="utf-8"
    )
    scenario = yaml.safe_load(desktop_catalog.scenario_path.read_text(encoding="utf-8"))
    scenario["parameters"][0]["default"] = "sample"
    desktop_catalog.scenario_path.write_text(
        yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8"
    )
    bundle = compose_bundle(
        load_composition_request(desktop_catalog.request_path),
        (tmp_path / "runtime-output").resolve(),
    )
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    return RuntimeBundleFixture(bundle, manifest["plan_sha256"])


def _runtime_dependencies(exists: object) -> dict[str, object]:
    class TargetNotFoundError(Exception):
        pass

    touch = Mock()

    def snapshot(*, filename: str) -> None:
        Path(filename).write_bytes(b"png")

    return {
        "TargetNotFoundError": TargetNotFoundError,
        "Template": lambda path, threshold: (path, threshold),
        "exists": exists,
        "wait": lambda target, timeout: (10, 10),
        "touch": touch,
        "text": Mock(),
        "keyboard": Mock(send_keys=Mock()),
        "mouse": Mock(),
        "swipe": Mock(),
        "set_current": Mock(),
        "windows": {},
        "desktop_aliases": set(),
        "win32clipboard": object(),
        "snapshot": snapshot,
    }


def _patch_composed_runtime(
    monkeypatch: pytest.MonkeyPatch,
    dependencies: dict[str, object],
) -> None:
    monkeypatch.setattr(airtest_runtime, "_dependencies", lambda: dependencies)
    monkeypatch.setattr(
        airtest_runtime,
        "_prepare_apps",
        lambda manifest, deps: {"example-app": 0},
    )
    monkeypatch.setattr(
        airtest_runtime,
        "_preserve_text_clipboard",
        lambda clipboard: nullcontext(),
    )
    monkeypatch.setattr(
        airtest_runtime,
        "_assert_success",
        lambda assertion, bundle, indexes, deps: None,
    )


def test_already_complete_uses_fresh_match_and_skips_component_action(
    runtime_bundle: RuntimeBundleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exists = Mock(return_value=(10, 10))
    dependencies = _runtime_dependencies(exists)
    _patch_composed_runtime(monkeypatch, dependencies)

    code = airtest_runtime.run_bundle(
        runtime_bundle.path,
        confirmed_plan_sha256=runtime_bundle.plan_sha,
    )

    assert code == 0
    dependencies["touch"].assert_not_called()
    result = json.loads(
        (runtime_bundle.path / "run-result.json").read_text(encoding="utf-8")
    )
    assert result["components"][0]["status"] == "already-complete"


def test_failed_precondition_runs_recovery_once_then_stops(
    runtime_bundle: RuntimeBundleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exists = Mock(side_effect=[False, False, True, True, False])
    dependencies = _runtime_dependencies(exists)
    _patch_composed_runtime(monkeypatch, dependencies)

    code = airtest_runtime.run_bundle(
        runtime_bundle.path,
        confirmed_plan_sha256=runtime_bundle.plan_sha,
    )

    assert code == 1
    assert dependencies["touch"].call_count == 1
    result = (runtime_bundle.path / "run-result.json").read_text(encoding="utf-8")
    assert "component_precondition_failed" in result
    parsed = json.loads(result)
    assert parsed["components"][0]["status"] == "failed"
    assert parsed["components"][0]["error"] == "component_precondition_failed"


def test_ambiguous_component_image_never_calls_coordinate_or_cua(
    runtime_bundle: RuntimeBundleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exists = Mock(side_effect=RuntimeFailure("ambiguous-match: image_not_unique"))
    dependencies = _runtime_dependencies(exists)
    _patch_composed_runtime(monkeypatch, dependencies)

    assert (
        airtest_runtime.run_bundle(
            runtime_bundle.path,
            confirmed_plan_sha256=runtime_bundle.plan_sha,
        )
        == 1
    )
    dependencies["touch"].assert_not_called()
    result = json.loads(
        (runtime_bundle.path / "run-result.json").read_text(encoding="utf-8")
    )
    assert result["components"][0]["occurrence_id"] == "c001-search-model"
    assert result["components"][0]["phase"] == "check-already-complete"
    assert "ambiguous-match" in result["components"][0]["error"]


def test_component_asset_drift_fails_before_application_preparation(
    runtime_bundle: RuntimeBundleFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = json.loads(
        (runtime_bundle.path / "manifest.json").read_text(encoding="utf-8")
    )
    asset = runtime_bundle.path / manifest["assets"][0]["path"]
    asset.write_bytes(asset.read_bytes() + b"drift")
    prepared = Mock()
    monkeypatch.setattr(airtest_runtime, "_prepare_apps", prepared)

    assert (
        airtest_runtime.run_bundle(
            runtime_bundle.path,
            confirmed_plan_sha256=runtime_bundle.plan_sha,
        )
        == 1
    )
    prepared.assert_not_called()
    assert "component_integrity_mismatch" in (
        runtime_bundle.path / "run-result.json"
    ).read_text(encoding="utf-8")


def test_side_effecting_composed_bundle_requires_exact_plan_confirmation(
    desktop_catalog: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    from agent_workflow_hub.desktop_client_automation.composition import compose_bundle
    from agent_workflow_hub.desktop_client_automation.composition_contracts import (
        load_composition_request,
    )

    component = yaml.safe_load(
        desktop_catalog.component_path.read_text(encoding="utf-8")
    )
    component["effect"] = "create"
    desktop_catalog.component_path.write_text(
        yaml.safe_dump(component, sort_keys=False), encoding="utf-8"
    )
    bundle = compose_bundle(
        load_composition_request(desktop_catalog.request_path),
        (tmp_path / "confirmation-output").resolve(),
    )
    initialized = Mock()
    monkeypatch.setattr(airtest_runtime, "_dependencies", initialized)

    assert airtest_runtime.run_bundle(bundle) == 1
    initialized.assert_not_called()
    assert "confirmed_plan_sha256_required" in (
        bundle / "run-result.json"
    ).read_text(encoding="utf-8")


def test_explicit_parameter_file_takes_precedence_over_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = (tmp_path / "environment.json").resolve()
    explicit = (tmp_path / "explicit.json").resolve()
    environment.write_text('{"term":"environment"}', encoding="utf-8")
    explicit.write_text('{"term":"explicit"}', encoding="utf-8")
    monkeypatch.setenv("DESKTOP_AUTOMATION_PARAMETERS", str(environment))

    values = airtest_runtime._parameters(
        {
            "parameters": [
                {
                    "name": "term",
                    "kind": "text",
                    "default": "default",
                    "sensitive": False,
                }
            ]
        },
        tmp_path,
        explicit,
    )

    assert values == {"term": "explicit"}


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


def test_prepare_apps_connects_primary_desktop_without_window_handle() -> None:
    initialized: list[dict[str, object]] = []
    dependencies: dict[str, object] = {
        "psutil": object(),
        "init_device": lambda **kwargs: initialized.append(kwargs),
    }
    manifest = {
        "apps": [
            {
                "alias": "desktop",
                "target_kind": "desktop",
                "lifecycle": "attach-only",
                "display_id": "primary",
            }
        ]
    }

    indexes = airtest_runtime._prepare_apps(manifest, dependencies)

    assert indexes == {"desktop": 0}
    assert initialized == [{"platform": "Windows"}]
    assert dependencies["desktop_aliases"] == {"desktop"}
    assert dependencies["windows"] == {}


class FakeWin32Gui:
    def __init__(self, foreground: int, shell_hosts: set[int]) -> None:
        self.foreground = foreground
        self.shell_hosts = shell_hosts

    def GetForegroundWindow(self) -> int:
        return self.foreground

    def GetAncestor(self, hwnd: int, flag: int) -> int:
        assert flag == 2
        return hwnd

    def GetShellWindow(self) -> int:
        return 100

    def EnumWindows(self, callback: object, data: object) -> None:
        for hwnd in sorted(self.shell_hosts):
            callback(hwnd, data)  # type: ignore[operator]

    def FindWindowEx(
        self, hwnd: int, child: int, class_name: str, title: object,
    ) -> int:
        assert child == 0
        assert title is None
        return (
            500
            if hwnd in self.shell_hosts and class_name == "SHELLDLL_DefView"
            else 0
        )


class FakeWin32GuiWithoutGetShellWindow(FakeWin32Gui):
    GetShellWindow = None  # type: ignore[assignment]

    def FindWindow(self, class_name: str, title: object) -> int:
        assert class_name == "Progman"
        assert title is None
        return 100


class FakeWin32GuiWithForegroundGeometry(FakeWin32Gui):
    def __init__(
        self,
        foreground: int,
        shell_hosts: set[int],
        foreground_rect: tuple[int, int, int, int],
    ) -> None:
        super().__init__(foreground, shell_hosts)
        self.foreground_rect = foreground_rect

    def GetDesktopWindow(self) -> int:
        return 1

    def GetWindowRect(self, hwnd: int) -> tuple[int, int, int, int]:
        return (0, 0, 1920, 1080) if hwnd == 1 else self.foreground_rect


def test_desktop_foreground_accepts_shell_host() -> None:
    airtest_runtime._require_desktop_foreground(FakeWin32Gui(100, {100}))


def test_desktop_foreground_accepts_workerw_shell_host() -> None:
    airtest_runtime._require_desktop_foreground(FakeWin32Gui(200, {200}))


def test_desktop_foreground_falls_back_when_get_shell_window_is_unavailable() -> None:
    airtest_runtime._require_desktop_foreground(
        FakeWin32GuiWithoutGetShellWindow(200, {200})
    )


def test_desktop_foreground_accepts_non_occluding_offscreen_helper() -> None:
    airtest_runtime._require_desktop_foreground(
        FakeWin32GuiWithForegroundGeometry(
            300, {100, 200}, (-504, -504, -332, -434)
        )
    )


def test_desktop_foreground_rejects_on_screen_helper() -> None:
    with pytest.raises(RuntimeFailure, match="desktop-not-foreground"):
        airtest_runtime._require_desktop_foreground(
            FakeWin32GuiWithForegroundGeometry(
                300, {100, 200}, (100, 100, 500, 500)
            )
        )


def test_desktop_foreground_rejects_occluding_app() -> None:
    with pytest.raises(RuntimeFailure, match="desktop-not-foreground"):
        airtest_runtime._require_desktop_foreground(FakeWin32Gui(900, {100, 200}))


def test_run_step_checks_desktop_foreground_before_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []
    win32gui = object()

    class Device:
        monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        main_monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}

    monkeypatch.setattr(
        airtest_runtime,
        "_require_desktop_foreground",
        lambda api: events.append(("foreground", api)),
    )
    dependencies = {
        "set_current": lambda index: events.append(("device", index)),
        "windows": {},
        "desktop_aliases": {"desktop"},
        "win32gui": win32gui,
        "device": lambda: Device(),
        "touch": lambda position: events.append(("touch", position)),
    }

    airtest_runtime._run_step(
        {
            "app": "desktop",
            "action": "click-coordinate",
            "x": 10,
            "y": 20,
            "timeout_seconds": 1,
        },
        Path(),
        {},
        {"desktop": 0},
        dependencies,
    )

    assert events == [
        ("device", 0),
        ("foreground", win32gui),
        ("touch", (10, 20)),
    ]


class FakeDesktopDevice:
    monitor = {"left": -1280, "top": 0, "width": 3200, "height": 1080}
    main_monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}

    def __init__(self, shape: tuple[int, int, int] = (1080, 3200, 3)) -> None:
        self.shape = shape

    def snapshot(self) -> object:
        return np.zeros(self.shape, dtype=np.uint8)


class MatchTemplate:
    def __init__(self, matches: list[dict[str, object]]) -> None:
        self.matches = matches

    def match_all_in(self, screen: object) -> list[dict[str, object]]:
        assert getattr(screen, "shape") == (1080, 1920, 3)
        return self.matches


def test_unique_desktop_match_offsets_primary_result_into_virtual_screen() -> None:
    result = airtest_runtime._wait_for_unique_desktop_match(
        {"device": lambda: FakeDesktopDevice()},
        MatchTemplate([{"result": (50, 60), "confidence": 0.99}]),
        timeout=1,
        clock=lambda: 0.0,
        sleeper=lambda _: None,
    )

    assert result == (1330, 60)


def test_unique_desktop_match_times_out_without_a_match() -> None:
    class TargetNotFoundError(Exception):
        pass

    clock = iter([0.0, 2.0]).__next__
    with pytest.raises(TargetNotFoundError, match="desktop template was not found"):
        airtest_runtime._wait_for_unique_desktop_match(
            {
                "device": lambda: FakeDesktopDevice(),
                "TargetNotFoundError": TargetNotFoundError,
            },
            MatchTemplate([]),
            timeout=1,
            clock=clock,
            sleeper=lambda _: None,
        )


def test_unique_desktop_match_rejects_ambiguous_matches() -> None:
    with pytest.raises(RuntimeFailure, match="ambiguous-match"):
        airtest_runtime._wait_for_unique_desktop_match(
            {"device": lambda: FakeDesktopDevice()},
            MatchTemplate([{"result": (10, 20)}, {"result": (30, 40)}]),
            timeout=1,
        )


def test_unique_desktop_match_rejects_display_mismatch() -> None:
    with pytest.raises(RuntimeFailure, match="display-mismatch"):
        airtest_runtime._wait_for_unique_desktop_match(
            {"device": lambda: FakeDesktopDevice((1079, 3200, 3))},
            MatchTemplate([]),
            timeout=1,
        )


def _desktop_step_dependencies(events: list[object]) -> dict[str, object]:
    return {
        "set_current": lambda index: events.append(("device", index)),
        "windows": {},
        "desktop_aliases": {"desktop"},
        "win32gui": object(),
        "device": lambda: FakeDesktopDevice(),
        "Template": lambda path, threshold: (path, threshold),
        "wait": lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("window matcher must not be used for desktop")
        ),
        "touch": lambda position, **kwargs: events.append(
            ("touch", position, kwargs)
        ),
        "swipe": lambda start, end: events.append(("swipe", start, end)),
        "mouse": object(),
    }


def test_desktop_click_coordinate_is_primary_display_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    monkeypatch.setattr(airtest_runtime, "_require_desktop_foreground", lambda _: None)

    airtest_runtime._run_step(
        {
            "app": "desktop",
            "action": "click-coordinate",
            "x": 50,
            "y": 60,
            "timeout_seconds": 1,
        },
        Path(),
        {},
        {"desktop": 0},
        _desktop_step_dependencies(events),
    )

    assert ("touch", (1330, 60), {}) in events


def test_primary_local_coordinate_rejects_display_outside_virtual_desktop() -> None:
    class Device:
        monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        main_monitor = {"left": -100, "top": 0, "width": 1920, "height": 1080}

    with pytest.raises(RuntimeFailure, match="display-mismatch"):
        airtest_runtime._primary_local_to_virtual(Device(), (50, 60))


@pytest.mark.parametrize(
    ("action", "touch_kwargs"),
    [
        ("double-click-image", {"times": 2}),
        ("right-click-image", {"right_click": True}),
    ],
)
def test_desktop_image_click_uses_fresh_unique_match(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    touch_kwargs: dict[str, object],
) -> None:
    events: list[object] = []
    monkeypatch.setattr(airtest_runtime, "_require_desktop_foreground", lambda _: None)
    monkeypatch.setattr(
        airtest_runtime,
        "_wait_for_unique_desktop_match",
        lambda *args, **kwargs: (410, 220),
    )

    airtest_runtime._run_step(
        {
            "app": "desktop",
            "action": action,
            "template": "assets/icon.png",
            "threshold": 0.8,
            "timeout_seconds": 1,
        },
        Path(),
        {},
        {"desktop": 0},
        _desktop_step_dependencies(events),
    )

    assert ("touch", (410, 220), touch_kwargs) in events


def test_desktop_drag_uses_fresh_matches_and_airtest_swipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    positions = iter([(100, 200), (300, 400)])
    monkeypatch.setattr(airtest_runtime, "_require_desktop_foreground", lambda _: None)
    monkeypatch.setattr(
        airtest_runtime,
        "_wait_for_unique_desktop_match",
        lambda *args, **kwargs: next(positions),
    )

    airtest_runtime._run_step(
        {
            "app": "desktop",
            "action": "drag-image",
            "from_template": "assets/from.png",
            "to_template": "assets/to.png",
            "timeout_seconds": 1,
        },
        Path(),
        {},
        {"desktop": 0},
        _desktop_step_dependencies(events),
    )

    assert ("swipe", (100, 200), (300, 400)) in events


def test_desktop_final_assertion_uses_unique_primary_display_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    moments = iter([0.0, 0.1, 0.2, 0.3]).__next__
    monkeypatch.setattr(airtest_runtime.time, "monotonic", moments)
    monkeypatch.setattr(airtest_runtime.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        airtest_runtime,
        "_desktop_match_once",
        lambda deps, target: calls.append(target) or (500, 300),
    )
    dependencies = _desktop_step_dependencies([])
    dependencies["exists"] = lambda target: (_ for _ in ()).throw(
        AssertionError("window exists matcher must not be used for desktop")
    )

    airtest_runtime._assert_success(
        {
            "app": "desktop",
            "template": "assets/window.png",
            "threshold": 0.8,
            "timeout_seconds": 1,
            "stable_seconds": 0.05,
        },
        Path(),
        {"desktop": 0},
        dependencies,
    )

    assert calls == [(str(Path() / "assets/window.png"), 0.8)]


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
