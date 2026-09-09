from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

from agent_workflow_hub.desktop_client_automation.contracts import load_request
from agent_workflow_hub.desktop_client_automation.renderer import compile_bundle


def _source_request(tmp_path: Path) -> Path:
    target = tmp_path / "button.png"
    success = tmp_path / "success.png"
    Image.new("RGB", (12, 12), "red").save(target)
    Image.new("RGB", (12, 12), "green").save(success)
    value = {
        "schema_version": "1.0",
        "name": "generated-demo",
        "description": "生成示例",
        "source_surface": "cua-driver-mcp",
        "target_platform": "windows",
        "effective_path_confirmed": True,
        "decision": "generate-and-replay",
        "apps": [{
            "alias": "app",
            "target_kind": "application",
            "lifecycle": "reuse",
            "executable": str((tmp_path / "app.exe").resolve()),
            "window_title_regex": "^App$",
        }],
        "parameters": [{"name": "query", "default": "abc"}],
        "steps": [{
            "id": "open",
            "app": "app",
            "action": "click-image",
            "effect": "read",
            "template": str(target.resolve()),
        }],
        "success_assertion": {"app": "app", "template": str(success.resolve())},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path.resolve()


def test_compile_creates_versioned_non_overwriting_air_bundles(tmp_path: Path) -> None:
    request = load_request(_source_request(tmp_path))
    output = (tmp_path / "outputs").resolve()
    first = compile_bundle(request, output)
    second = compile_bundle(request, output)
    assert first.name == "generated-demo-v001.air"
    assert second.name == "generated-demo-v002.air"
    manifest = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "generated-unverified"
    assert manifest["source_surface"] == "cua-driver-mcp"
    assert manifest["aggregate_effect"] == "read"
    assert (first / "assets" / "001-open.png").is_file()
    assert (first / "assets" / "success-assertion.png").is_file()
    entry = first / f"{first.stem}.py"
    assert entry.is_file()
    assert "from _airtest_runtime import run_bundle" in entry.read_text(encoding="utf-8")
    runtime = first / "_airtest_runtime.py"
    assert runtime.is_file()
    assert "_require_foreground(window)" in runtime.read_text(encoding="utf-8")
    assert json.loads((first / "parameters.example.json").read_text(encoding="utf-8")) == {
        "query": "abc"
    }


def test_compile_preserves_native_desktop_target_in_standalone_bundle(
    tmp_path: Path,
) -> None:
    icon = tmp_path / "desktop-icon.png"
    success = tmp_path / "application-window.png"
    Image.new("RGB", (12, 12), "blue").save(icon)
    Image.new("RGB", (12, 12), "green").save(success)
    value = {
        "schema_version": "1.0",
        "name": "desktop-demo",
        "description": "双击主显示器桌面图标。",
        "source_surface": "cua-driver-mcp",
        "target_platform": "windows",
        "effective_path_confirmed": True,
        "decision": "generate-only",
        "apps": [{
            "alias": "desktop",
            "target_kind": "desktop",
            "lifecycle": "attach-only",
            "display_id": "primary",
        }],
        "parameters": [],
        "steps": [{
            "id": "open-application",
            "app": "desktop",
            "action": "double-click-image",
            "effect": "idempotent",
            "template": str(icon.resolve()),
        }],
        "success_assertion": {
            "app": "desktop",
            "template": str(success.resolve()),
        },
    }
    request_path = tmp_path / "desktop-request.json"
    request_path.write_text(json.dumps(value), encoding="utf-8")

    bundle = compile_bundle(
        load_request(request_path.resolve()), (tmp_path / "outputs").resolve()
    )

    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["apps"] == [{
        "alias": "desktop",
        "target_kind": "desktop",
        "lifecycle": "attach-only",
        "display_id": "primary",
        "executable": None,
        "launch_executable": None,
        "window_process_executable": None,
        "launch_args": [],
        "window_title_regex": None,
        "startup_timeout_seconds": 30,
        "shutdown_timeout_seconds": 10,
        "force_terminate": False,
    }]
    runtime = (bundle / "_airtest_runtime.py").read_text(encoding="utf-8")
    assert "_wait_for_unique_desktop_match" in runtime
    assert "desktop-not-foreground" in runtime
