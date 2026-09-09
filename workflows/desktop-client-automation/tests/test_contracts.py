from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from agent_workflow_hub.desktop_client_automation.contracts import ContractError, load_request


def _image(path: Path) -> Path:
    Image.new("RGB", (20, 20), "white").save(path)
    return path.resolve()


def _request(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    target = _image(tmp_path / "target.png")
    success = _image(tmp_path / "success.png")
    value: dict[str, object] = {
        "schema_version": "1.0",
        "name": "demo-flow",
        "description": "跨应用演示",
        "source_surface": "codex-desktop-native-windows-computer-use",
        "target_platform": "windows",
        "effective_path_confirmed": True,
        "decision": "generate-only",
        "apps": [
            {
                "alias": "primary",
                "target_kind": "application",
                "lifecycle": "restart",
                "executable": str((tmp_path / "demo.exe").resolve()),
                "launch_executable": str((tmp_path / "launcher.exe").resolve()),
                "window_process_executable": str((tmp_path / "window-host.exe").resolve()),
                "launch_args": [],
                "window_title_regex": "^Demo$",
                "force_terminate": True,
            },
            {
                "alias": "dialog",
                "target_kind": "system-dialog",
                "lifecycle": "attach-only",
                "window_title_regex": "^Open$",
            },
        ],
        "parameters": [
            {"name": "query", "kind": "text", "default": "hello", "sensitive": False}
        ],
        "steps": [
            {
                "id": "click_search",
                "app": "primary",
                "action": "click-image",
                "effect": "none",
                "template": str(target),
                "retries": 2,
            },
            {
                "id": "type_query",
                "app": "primary",
                "action": "type-text",
                "effect": "none",
                "text": "${query}",
            },
        ],
        "success_assertion": {
            "app": "primary",
            "template": str(success),
            "threshold": 0.85,
            "timeout_seconds": 20,
            "stable_seconds": 1,
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path.resolve(), value


def test_loads_confirmed_cross_app_request(tmp_path: Path) -> None:
    path, _ = _request(tmp_path)
    request = load_request(path)
    assert request.name == "demo-flow"
    assert [app.target_kind for app in request.apps] == ["application", "system-dialog"]
    assert request.apps[0].launch_executable == (tmp_path / "launcher.exe").resolve()
    assert request.apps[0].window_process_executable == (
        tmp_path / "window-host.exe"
    ).resolve()
    assert request.aggregate_effect == "none"
    assert request.success_assertion.stable_seconds == 1


def test_loads_primary_desktop_target(tmp_path: Path) -> None:
    path, value = _request(tmp_path)
    value["apps"][0] = {  # type: ignore[index]
        "alias": "desktop",
        "target_kind": "desktop",
        "lifecycle": "attach-only",
        "display_id": "primary",
    }
    value["steps"][0]["app"] = "desktop"  # type: ignore[index]
    value["steps"][1]["app"] = "desktop"  # type: ignore[index]
    value["success_assertion"]["app"] = "desktop"  # type: ignore[index]
    path.write_text(json.dumps(value), encoding="utf-8")

    request = load_request(path)

    desktop = request.apps[0]
    assert desktop.target_kind == "desktop"
    assert desktop.display_id == "primary"
    assert desktop.window_title_regex is None
    assert desktop.executable is None


@pytest.mark.parametrize(
    ("field", "invalid", "message"),
    [
        ("lifecycle", "reuse", "desktop must be attach-only"),
        ("display_id", "secondary", "display_id must equal primary"),
        ("executable", r"C:\Apps\demo.exe", "desktop must omit"),
        ("window_title_regex", "^Program Manager$", "desktop must omit"),
    ],
)
def test_rejects_invalid_desktop_target(
    tmp_path: Path,
    field: str,
    invalid: object,
    message: str,
) -> None:
    path, value = _request(tmp_path)
    desktop: dict[str, object] = {
        "alias": "desktop",
        "target_kind": "desktop",
        "lifecycle": "attach-only",
        "display_id": "primary",
    }
    desktop[field] = invalid
    value["apps"][0] = desktop  # type: ignore[index]
    value["steps"][0]["app"] = "desktop"  # type: ignore[index]
    value["steps"][1]["app"] = "desktop"  # type: ignore[index]
    value["success_assertion"]["app"] = "desktop"  # type: ignore[index]
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ContractError, match=message):
        load_request(path)


@pytest.mark.parametrize(
    "source_surface",
    [
        "codex-desktop-native-windows-computer-use",
        "cua-driver-mcp",
    ],
)
def test_accepts_supported_source_surfaces(
    tmp_path: Path,
    source_surface: str,
) -> None:
    path, value = _request(tmp_path)
    value["source_surface"] = source_surface
    path.write_text(json.dumps(value), encoding="utf-8")

    assert load_request(path).source_surface == source_surface


def test_rejects_unknown_source_surface(tmp_path: Path) -> None:
    path, value = _request(tmp_path)
    value["source_surface"] = "unreviewed-desktop-driver"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ContractError, match="source_surface must be one of"):
        load_request(path)


def test_rejects_retry_for_side_effecting_action(tmp_path: Path) -> None:
    path, value = _request(tmp_path)
    value["steps"][0]["effect"] = "submit"  # type: ignore[index]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContractError, match="cannot retry"):
        load_request(path)


def test_rejects_system_dialog_lifecycle_or_executable(tmp_path: Path) -> None:
    path, value = _request(tmp_path)
    value["apps"][1]["lifecycle"] = "restart"  # type: ignore[index]
    with pytest.raises(ContractError, match="system-dialog"):
        path.write_text(json.dumps(value), encoding="utf-8")
        load_request(path)


def test_rejects_sensitive_parameter_default(tmp_path: Path) -> None:
    path, value = _request(tmp_path)
    value["parameters"] = [
        {"name": "password", "kind": "text", "default": "secret", "sensitive": True}
    ]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContractError, match="cannot have a stored default"):
        load_request(path)


def test_rejects_automating_sensitive_parameter(tmp_path: Path) -> None:
    path, value = _request(tmp_path)
    value["parameters"] = [
        {"name": "password", "kind": "text", "sensitive": True}
    ]
    value["steps"][1]["text"] = "${password}"  # type: ignore[index]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContractError, match="must not automate sensitive"):
        load_request(path)


def test_rejects_ambiguous_hotkey_sequence(tmp_path: Path) -> None:
    path, value = _request(tmp_path)
    value["steps"][1] = {  # type: ignore[index]
        "id": "bad_hotkey",
        "app": "primary",
        "action": "hotkey",
        "effect": "none",
        "keys": ["A", "B"],
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ContractError, match="zero or more modifiers"):
        load_request(path)
