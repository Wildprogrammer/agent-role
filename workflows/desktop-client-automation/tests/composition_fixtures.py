from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from PIL import Image


def _write_image(path: Path, color: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), color).save(path)
    return path


@dataclass
class DesktopCatalogFixture:
    root: Path
    component_path: Path
    scenario_path: Path
    request_path: Path
    action_template: Path
    success_template: Path

    def update_component(self, **changes: object) -> None:
        value = yaml.safe_load(self.component_path.read_text(encoding="utf-8"))
        template = changes.pop("template", None)
        if template is not None:
            value["implementations"]["windows-airtest"]["steps"][0][
                "template"
            ] = template
        value.update(changes)
        self.component_path.write_text(
            yaml.safe_dump(value, sort_keys=False),
            encoding="utf-8",
        )

    def update_request(self, **changes: object) -> None:
        value = json.loads(self.request_path.read_text(encoding="utf-8"))
        value.update(changes)
        self.request_path.write_text(json.dumps(value), encoding="utf-8")

    def change_template_pixels(self) -> None:
        _write_image(self.action_template, "blue")

    def link_action_template_to(self, target: Path) -> None:
        self.action_template.unlink()
        try:
            os.symlink(target, self.action_template)
        except OSError as exc:
            pytest.skip(f"file symlink is unavailable: {exc}")


@pytest.fixture
def desktop_catalog(tmp_path: Path) -> DesktopCatalogFixture:
    root = (tmp_path / "catalog").resolve()
    version_root = root / "components" / "search-model" / "v001"
    scenario_path = root / "scenarios" / "search-favorite-model.yaml"
    component_path = version_root / "component.yaml"
    request_path = (tmp_path / "composition-request.json").resolve()
    action_template = _write_image(version_root / "search-button.png", "red")
    _write_image(version_root / "favorites-state.png", "green")
    _write_image(version_root / "results-state.png", "yellow")
    success_template = _write_image(tmp_path / "success.png", "white")

    component_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "platform": "windows",
                "app_id": "example-app",
                "id": "search-model",
                "version": 1,
                "effect": "read",
                "timeout_seconds": 20,
                "parameters": ["query"],
                "precondition": {
                    "kind": "image-visible",
                    "template": "favorites-state.png",
                    "threshold": 0.85,
                    "timeout_seconds": 5,
                    "stable_seconds": 0.5,
                },
                "implementations": {
                    "windows-airtest": {
                        "steps": [
                            {
                                "action": "click-image",
                                "template": "search-button.png",
                            },
                            {"action": "hotkey", "keys": ["CTRL", "A"]},
                            {"action": "type-text", "text": "${query}"},
                            {"action": "press-key", "key": "ENTER"},
                        ]
                    }
                },
                "postcondition": {
                    "kind": "image-visible",
                    "template": "results-state.png",
                    "threshold": 0.85,
                    "timeout_seconds": 10,
                    "stable_seconds": 0.5,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    scenario_path.parent.mkdir(parents=True, exist_ok=True)
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "app_id": "example-app",
                "id": "search-favorite-model",
                "description": "Search one model in favorites.",
                "parameters": [
                    {
                        "name": "term",
                        "kind": "text",
                        "default": None,
                        "sensitive": False,
                    }
                ],
                "components": [
                    {"use": "search-model@1", "with": {"query": "term"}}
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    request_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scenario": str(scenario_path.resolve()),
                "catalog_root": str(root),
                "source_surface": "cua-driver-mcp",
                "target_platform": "windows",
                "apps": [
                    {
                        "alias": "example-app",
                        "target_kind": "application",
                        "lifecycle": "reuse",
                        "executable": str((tmp_path / "ExampleApp.exe").resolve()),
                        "launch_args": [],
                        "window_title_regex": "^Example App$",
                    }
                ],
                "success_assertion": {
                    "app": "example-app",
                    "template": str(success_template.resolve()),
                    "threshold": 0.85,
                    "timeout_seconds": 10,
                    "stable_seconds": 0.5,
                },
                "decision": "generate-only",
                "effective_path_confirmed": True,
            }
        ),
        encoding="utf-8",
    )
    return DesktopCatalogFixture(
        root=root,
        component_path=component_path,
        scenario_path=scenario_path,
        request_path=request_path,
        action_template=action_template,
        success_template=success_template,
    )
