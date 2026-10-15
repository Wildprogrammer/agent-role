from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    from agent_workflow_hub.automation_composition.lifecycle import (
        LifecyclePhase,
        initial_state,
        transition,
    )
except ImportError:  # pragma: no cover - used by a copied standalone bundle
    from _automation_lifecycle import LifecyclePhase, initial_state, transition


class RuntimeFailure(RuntimeError):
    pass


class ComponentRuntimeFailure(RuntimeFailure):
    def __init__(self, message: str, component: dict[str, Any]) -> None:
        super().__init__(message)
        self.components = [component]


DESKTOP_INPUT_ACTIONS = {
    "click-image",
    "double-click-image",
    "right-click-image",
    "click-coordinate",
    "type-text",
    "press-key",
    "hotkey",
    "scroll",
    "drag-image",
    "copy-text",
    "paste-text",
}
GA_ROOT = 2


def _dependencies() -> dict[str, Any]:
    try:
        from airtest import aircv
        import psutil
        import win32clipboard
        import win32gui
        from airtest.core.api import (
            Template,
            device,
            exists,
            init_device,
            set_current,
            snapshot,
            swipe,
            text,
            touch,
            wait,
        )
        from airtest.core.error import TargetNotFoundError
        from pywinauto import keyboard, mouse
    except ImportError as exc:
        raise RuntimeFailure(f"Airtest runtime dependency is unavailable: {exc}") from exc
    return locals()


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeFailure(f"invalid {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeFailure(f"{label} must be a JSON object")
    return value


def _parameters(
    manifest: dict[str, Any],
    bundle: Path,
    parameters_path: Path | None = None,
) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in manifest.get("parameters", []):
        if item.get("sensitive"):
            continue
        default = item.get("default")
        if isinstance(default, str):
            values[item["name"]] = default
    source = (
        str(parameters_path)
        if parameters_path is not None
        else os.environ.get("DESKTOP_AUTOMATION_PARAMETERS")
    )
    if source:
        path = Path(source)
        if not path.is_absolute() or not path.is_file():
            raise RuntimeFailure("DESKTOP_AUTOMATION_PARAMETERS must name an existing absolute JSON file")
        supplied = _read_json(path, "parameters")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in supplied.items()):
            raise RuntimeFailure("all supplied parameters must be string pairs")
        values.update(supplied)
    missing = [
        item["name"]
        for item in manifest.get("parameters", [])
        if not item.get("sensitive") and item["name"] not in values
    ]
    if missing:
        raise RuntimeFailure(f"missing parameters: {', '.join(missing)}")
    return values


