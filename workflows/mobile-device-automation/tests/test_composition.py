from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from agent_workflow_hub.mobile_device_automation.composition import (
    compose_automation_request,
    compose_bundle,
)
from agent_workflow_hub.mobile_device_automation.composition_contracts import (
    load_composition_request,
)
from agent_workflow_hub.mobile_device_automation.contracts import ContractError
from agent_workflow_hub.mobile_device_automation.runner import preview_bundle


@dataclass
class CompositionFixture:
    root: Path
    request_path: Path
    scenario_path: Path
    stage: Path

    def load_request(self):
        return load_composition_request(self.request_path)

    def component_path(self, component_id: str) -> Path:
        return self.root / "components" / component_id / "v001" / "component.yaml"

    def write_action(self, text: str, component_id: str = "input-query") -> None:
        path = self.component_path(component_id).parent / "input-pinyin.ad"
        path.write_text("context platform=android\n" + text, encoding="utf-8")


def _selector(value: str) -> dict[str, object]:
    return {
        "kind": "selector-visible",
        "selector": value,
        "timeout_seconds": 2,
    }


@pytest.fixture
def composition_fixture(tmp_path: Path) -> CompositionFixture:
    root = tmp_path / "catalog"
    ensure = root / "components" / "ensure-home" / "v001"
    query = root / "components" / "input-query" / "v001"
    ensure.mkdir(parents=True)
    query.mkdir(parents=True)
    (root / "scenarios").mkdir()
    (ensure / "action.ad").write_text(
        "context platform=android\nopen com.example.app\n", encoding="utf-8"
    )
    (query / "input-direct.ad").write_text(
        'context platform=android\nfill "id=search" "${term}"\n', encoding="utf-8"
    )
    (query / "input-pinyin.ad").write_text(
        'context platform=android\nfill "id=search" "${term}"\n', encoding="utf-8"
    )
    (ensure / "component.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "app_id": "example-app",
                "id": "ensure-home",
                "version": 1,
                "effect": "idempotent",
                "timeout_seconds": 10,
                "parameters": [],
                "already_complete": _selector("id=home-marker"),
                "implementations": {"default": {"script": "action.ad"}},
                "postcondition": _selector("id=home-marker"),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (query / "component.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "app_id": "example-app",
                "id": "input-query",
                "version": 1,
                "effect": "idempotent",
                "timeout_seconds": 10,
                "parameters": ["term"],
                "precondition": _selector("id=search"),
                "implementations": {
                    "direct-ime": {"script": "input-direct.ad"},
                    "pinyin-fallback": {"script": "input-pinyin.ad"},
                },
                "postcondition": _selector("id=search"),
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    scenario_path = root / "scenarios" / "search.yaml"
    scenario_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "app_id": "example-app",
                "id": "search",
                "description": "Search in an example application.",
                "parameters": [
                    {"name": "query", "default": None, "sensitive": False},
                    {"name": "query_pinyin", "default": None, "sensitive": False},
                ],
                "components": [
                    {"use": "ensure-home@1"},
                    {"use": "input-query@1", "with": {"term": "query_pinyin"}},
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    request_path = tmp_path / "composition.json"
    request_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scenario": str(scenario_path.resolve()),
                "catalog_root": str(root.resolve()),
                "text_input_mode": "pinyin-fallback",
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
    return CompositionFixture(root, request_path, scenario_path, tmp_path / "stage")


def test_composes_linear_steps_and_selects_text_mode(
    composition_fixture: CompositionFixture,
) -> None:
    automation = compose_automation_request(
        composition_fixture.load_request(), staging_root=composition_fixture.stage
    )
    assert [step.id for step in automation.steps] == [
        "c001-ensure-home",
        "c002-input-query",
    ]
    assert automation.steps[1].script.read_text("utf-8").endswith(
        'fill "id=search" "${query_pinyin}"\n'
    )
    assert automation.steps[1].component.implementation == "pinyin-fallback"
    assert automation.aggregate_effect == "idempotent"


def test_repeated_components_receive_unique_occurrence_ids(
    composition_fixture: CompositionFixture,
) -> None:
    scenario = yaml.safe_load(composition_fixture.scenario_path.read_text("utf-8"))
    scenario["components"].append(
        {"use": "input-query@1", "with": {"term": "query"}}
    )
    composition_fixture.scenario_path.write_text(
        yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8"
    )
    automation = compose_automation_request(
        composition_fixture.load_request(), staging_root=composition_fixture.stage
    )
    assert [step.id for step in automation.steps] == [
        "c001-ensure-home",
        "c002-input-query",
        "c003-input-query",
    ]


@pytest.mark.parametrize(
    "unsafe", ["${undeclared}", "${query:-fallback}", "$(whoami)", "`whoami`"]
)
def test_rejects_undeclared_or_executable_placeholder_syntax(
    composition_fixture: CompositionFixture, unsafe: str
) -> None:
    composition_fixture.write_action(f'fill "id=search" "{unsafe}"\n')
    with pytest.raises(ContractError, match="component_parameter_invalid"):
        compose_automation_request(
            composition_fixture.load_request(), staging_root=composition_fixture.stage
        )


@pytest.mark.parametrize(
    "mapping", [{}, {"unknown": "query"}, {"term": "unknown"}]
)
def test_rejects_incomplete_or_unknown_parameter_mapping(
    composition_fixture: CompositionFixture, mapping: dict[str, str]
) -> None:
    scenario = yaml.safe_load(composition_fixture.scenario_path.read_text("utf-8"))
    scenario["components"][1]["with"] = mapping
    composition_fixture.scenario_path.write_text(
        yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(ContractError, match="component_parameter_invalid"):
        compose_automation_request(
            composition_fixture.load_request(), staging_root=composition_fixture.stage
        )


def test_missing_requested_and_default_variant_is_rejected(
    composition_fixture: CompositionFixture,
) -> None:
    descriptor = yaml.safe_load(
        composition_fixture.component_path("input-query").read_text("utf-8")
    )
    descriptor["implementations"] = {
        "direct-ime": {"script": "input-direct.ad"}
    }
    composition_fixture.component_path("input-query").write_text(
        yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8"
    )
    with pytest.raises(ContractError, match="component_variant_unavailable"):
        compose_automation_request(
            composition_fixture.load_request(), staging_root=composition_fixture.stage
        )


def test_variant_specific_parameters_are_validated_across_all_implementations(
    composition_fixture: CompositionFixture,
) -> None:
    descriptor_path = composition_fixture.component_path("input-query")
    descriptor = yaml.safe_load(descriptor_path.read_text("utf-8"))
    descriptor["parameters"] = ["query", "query_pinyin"]
    descriptor_path.write_text(yaml.safe_dump(descriptor, sort_keys=False), encoding="utf-8")
    version_root = descriptor_path.parent
    (version_root / "input-direct.ad").write_text(
        'context platform=android\nfill "id=search" "${query}"\n', encoding="utf-8"
    )
    (version_root / "input-pinyin.ad").write_text(
        'context platform=android\nfill "id=search" "${query_pinyin}"\n'
        'wait text "${query}"\n',
        encoding="utf-8",
    )
    scenario = yaml.safe_load(composition_fixture.scenario_path.read_text("utf-8"))
    scenario["components"][1]["with"] = {
        "query": "query",
        "query_pinyin": "query_pinyin",
    }
    composition_fixture.scenario_path.write_text(
        yaml.safe_dump(scenario, sort_keys=False), encoding="utf-8"
    )
    request = json.loads(composition_fixture.request_path.read_text("utf-8"))
    request["text_input_mode"] = "direct-ime"
    composition_fixture.request_path.write_text(json.dumps(request), encoding="utf-8")

    automation = compose_automation_request(
        composition_fixture.load_request(), staging_root=composition_fixture.stage
    )
    assert automation.steps[1].component.implementation == "direct-ime"
    assert "${query}" in automation.steps[1].script.read_text("utf-8")


def test_duplicate_component_parameter_mapping_is_rejected(
    composition_fixture: CompositionFixture,
) -> None:
    composition_fixture.scenario_path.write_text(
        """schema_version: '1.0'
app_id: example-app
id: search
description: Search in an example application.
parameters:
  - name: query
    default: null
    sensitive: false
  - name: query_pinyin
    default: null
    sensitive: false
components:
  - use: input-query@1
    with:
      term: query
      term: query_pinyin
""",
        encoding="utf-8",
    )
    with pytest.raises(ContractError, match="component_parameter_invalid"):
        compose_automation_request(
            composition_fixture.load_request(), staging_root=composition_fixture.stage
        )


def test_composed_bundle_is_self_contained(
    composition_fixture: CompositionFixture, tmp_path: Path
) -> None:
    request = composition_fixture.load_request()
    bundle = compose_bundle(request, (tmp_path / "outputs").resolve())
    catalog_text = str(composition_fixture.root.resolve())
    shutil.rmtree(composition_fixture.root)
    preview = preview_bundle(bundle)
    assert [step["component"]["id"] for step in preview["steps"]] == [
        "ensure-home",
        "input-query",
    ]
    assert catalog_text not in (bundle / "workflow.yaml").read_text("utf-8")
    assert catalog_text not in (bundle / "metadata.json").read_text("utf-8")
