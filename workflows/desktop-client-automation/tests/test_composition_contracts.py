from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import pytest
import yaml

from agent_workflow_hub.desktop_client_automation.composition_contracts import (
    load_component,
    load_composition_request,
    load_scenario,
)
from agent_workflow_hub.desktop_client_automation.contracts import ContractError

_fixtures = import_module(
    "workflows.desktop-client-automation.tests.composition_fixtures"
)
DesktopCatalogFixture = _fixtures.DesktopCatalogFixture
desktop_catalog = _fixtures.desktop_catalog


def test_loads_exact_windows_component_and_scenario(
    desktop_catalog: DesktopCatalogFixture,
) -> None:
    component = load_component(
        desktop_catalog.component_path,
        catalog_root=desktop_catalog.root,
    )
    scenario = load_scenario(
        desktop_catalog.scenario_path,
        catalog_root=desktop_catalog.root,
    )

    assert component.id == "search-model"
    assert component.version == 1
    assert component.precondition.kind == "image-visible"
    assert scenario.components[0].component_id == "search-model"


def test_desktop_component_rejects_unknown_platform_and_asset_escape(
    desktop_catalog: DesktopCatalogFixture,
) -> None:
    desktop_catalog.update_component(platform="macos")
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(
            desktop_catalog.component_path,
            catalog_root=desktop_catalog.root,
        )

    desktop_catalog.update_component(
        platform="windows",
        template="../../../../outside.png",
    )
    with pytest.raises(ContractError, match="component_asset_outside_catalog"):
        load_component(
            desktop_catalog.component_path,
            catalog_root=desktop_catalog.root,
        )


def test_composition_request_is_single_app_windows_only(
    desktop_catalog: DesktopCatalogFixture,
) -> None:
    request = load_composition_request(desktop_catalog.request_path)
    assert request.target_platform == "windows"
    assert len(request.apps) == 1

    desktop_catalog.update_request(target_platform="macos")
    with pytest.raises(ContractError, match="needs-replay-backend"):
        load_composition_request(desktop_catalog.request_path)


def test_component_rejects_unknown_fields(
    desktop_catalog: DesktopCatalogFixture,
) -> None:
    desktop_catalog.update_component(unexpected=True)
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(desktop_catalog.component_path, catalog_root=desktop_catalog.root)


def test_component_rejects_duplicate_yaml_keys(
    desktop_catalog: DesktopCatalogFixture,
) -> None:
    text = desktop_catalog.component_path.read_text(encoding="utf-8")
    desktop_catalog.component_path.write_text(text + "id: duplicate\n", encoding="utf-8")
    with pytest.raises(ContractError, match="component_parameter_invalid"):
        load_component(desktop_catalog.component_path, catalog_root=desktop_catalog.root)


def test_scenario_rejects_floating_component_versions(
    desktop_catalog: DesktopCatalogFixture,
) -> None:
    scenario = yaml.safe_load(desktop_catalog.scenario_path.read_text(encoding="utf-8"))
    scenario["components"][0]["use"] = "search-model@latest"
    desktop_catalog.scenario_path.write_text(
        yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(ContractError, match="component_version_mismatch"):
        load_scenario(desktop_catalog.scenario_path, catalog_root=desktop_catalog.root)


def test_component_requires_zero_padded_version_directory(
    desktop_catalog: DesktopCatalogFixture,
) -> None:
    short_version_root = desktop_catalog.component_path.parents[1] / "v1"
    short_version_root.mkdir()
    short_component = short_version_root / "component.yaml"
    short_component.write_bytes(desktop_catalog.component_path.read_bytes())
    for template in desktop_catalog.component_path.parent.glob("*.png"):
        (short_version_root / template.name).write_bytes(template.read_bytes())

    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(short_component, catalog_root=desktop_catalog.root)


def test_component_rejects_absolute_and_symlink_asset_escape(
    desktop_catalog: DesktopCatalogFixture,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.png"
    outside.write_bytes(desktop_catalog.action_template.read_bytes())
    desktop_catalog.update_component(template=str(outside.resolve()))
    with pytest.raises(ContractError, match="component_asset_outside_catalog"):
        load_component(desktop_catalog.component_path, catalog_root=desktop_catalog.root)

    desktop_catalog.update_component(template="search-button.png")
    desktop_catalog.link_action_template_to(outside.resolve())
    with pytest.raises(ContractError, match="component_asset_outside_catalog"):
        load_component(desktop_catalog.component_path, catalog_root=desktop_catalog.root)


def test_component_rejects_app_mismatch_empty_steps_and_manual_step(
    desktop_catalog: DesktopCatalogFixture,
) -> None:
    scenario = yaml.safe_load(desktop_catalog.scenario_path.read_text(encoding="utf-8"))
    scenario["app_id"] = "different-app"
    desktop_catalog.scenario_path.write_text(
        yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_scenario(desktop_catalog.scenario_path, catalog_root=desktop_catalog.root)

    desktop_catalog.update_component(
        implementations={"windows-airtest": {"steps": []}}
    )
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(desktop_catalog.component_path, catalog_root=desktop_catalog.root)

    desktop_catalog.update_component(
        implementations={
            "windows-airtest": {
                "steps": [{"action": "manual-step", "description": "click it"}]
            }
        }
    )
    with pytest.raises(ContractError, match="component_catalog_invalid"):
        load_component(desktop_catalog.component_path, catalog_root=desktop_catalog.root)


@pytest.mark.parametrize(
    "text",
    ["$(whoami)", "`whoami`", "${bad-token}", "${missing}"],
)
def test_component_rejects_executable_or_unknown_placeholders(
    desktop_catalog: DesktopCatalogFixture,
    text: str,
) -> None:
    value = yaml.safe_load(desktop_catalog.component_path.read_text(encoding="utf-8"))
    value["implementations"]["windows-airtest"]["steps"][2]["text"] = text
    desktop_catalog.component_path.write_text(
        yaml.safe_dump(value, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ContractError, match="component_parameter_invalid"):
        load_component(desktop_catalog.component_path, catalog_root=desktop_catalog.root)


@pytest.mark.parametrize(
    "mapping",
    [{}, {"unknown": "term"}, {"query": "missing"}],
)
def test_scenario_rejects_missing_or_unknown_mappings(
    desktop_catalog: DesktopCatalogFixture,
    mapping: dict[str, str],
) -> None:
    scenario = yaml.safe_load(desktop_catalog.scenario_path.read_text(encoding="utf-8"))
    scenario["components"][0]["with"] = mapping
    desktop_catalog.scenario_path.write_text(
        yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ContractError, match="component_parameter_invalid"):
        load_scenario(desktop_catalog.scenario_path, catalog_root=desktop_catalog.root)


def test_component_rejects_declared_but_unused_mapping(
    desktop_catalog: DesktopCatalogFixture,
) -> None:
    value = yaml.safe_load(desktop_catalog.component_path.read_text(encoding="utf-8"))
    value["implementations"]["windows-airtest"]["steps"][2]["text"] = "fixed"
    desktop_catalog.component_path.write_text(
        yaml.safe_dump(value, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(ContractError, match="unused mappings"):
        load_component(desktop_catalog.component_path, catalog_root=desktop_catalog.root)


def test_public_composition_schemas_are_closed_json_schemas() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    references = Path(__file__).parents[1] / "references"
    for name in (
        "app-component.schema.json",
        "app-scenario.schema.json",
        "app-composition-request.schema.json",
    ):
        schema = json.loads((references / name).read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
