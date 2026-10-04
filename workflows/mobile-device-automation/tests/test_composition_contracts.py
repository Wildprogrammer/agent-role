from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from agent_workflow_hub.mobile_device_automation.composition_contracts import (
    component_descriptor_path,
    load_component,
    load_composition_request,
    load_scenario,
)
from agent_workflow_hub.mobile_device_automation.contracts import ContractError


@dataclass
class CatalogFixture:
    root: Path
    component_path: Path
    scenario_path: Path
    request_path: Path
    stage: Path

    def write_action(self, name: str = "action.ad", text: str | None = None) -> Path:
        path = self.component_path.parent / name
        path.write_text(
            text or "context platform=android\nopen com.example.app\n",
            encoding="utf-8",
        )
        return path

    def write_component(self, **changes: object) -> None:
        value: dict[str, object] = {
            "schema_version": "1.0",
            "app_id": "example-app",
            "id": "ensure-home",
            "version": 1,
            "effect": "idempotent",
            "timeout_seconds": 30,
            "parameters": [],
            "already_complete": {
                "kind": "selector-visible",
                "selector": "id=home-marker",
                "timeout_seconds": 2,
            },
            "implementations": {"default": {"script": "action.ad"}},
            "postcondition": {
                "kind": "selector-visible",
                "selector": "id=home-marker",
                "timeout_seconds": 5,
            },
        }
        for key, item in changes.items():
            if key == "script":
                value["implementations"] = {"default": {"script": item}}
            else:
                value[key] = item
        self.component_path.write_text(
            yaml.safe_dump(value, sort_keys=False), encoding="utf-8"
        )

    def link_component_to_outside(self) -> None:
        outside = self.stage / "outside-component.yaml"
        outside.write_text(self.component_path.read_text(encoding="utf-8"), encoding="utf-8")
        self.component_path.unlink()
        try:
            os.symlink(outside, self.component_path)
        except OSError as exc:
            pytest.skip(f"file symlink is unavailable: {exc}")

    def load_request(self):
        return load_composition_request(self.request_path)


@pytest.fixture
def catalog_fixture(tmp_path: Path) -> CatalogFixture:
    root = tmp_path / "catalog"
    component_path = root / "components" / "ensure-home" / "v001" / "component.yaml"
    scenario_path = root / "scenarios" / "home.yaml"
    request_path = tmp_path / "composition-request.json"
    component_path.parent.mkdir(parents=True)
    scenario_path.parent.mkdir(parents=True)
    fixture = CatalogFixture(root, component_path, scenario_path, request_path, tmp_path)
    fixture.write_action()
    fixture.write_component()
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "app_id": "example-app",
                "id": "home",
                "description": "Normalize the example application home state.",
                "parameters": [],
                "components": [{"use": "ensure-home@1"}],
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
                "catalog_root": str(root.resolve()),
                "text_input_mode": "direct-ime",
                "source_surface": "agent-device-cli",
                "target_platform": "android",
                "device": {
                    "serial": "emulator-5554",
                    "transport": "emulator",
                    "viewport": {
                        "width": 1080,
                        "height": 2400,
                        "orientation": "portrait",
                        "density": 2.75,
                    },
                },
                "decision": "generate-only",
                "effective_path_confirmed": True,
            }
        ),
        encoding="utf-8",
    )
    return fixture


def test_loads_exact_version_component_scenario_and_request(
    catalog_fixture: CatalogFixture,
) -> None:
    request = load_composition_request(catalog_fixture.request_path)
    scenario = load_scenario(request.scenario, catalog_root=request.catalog_root)
    component = load_component(
        request.catalog_root / "components/ensure-home/v001/component.yaml",
        catalog_root=request.catalog_root,
    )
    assert request.text_input_mode == "direct-ime"
    assert scenario.components[0].component_id == "ensure-home"
    assert scenario.components[0].version == 1
    assert component.implementations["default"].name == "action.ad"
    assert len(component.descriptor_sha256) == 64


def test_component_descriptor_path_is_exact(catalog_fixture: CatalogFixture) -> None:
    assert component_descriptor_path(catalog_fixture.root, "ensure-home", 1) == (
        catalog_fixture.root / "components" / "ensure-home" / "v001" / "component.yaml"
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"unexpected": True}, "component_catalog_invalid"),
        ({"id": "Ensure Home"}, "component_catalog_invalid"),
        ({"version": 0}, "component_catalog_invalid"),
        ({"timeout_seconds": 0}, "component_catalog_invalid"),
        ({"implementations": {}}, "component_catalog_invalid"),
        ({"postcondition": None}, "component_catalog_invalid"),
    ],
)
def test_rejects_invalid_component_contract(
    catalog_fixture: CatalogFixture, changes: dict[str, object], message: str
) -> None:
    catalog_fixture.write_component(**changes)
    with pytest.raises(ContractError, match=message):
        load_component(catalog_fixture.component_path, catalog_root=catalog_fixture.root)