def _canonical_plan(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in manifest.items()
        if key
        not in {
            "generated_at",
            "plan_sha256",
            "source_request",
            "status",
        }
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_bundle_integrity(
    bundle: Path,
    manifest: dict[str, Any],
    confirmed_plan_sha256: str | None,
) -> None:
    if "component_provenance" not in manifest:
        return
    expected_plan = manifest.get("plan_sha256")
    if not isinstance(expected_plan, str) or len(expected_plan) != 64:
        raise RuntimeFailure("component_integrity_mismatch: plan digest is missing")
    actual_plan = hashlib.sha256(
        json.dumps(
            _canonical_plan(manifest),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if actual_plan != expected_plan:
        raise RuntimeFailure("component_integrity_mismatch: plan digest changed")

    root = bundle.resolve()
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        raise RuntimeFailure("component_integrity_mismatch: asset inventory is missing")
    for item in assets:
        if not isinstance(item, dict):
            raise RuntimeFailure("component_integrity_mismatch: invalid asset record")
        raw_path = item.get("path")
        if not isinstance(raw_path, str):
            raise RuntimeFailure("component_integrity_mismatch: invalid asset path")
        relative = Path(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeFailure("component_integrity_mismatch: unsafe asset path")
        candidate = root / relative
        current = candidate
        while current != root:
            if current.is_symlink():
                raise RuntimeFailure("component_integrity_mismatch: symlink asset path")
            current = current.parent
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise RuntimeFailure(
                "component_integrity_mismatch: asset is missing or outside bundle"
            ) from exc
        if not resolved.is_file():
            raise RuntimeFailure("component_integrity_mismatch: asset is not a file")
        if resolved.stat().st_size != item.get("size") or _sha256(resolved) != item.get(
            "sha256"
        ):
            raise RuntimeFailure("component_integrity_mismatch: asset content changed")

    if confirmed_plan_sha256 is not None and confirmed_plan_sha256 != expected_plan:
        raise RuntimeFailure("confirmed_plan_sha256_mismatch")
    if manifest.get("aggregate_effect") in {
        "create",
        "submit",
        "send",
        "delete",
        "unknown",
    } and confirmed_plan_sha256 != expected_plan:
        raise RuntimeFailure("confirmed_plan_sha256_required")


def _resolve_text(value: str, parameters: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in parameters:
            raise RuntimeFailure(f"missing runtime parameter: {name}")
        return parameters[name]

    return re.sub(r"\$\{([a-z][a-z0-9_-]{0,63})\}", replace, value)


def _matching_processes(psutil: Any, executable: str) -> list[Any]:
    expected = os.path.normcase(os.path.abspath(executable))
    matches = []
    for process in psutil.process_iter(["pid", "exe"]):
        try:
            actual = process.info.get("exe")
            if actual and os.path.normcase(os.path.abspath(actual)) == expected:
                matches.append(process)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return matches


def _uia_windows(
    *,
    _find_elements: Any = None,
    _wrapper_class: Any = None,
    **criteria: Any,
) -> list[Any]:
    if _find_elements is None or _wrapper_class is None:
        try:
            from pywinauto import findwindows
            from pywinauto.backend import registry
        except ImportError as exc:
            raise RuntimeFailure("pywinauto is required for Windows window discovery") from exc
        _find_elements = findwindows.find_elements
        _wrapper_class = registry.backends["uia"].generic_wrapper_class
    criteria.setdefault("backend", "uia")
    criteria.setdefault("top_level_only", True)
    return [_wrapper_class(element) for element in _find_elements(**criteria)]


def _require_foreground(window: Any) -> None:
    is_active = getattr(window, "is_active", None)
    if not callable(is_active) or not is_active():
        raise RuntimeFailure(
            "target window did not become foreground; remove occluding windows and retry"
        )


def _focus_window(window: Any) -> None:
    window.set_focus()
    _require_foreground(window)


def _desktop_shell_hosts(win32gui: Any) -> set[int]:
    hosts: set[int] = set()
    get_shell_window = getattr(win32gui, "GetShellWindow", None)
    if callable(get_shell_window):
        shell = int(get_shell_window() or 0)
    else:
        find_window = getattr(win32gui, "FindWindow", None)
        shell = (
            int(find_window("Progman", None) or 0)
            if callable(find_window)
            else 0
        )
    if shell:
        hosts.add(shell)

    def collect(hwnd: int, _: object) -> bool:
        if win32gui.FindWindowEx(hwnd, 0, "SHELLDLL_DefView", None):
            hosts.add(int(hwnd))
        return True

    win32gui.EnumWindows(collect, None)
    return hosts


def _require_desktop_foreground(win32gui: Any) -> None:
    foreground = int(win32gui.GetForegroundWindow() or 0)
    root = int(win32gui.GetAncestor(foreground, GA_ROOT) or foreground)
    if foreground and root in _desktop_shell_hosts(win32gui):
        return
    if foreground:
        try:
            left, top, right, bottom = win32gui.GetWindowRect(root)
            desktop = win32gui.GetWindowRect(win32gui.GetDesktopWindow())
        except (AttributeError, OSError):
            pass
        else:
            desktop_left, desktop_top, desktop_right, desktop_bottom = desktop
            intersects_desktop = (
                left < desktop_right
                and right > desktop_left
                and top < desktop_bottom
                and bottom > desktop_top
            )
            if not intersects_desktop:
                return
    raise RuntimeFailure(
        "desktop-not-foreground: show the desktop manually and retry; "
        "the workflow does not send Win+D"
    )


def _windows_for_pid(pid: int) -> list[Any]:
    try:
        return _uia_windows(process=pid, visible_only=True)
    except Exception as exc:
        raise RuntimeFailure(
            "window discovery failed; refusing to force terminate the process"
        ) from exc


def _stop_unique_instance(app: dict[str, Any], deps: dict[str, Any]) -> None:
    psutil = deps["psutil"]
    matches = _matching_processes(psutil, app["executable"])
    if len(matches) > 1:
        raise RuntimeFailure(f"app {app['alias']} has multiple matching processes; select one manually")
    if not matches:
        return
    process = matches[0]
    windows = _windows_for_pid(process.pid)
    before_titles = {window.window_text() for window in windows}
    if windows:
        try:
            windows[0].close()
        except Exception:
            pass
    try:
        process.wait(timeout=float(app["shutdown_timeout_seconds"]))
        return
    except psutil.TimeoutExpired:
        pass
    after_windows = _windows_for_pid(process.pid)
    after_titles = {window.window_text() for window in after_windows}
    if after_windows and after_titles != before_titles:
        raise RuntimeFailure(
            f"app {app['alias']} displayed a close/save dialog; user action is required"
        )
    if not app.get("force_terminate"):
        raise RuntimeFailure(f"app {app['alias']} did not exit before timeout")
    process.kill()
    process.wait(timeout=5)


def _launch(app: dict[str, Any]) -> subprocess.Popen[bytes]:
    executable = app.get("launch_executable") or app.get("executable")
    if not executable or not Path(executable).is_file():
        raise RuntimeFailure(f"app {app['alias']} launch executable does not exist: {executable}")
    return subprocess.Popen([executable, *app.get("launch_args", [])])


def _wait_unique_process(app: dict[str, Any], deps: dict[str, Any]) -> Any:
    psutil = deps["psutil"]
    deadline = time.monotonic() + float(app["startup_timeout_seconds"])
    while time.monotonic() < deadline:
        matches = _matching_processes(psutil, app["executable"])
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeFailure(f"app {app['alias']} has multiple matching processes")
        time.sleep(0.25)
    raise RuntimeFailure(f"app {app['alias']} process was not found before timeout")


def _wait_processes(
    app: dict[str, Any], deps: dict[str, Any], executable: str | None = None,
) -> list[Any]:
    psutil = deps["psutil"]
    target = executable or app["executable"]
    deadline = time.monotonic() + float(app["startup_timeout_seconds"])
    while time.monotonic() < deadline:
        matches = _matching_processes(psutil, target)
        if matches:
            return matches
        time.sleep(0.25)
    raise RuntimeFailure(f"app {app['alias']} process was not found before timeout")


def _find_window(
    app: dict[str, Any], deps: dict[str, Any], pid: int | None = None,
    pids: set[int] | None = None,
) -> Any:
    if pid is not None and pids is not None:
        raise RuntimeFailure("window lookup cannot use both pid and pids")
    deadline = time.monotonic() + float(app["startup_timeout_seconds"])
    while time.monotonic() < deadline:
        criteria: dict[str, Any] = {
            "title_re": app["window_title_regex"],
            "visible_only": True,
        }
        if pid is not None:
            criteria["process"] = pid
        windows = _uia_windows(**criteria)
        if pids is not None:
            windows = [window for window in windows if window.process_id() in pids]
        if len(windows) == 1:
            return windows[0]
        if len(windows) > 1:
            raise RuntimeFailure(f"app {app['alias']} matched multiple windows")
        time.sleep(0.25)
    raise RuntimeFailure(f"app {app['alias']} window was not found before timeout")


def _prepare_apps(manifest: dict[str, Any], deps: dict[str, Any]) -> dict[str, int]:
    psutil = deps["psutil"]
    init_device = deps["init_device"]
    indexes: dict[str, int] = {}
    windows: dict[str, Any] = {}
    desktop_aliases: set[str] = set()
    for app in manifest["apps"]:
        if app["target_kind"] == "desktop":
            init_device(platform="Windows")
            indexes[app["alias"]] = len(indexes)
            desktop_aliases.add(app["alias"])
            continue
        pid: int | None = None
        pids: set[int] | None = None
        if app["target_kind"] == "application":
            matches = _matching_processes(psutil, app["executable"])
            if app["lifecycle"] == "restart":
                _stop_unique_instance(app, deps)
                _launch(app)
                process = _wait_unique_process(app, deps)
                pid = process.pid
            elif app["lifecycle"] == "reuse":
                if not matches:
                    _launch(app)
            elif not matches:
                raise RuntimeFailure(f"attach-only app {app['alias']} is not running")
            if app["lifecycle"] != "restart":
                matches = _wait_processes(app, deps)
                if len(matches) == 1:
                    pid = matches[0].pid
                else:
                    pids = {process.pid for process in matches}
            window_process_executable = app.get("window_process_executable")
            if window_process_executable:
                window_processes = _wait_processes(
                    app, deps, executable=window_process_executable
                )
                pid = None
                pids = {process.pid for process in window_processes}
        window = _find_window(app, deps, pid, pids)
        handle = window.handle
        init_device(platform="Windows", uuid=str(handle))
        indexes[app["alias"]] = len(indexes)
        windows[app["alias"]] = window
    deps["windows"] = windows
    deps["desktop_aliases"] = desktop_aliases
    return indexes


@contextmanager
def _preserve_text_clipboard(win32clipboard: Any):
    original: str | None = None
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                original = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        original = None
    try:
        yield
    finally:
        if original is not None:
            try:
                win32clipboard.OpenClipboard()
                try:
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, original)
                finally:
                    win32clipboard.CloseClipboard()
            except Exception:
                pass


def _template(deps: dict[str, Any], bundle: Path, step: dict[str, Any], key: str = "template") -> Any:
    return deps["Template"](
        str(bundle / step[key]), threshold=float(step.get("threshold", 0.8))
    )


def _primary_display_crop(device: Any, screen: Any) -> tuple[Any, int, int]:
    virtual = device.monitor
    primary = device.main_monitor
    expected = (int(virtual["height"]), int(virtual["width"]))
    if getattr(screen, "shape", ())[:2] != expected:
        raise RuntimeFailure("display-mismatch: desktop screenshot size changed")
    left = int(primary["left"] - virtual["left"])
    top = int(primary["top"] - virtual["top"])
    right = left + int(primary["width"])
    bottom = top + int(primary["height"])
    if left < 0 or top < 0 or right > expected[1] or bottom > expected[0]:
        raise RuntimeFailure(
            "display-mismatch: primary display is outside virtual desktop"
        )
    return screen[top:bottom, left:right], left, top


def _primary_local_to_virtual(
    device: Any, position: tuple[int, int],
) -> tuple[int, int]:
    virtual = device.monitor
    primary = device.main_monitor
    x, y = map(int, position)
    width = int(primary["width"])
    height = int(primary["height"])
    offset_x = int(primary["left"] - virtual["left"])
    offset_y = int(primary["top"] - virtual["top"])
    if (
        offset_x < 0
        or offset_y < 0
        or offset_x + width > int(virtual["width"])
        or offset_y + height > int(virtual["height"])
    ):
        raise RuntimeFailure(
            "display-mismatch: primary display is outside virtual desktop"
        )
    if not 0 <= x < width or not 0 <= y < height:
        raise RuntimeFailure(
            "display-mismatch: desktop coordinate is outside primary display"
        )
    return (
        x + offset_x,
        y + offset_y,
    )


def _desktop_match_once(
    deps: dict[str, Any], target: Any,
) -> tuple[int, int] | None:
    current = deps["device"]()
    screen = current.snapshot()
    primary, offset_x, offset_y = _primary_display_crop(current, screen)
    matches = target.match_all_in(primary) or []
    if len(matches) > 1:
        raise RuntimeFailure(
            f"ambiguous-match: desktop template matched {len(matches)} locations"
        )
    if not matches:
        return None
    x, y = matches[0]["result"]
    return int(x) + offset_x, int(y) + offset_y


def _wait_for_unique_desktop_match(
    deps: dict[str, Any],
    target: Any,
    timeout: float,
    *,
    clock: Any = time.monotonic,
    sleeper: Any = time.sleep,
) -> tuple[int, int]:
    deadline = clock() + timeout
    while True:
        position = _desktop_match_once(deps, target)
        if position is not None:
            return position
        if clock() >= deadline:
            raise deps["TargetNotFoundError"]("desktop template was not found")
        sleeper(0.25)


def _activate(deps: dict[str, Any], indexes: dict[str, int], alias: str) -> None:
    deps["set_current"](indexes[alias])
    window = deps.get("windows", {}).get(alias)
    if window is not None:
        _focus_window(window)


def _hotkey(deps: dict[str, Any], keys: list[str]) -> None:
    modifiers = {"CTRL": "^", "ALT": "%", "SHIFT": "+"}
    prefix = "".join(modifiers[key.upper()] for key in keys[:-1] if key.upper() in modifiers)
    final = keys[-1]
    if prefix and len(final) == 1 and final.isalpha():
        final = final.lower()
    token = final if len(final) == 1 else "{" + final.upper() + "}"
    keyboard = deps["keyboard"]
    send_keys = getattr(keyboard, "send_keys", None) or getattr(keyboard, "SendKeys", None)
    if not callable(send_keys):
        raise RuntimeFailure("pywinauto keyboard API is unavailable")
    send_keys(prefix + token, pause=0.02)


def _run_step(
    step: dict[str, Any], bundle: Path, parameters: dict[str, str],
    indexes: dict[str, int], deps: dict[str, Any]
) -> None:
    _activate(deps, indexes, step["app"])
    action = step["action"]
    if (
        step["app"] in deps.get("desktop_aliases", set())
        and action in DESKTOP_INPUT_ACTIONS
    ):
        _require_desktop_foreground(deps["win32gui"])
    timeout = float(step["timeout_seconds"])
    if action in {"click-image", "double-click-image", "right-click-image", "wait-image"}:
        target = _template(deps, bundle, step)
        is_desktop = step["app"] in deps.get("desktop_aliases", set())
        if is_desktop:
            position = _wait_for_unique_desktop_match(deps, target, timeout)
        else:
            position = deps["wait"](target, timeout=timeout)
        if action == "wait-image":
            return
        if action == "right-click-image":
            if is_desktop:
                deps["touch"](position, right_click=True)
            else:
                deps["mouse"].click(button="right", coords=position)
        else:
            deps["touch"](position, times=2 if action == "double-click-image" else 1)
    elif action == "click-coordinate":
        position = (int(step["x"]), int(step["y"]))
        if step["app"] in deps.get("desktop_aliases", set()):
            position = _primary_local_to_virtual(deps["device"](), position)
        deps["touch"](position)
    elif action == "type-text":
        deps["text"](_resolve_text(step["text"], parameters), enter=False)
    elif action == "press-key":
        _hotkey(deps, [step["key"]])
    elif action == "hotkey":
        _hotkey(deps, list(step["keys"]))
    elif action == "scroll":
        amount = int(step.get("amount", 1))
        direction = step["direction"]
        wheel = amount if direction in {"up", "left"} else -amount
        deps["mouse"].scroll(coords=None, wheel_dist=wheel)
    elif action == "drag-image":
        if step["app"] in deps.get("desktop_aliases", set()):
            start = _wait_for_unique_desktop_match(
                deps, _template(deps, bundle, step, "from_template"), timeout
            )
            end = _wait_for_unique_desktop_match(
                deps, _template(deps, bundle, step, "to_template"), timeout
            )
            deps["swipe"](start, end)
        else:
            start = deps["wait"](
                _template(deps, bundle, step, "from_template"), timeout=timeout
            )
            end = deps["wait"](
                _template(deps, bundle, step, "to_template"), timeout=timeout
            )
            deps["mouse"].move(coords=start)
            deps["mouse"].press(button="left", coords=start)
            deps["mouse"].move(coords=end)
            deps["mouse"].release(button="left", coords=end)
    elif action == "copy-text":
        _hotkey(deps, ["CTRL", "C"])
    elif action == "paste-text":
        _hotkey(deps, ["CTRL", "V"])
    elif action == "manual-step":
        input(f"Manual step required: {step['description']}\nPress Enter to continue...")
    else:
        raise RuntimeFailure(f"unsupported runtime action: {action}")


def _run_step_with_retries(
    step: dict[str, Any],
    bundle: Path,
    parameters: dict[str, str],
    indexes: dict[str, int],
    deps: dict[str, Any],
) -> None:
    attempts = int(step.get("retries", 0)) + 1
    for attempt in range(attempts):
        try:
            _run_step(step, bundle, parameters, indexes, deps)
            return
        except deps["TargetNotFoundError"]:
            if attempt + 1 >= attempts:
                if step.get("optional"):
                    return
                raise


def _probe_component_assertion(
    assertion: dict[str, Any],
    bundle: Path,
    indexes: dict[str, int],
    deps: dict[str, Any],
) -> bool:
    _activate(deps, indexes, assertion["app"])
    target = _template(deps, bundle, assertion)
    is_desktop = assertion["app"] in deps.get("desktop_aliases", set())

    def visible() -> bool:
        match = (
            _desktop_match_once(deps, target)
            if is_desktop
            else deps["exists"](target)
        )
        return bool(match)

    if not visible():
        return False
    stable_seconds = float(assertion.get("stable_seconds", 0))
    if stable_seconds > 0:
        time.sleep(min(stable_seconds, float(assertion.get("timeout_seconds", 1))))
        return visible()
    return True


def _component_result(
    component: dict[str, Any],
    *,
    status: str | None,
    error: str | None,
    phase: str,
) -> dict[str, Any]:
    return {
        "occurrence_id": component["occurrence_id"],
        "component_id": component["id"],
        "version": component["version"],
        "status": status,
        "error": error,
        "phase": phase,
    }


def _probe_component_phase(
    component: dict[str, Any],
    phase: LifecyclePhase,
    assertion: dict[str, Any],
    bundle: Path,
    indexes: dict[str, int],
    deps: dict[str, Any],
) -> bool:
    try:
        return _probe_component_assertion(assertion, bundle, indexes, deps)
    except RuntimeFailure as exc:
        raise ComponentRuntimeFailure(
            str(exc),
            _component_result(
                component,
                status="failed",
                error=str(exc),
                phase=phase.value,
            ),
        ) from exc


def _run_component_steps(
    steps: list[dict[str, Any]],
    bundle: Path,
    parameters: dict[str, str],
    indexes: dict[str, int],
    deps: dict[str, Any],
) -> dict[str, Any]:
    component = steps[0]["component"]
    state = initial_state(
        has_already_complete=component.get("already_complete") is not None,
        has_precondition=component.get("precondition") is not None,
        has_recovery=component.get("recovery") is not None,
    )
    last_phase = state.phase
    while state.phase not in {LifecyclePhase.COMPLETE, LifecyclePhase.FAILED}:
        last_phase = state.phase
        if state.phase is LifecyclePhase.CHECK_ALREADY_COMPLETE:
            assertion = component["already_complete"]
            try:
                passed = _probe_component_phase(
                    component, state.phase, assertion, bundle, indexes, deps
                )
            except deps["TargetNotFoundError"]:
                passed = False
            state = transition(
                state, "assertion-passed" if passed else "assertion-failed"
            )
        elif state.phase in {
            LifecyclePhase.CHECK_PRECONDITION,
            LifecyclePhase.RECHECK_PRECONDITION,
        }:
            assertion = component["precondition"]
            try:
                passed = _probe_component_phase(
                    component, state.phase, assertion, bundle, indexes, deps
                )
            except deps["TargetNotFoundError"]:
                passed = False
            state = transition(
                state, "assertion-passed" if passed else "assertion-failed"
            )
        elif state.phase is LifecyclePhase.RUN_RECOVERY:
            recovery = component["recovery"]
            try:
                for recovery_step in recovery["steps"]:
                    _run_step_with_retries(
                        recovery_step, bundle, parameters, indexes, deps
                    )
                recovered = _probe_component_assertion(
                    recovery["postcondition"], bundle, indexes, deps
                )
            except (RuntimeFailure, deps["TargetNotFoundError"]):
                recovered = False
            state = transition(
                state, "action-passed" if recovered else "action-failed"
            )
        elif state.phase is LifecyclePhase.RUN_ACTION:
            try:
                for step in steps:
                    _run_step_with_retries(
                        step, bundle, parameters, indexes, deps
                    )
            except (RuntimeFailure, deps["TargetNotFoundError"]):
                state = transition(state, "action-failed")
            else:
                state = transition(state, "action-passed")
        elif state.phase is LifecyclePhase.CHECK_POSTCONDITION:
            try:
                passed = _probe_component_phase(
                    component,
                    state.phase,
                    component["postcondition"],
                    bundle,
                    indexes,
                    deps,
                )
            except deps["TargetNotFoundError"]:
                passed = False
            state = transition(
                state, "assertion-passed" if passed else "assertion-failed"
            )
        else:  # pragma: no cover - guarded by the shared transition table
            raise RuntimeFailure(f"unsupported component phase: {state.phase.value}")

    result = _component_result(
        component,
        status=state.outcome,
        error=state.error,
        phase=last_phase.value,
    )
    if state.phase is LifecyclePhase.FAILED:
        raise ComponentRuntimeFailure(str(state.error), result)
    return result


def _run_steps(
    manifest: dict[str, Any], bundle: Path, parameters: dict[str, str],
    indexes: dict[str, int], deps: dict[str, Any]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    steps = manifest["steps"]
    index = 0
    while index < len(steps):
        step = steps[index]
        component = step.get("component")
        if component is None:
            _run_step_with_retries(step, bundle, parameters, indexes, deps)
            index += 1
            continue
        occurrence_id = component["occurrence_id"]
        group: list[dict[str, Any]] = []
        while index < len(steps):
            candidate = steps[index]
            candidate_component = candidate.get("component")
            if (
                candidate_component is None
                or candidate_component.get("occurrence_id") != occurrence_id
            ):
                break
            group.append(candidate)
            index += 1
        try:
            results.append(
                _run_component_steps(group, bundle, parameters, indexes, deps)
            )
        except ComponentRuntimeFailure as exc:
            exc.components = [*results, *exc.components]
            raise
    return results


def _assert_success(
    assertion: dict[str, Any], bundle: Path, indexes: dict[str, int], deps: dict[str, Any]
) -> None:
    _activate(deps, indexes, assertion["app"])
    target = _template(deps, bundle, assertion)
    deadline = time.monotonic() + float(assertion["timeout_seconds"])
    stable_since: float | None = None
    is_desktop = assertion["app"] in deps.get("desktop_aliases", set())
    while time.monotonic() < deadline:
        found = (
            _desktop_match_once(deps, target)
            if is_desktop
            else deps["exists"](target)
        )
        if found:
            stable_since = stable_since or time.monotonic()
            if time.monotonic() - stable_since >= float(assertion["stable_seconds"]):
                return
        else:
            stable_since = None
        time.sleep(0.25)
    raise RuntimeFailure("the user-confirmed final assertion did not remain visible")


def run_bundle(
    bundle: Path,
    *,
    parameters_path: Path | None = None,
    confirmed_plan_sha256: str | None = None,
) -> int:
    bundle = bundle.resolve()
    manifest = _read_json(bundle / "manifest.json", "manifest")
    deps: dict[str, Any] = {}
    components: list[dict[str, Any]] = []
    result_path = bundle / "run-result.json"
    started = time.time()
    try:
        _validate_bundle_integrity(bundle, manifest, confirmed_plan_sha256)
        deps = _dependencies()
        parameters = (
            _parameters(manifest, bundle)
            if parameters_path is None
            else _parameters(manifest, bundle, parameters_path)
        )
        indexes = _prepare_apps(manifest, deps)
        with _preserve_text_clipboard(deps["win32clipboard"]):
            components = (
                _run_steps(manifest, bundle, parameters, indexes, deps) or []
            )
            _assert_success(manifest["success_assertion"], bundle, indexes, deps)
        evidence = bundle / "replay-success.png"
        deps["snapshot"](filename=str(evidence))
        result = {
            "status": "replay-verified",
            "started_at": started,
            "finished_at": time.time(),
            "aggregate_effect": manifest["aggregate_effect"],
            "evidence": str(evidence),
            "components": components,
        }
        exit_code = 0
    except Exception as exc:
        if isinstance(exc, ComponentRuntimeFailure):
            components = exc.components
        evidence = bundle / "replay-failure.png"
        try:
            if not deps:
                raise RuntimeFailure("runtime dependencies were not initialized")
            deps["snapshot"](filename=str(evidence))
        except Exception:
            evidence = None
        result = {
            "status": "replay-failed",
            "started_at": started,
            "finished_at": time.time(),
            "error": str(exc),
            "evidence": str(evidence) if evidence else None,
            "components": components,
        }
        exit_code = 1
    manifest["status"] = result["status"]
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return exit_code
