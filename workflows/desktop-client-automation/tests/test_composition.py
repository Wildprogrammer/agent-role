from __future__ import annotations

import json
from importlib import import_module
from pathlib import Path

import yaml

from agent_workflow_hub.desktop_client_automation.composition import (
    compose_automation_request,
    compose_bundle,
)
from agent_workflow_hub.desktop_client_automation.composition_contracts import (
    load_composition_request,
)

_fixtures = import_module(
    "workflows.desktop-client-automation.tests.composition_fixtures"
)
DesktopCatalogFixture = _fixtures.DesktopCatalogFixture
desktop_catalog = _fixtures.desktop_catalog


def _manifest(bundle: Path) -> dict[str, object]:
    return json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))


def test_desktop_scenario_expands_to_existing_steps(
    desktop_catalog: DesktopCatalogFixture,
    tmp_path: Path,
) -> None:
    request = load_composition_request(desktop_catalog.request_path)
    automation = compose_automation_request(
        request,
        staging_root=tmp_path / "stage",
    )

    assert [step.action for step in automation.steps] == [
        "click-image",
        "hotkey",
        "type-text",
        "press-key",
    ]
    assert [step.id for step in automation.steps] == [
        "c001-search-model-s001",
        "c001-search-model-s002",
        "c001-search-model-s003",
        "c001-search-model-s004",
    ]
    assert automation.steps[0].component is not None
    assert automation.steps[0].component.occurrence_id == "c001-search-model"
    assert automation.aggregate_effect == "read"
    assert automation.parameters[0].name == "term"
    assert automation.steps[2].values["text"] == "${term}"


def test_image_drift_without_version_bump_changes_plan_digest(
    desktop_catalog: DesktopCatalogFixture,
    tmp_path: Path,
) -> None:
    first = compose_bundle(
        load_composition_request(desktop_catalog.request_path),
        (tmp_path / "out").resolve(),
    )
    first_manifest = _manifest(first)

    desktop_catalog.change_template_pixels()
    second = compose_bundle(
        load_composition_request(desktop_catalog.request_path),
        (tmp_path / "out").resolve(),
    )
    second_manifest = _manifest(second)

    assert first_manifest["plan_sha256"] != second_manifest["plan_sha256"]
    assert (
        first_manifest["component_provenance"]
        != second_manifest["component_provenance"]
    )


def test_descriptor_drift_without_version_bump_changes_plan_digest(
    desktop_catalog: DesktopCatalogFixture,
    tmp_path: Path,
) -> None:
    first = compose_bundle(
        load_composition_request(desktop_catalog.request_path),
        (tmp_path / "out").resolve(),
    )
    first_manifest = _manifest(first)

    desktop_catalog.update_component(timeout_seconds=7.0)
    second = compose_bundle(
        load_composition_request(desktop_catalog.request_path),
        (tmp_path / "out").resolve(),
    )
    second_manifest = _manifest(second)

    assert first_manifest["plan_sha256"] != second_manifest["plan_sha256"]
    assert (
        first_manifest["component_provenance"]
        != second_manifest["component_provenance"]
    )


def test_runtime_parameter_values_do_not_change_compiled_plan(
    desktop_catalog: DesktopCatalogFixture,
    tmp_path: Path,
) -> None:
    bundle = compose_bundle(
        load_composition_request(desktop_catalog.request_path),
        (tmp_path / "out").resolve(),
    )
    manifest = _manifest(bundle)
    runtime_parameters = tmp_path / "runtime-parameters.json"
    runtime_parameters.write_text('{"term":"different at replay"}', encoding="utf-8")

    assert _manifest(bundle)["plan_sha256"] == manifest["plan_sha256"]

    scenario = yaml.safe_load(desktop_catalog.scenario_path.read_text(encoding="utf-8"))
    scenario["parameters"][0]["default"] = "compiled default"
    desktop_catalog.scenario_path.write_text(
        yaml.safe_dump(scenario, sort_keys=False),
        encoding="utf-8",
    )
    changed = compose_bundle(
        load_composition_request(desktop_catalog.request_path),
        (tmp_path / "out").resolve(),
    )
    assert _manifest(changed)["plan_sha256"] != manifest["plan_sha256"]


def test_legacy_bundle_shape_does_not_gain_composition_fields(
    tmp_path: Path,
) -> None:
    from agent_workflow_hub.desktop_client_automation.contracts import load_request
    from agent_workflow_hub.desktop_client_automation.renderer import compile_bundle

    renderer_tests = import_module(
        "workflows.desktop-client-automation.tests.test_renderer"
    )
    request = load_request(renderer_tests._source_request(tmp_path))
    bundle = compile_bundle(request, (tmp_path / "legacy-output").resolve())
    manifest = _manifest(bundle)

    assert "component_provenance" not in manifest
    assert "plan_sha256" not in manifest
    assert "assets" not in manifest