def test_component_requires_precondition_unless_it_is_idempotent_normalizer(
    catalog_fixture: CatalogFixture,
) -> None:
    catalog_fixture.write_component(effect="create")
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(catalog_fixture.component_path, catalog_root=catalog_fixture.root)


def test_component_lifecycle_assertions_are_selector_only(
    catalog_fixture: CatalogFixture, tmp_path: Path
) -> None:
    image = tmp_path / "marker.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nmarker")
    catalog_fixture.write_component(
        postcondition={
            "kind": "image-visible",
            "template": str(image.resolve()),
            "threshold": 0.8,
            "timeout_seconds": 2,
        }
    )
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(catalog_fixture.component_path, catalog_root=catalog_fixture.root)


def test_component_asset_must_resolve_inside_version_directory(
    catalog_fixture: CatalogFixture,
) -> None:
    outside = catalog_fixture.stage / "outside.ad"
    outside.write_text("context platform=android\n", encoding="utf-8")
    catalog_fixture.write_component(script="../../../../outside.ad")
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(catalog_fixture.component_path, catalog_root=catalog_fixture.root)


def test_catalog_link_cannot_escape_root(catalog_fixture: CatalogFixture) -> None:
    catalog_fixture.link_component_to_outside()
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(catalog_fixture.component_path, catalog_root=catalog_fixture.root)


def test_missing_exact_component_path_is_not_found(catalog_fixture: CatalogFixture) -> None:
    missing = component_descriptor_path(catalog_fixture.root, "ensure-home", 2)
    with pytest.raises(ContractError, match="component_not_found"):
        load_component(missing, catalog_root=catalog_fixture.root)


def test_descriptor_identity_must_match_version_directory(
    catalog_fixture: CatalogFixture,
) -> None:
    catalog_fixture.write_component(version=2)
    with pytest.raises(ContractError, match="component_version_mismatch"):
        load_component(catalog_fixture.component_path, catalog_root=catalog_fixture.root)


def test_scenario_rejects_unknown_fields_and_malformed_exact_reference(
    catalog_fixture: CatalogFixture,
) -> None:
    value = yaml.safe_load(catalog_fixture.scenario_path.read_text(encoding="utf-8"))
    value["components"] = [{"use": "ensure-home"}]
    value["unexpected"] = True
    catalog_fixture.scenario_path.write_text(yaml.safe_dump(value), encoding="utf-8")
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_scenario(catalog_fixture.scenario_path, catalog_root=catalog_fixture.root)


def test_scenario_must_match_component_app_id(catalog_fixture: CatalogFixture) -> None:
    value = yaml.safe_load(catalog_fixture.scenario_path.read_text(encoding="utf-8"))
    value["app_id"] = "different-app"
    catalog_fixture.scenario_path.write_text(yaml.safe_dump(value), encoding="utf-8")
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_scenario(catalog_fixture.scenario_path, catalog_root=catalog_fixture.root)


def test_scenario_path_must_stay_inside_catalog(catalog_fixture: CatalogFixture) -> None:
    outside = catalog_fixture.stage / "outside-scenario.yaml"
    outside.write_text(catalog_fixture.scenario_path.read_text(encoding="utf-8"), encoding="utf-8")
    request = json.loads(catalog_fixture.request_path.read_text(encoding="utf-8"))
    request["scenario"] = str(outside.resolve())
    catalog_fixture.request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        catalog_fixture.load_request()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("text_input_mode", "unknown"),
        ("target_platform", "ios"),
        ("effective_path_confirmed", False),
        ("unexpected", True),
    ],
)
def test_rejects_invalid_composition_request(
    catalog_fixture: CatalogFixture, field: str, value: object
) -> None:
    request = json.loads(catalog_fixture.request_path.read_text(encoding="utf-8"))
    request[field] = value
    catalog_fixture.request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        catalog_fixture.load_request()


def test_missing_implementation_script_is_catalog_invalid(
    catalog_fixture: CatalogFixture,
) -> None:
    catalog_fixture.write_component(script="missing.ad")
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(catalog_fixture.component_path, catalog_root=catalog_fixture.root)
