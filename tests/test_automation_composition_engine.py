from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import pytest

from agent_workflow_hub.automation_composition.engine import (
    ExpandedComponent,
    compose_scenario,
    rewrite_placeholders,
)
from agent_workflow_hub.automation_composition.models import (
    ComponentDefinition,
    ComponentProvenance,
    ComponentUse,
    Effect,
    ParameterDeclaration,
    ScenarioDefinition,
)


@dataclass(frozen=True)
class FakeImplementation:
    name: str
    text: str


@dataclass(frozen=True)
class FakePayload:
    text: str


@dataclass(frozen=True)
class FakePlan:
    expanded: tuple[ExpandedComponent[FakePayload], ...]
    provenance: tuple[ComponentProvenance, ...]
    aggregate_effect: Effect
    used_scenario_parameters: tuple[str, ...]


class FakeAdapter:
    def __init__(
        self,
        catalog_root: Path,
        components: Mapping[str, ComponentDefinition[object, FakeImplementation, object]],
    ) -> None:
        self.catalog_root = catalog_root
        self.components = dict(components)
        self.used_parameters_by_component = {
            component_id: set(component.parameters)
            for component_id, component in components.items()
        }

    def load_component(
        self, use: ComponentUse
    ) -> ComponentDefinition[object, FakeImplementation, object]:
        component = self.components[use.component_id]
        if component.version != use.version:
            raise ValueError("component_version_mismatch")
        return component

    def validate_component_payload(
        self,
        component: ComponentDefinition[object, FakeImplementation, object],
        catalog_root: Path,
    ) -> None:
        assert component.source.is_relative_to(catalog_root)

    def select_implementation(
        self,
        component: ComponentDefinition[object, FakeImplementation, object],
        request: object,
    ) -> FakeImplementation:
        return component.implementations["default"]

    def expand_component(
        self,
        component: ComponentDefinition[object, FakeImplementation, object],
        use: ComponentUse,
        implementation: FakeImplementation,
        occurrence_id: str,
    ) -> ExpandedComponent[FakePayload]:
        text, _ = rewrite_placeholders(
            implementation.text,
            use.parameter_map,
            f"{occurrence_id} action",
        )
        return ExpandedComponent(
            occurrence_id=occurrence_id,
            component_id=component.id,
            version=component.version,
            descriptor_sha256=component.descriptor_sha256,
            effect=component.effect,
            parameter_map=use.parameter_map,
            used_parameters=frozenset(
                self.used_parameters_by_component[component.id]
            ),
            implementation=implementation.name,
            asset_sha256={},
            payload=FakePayload(text=text),
        )

    def collect_assets(
        self,
        component: ComponentDefinition[object, FakeImplementation, object],
        implementation: FakeImplementation,
    ) -> Mapping[str, str]:
        return {"action.txt": component.descriptor_sha256}

    def build_platform_request(
        self,
        scenario: ScenarioDefinition[ParameterDeclaration],
        expanded: tuple[ExpandedComponent[FakePayload], ...],
        provenance: tuple[ComponentProvenance, ...],
        aggregate_effect: Effect,
        request: object,
    ) -> FakePlan:
        used = tuple(
            sorted(
                {
                    target
                    for item in expanded
                    for target in item.parameter_map.values()
                }
            )
        )
        return FakePlan(expanded, provenance, aggregate_effect, used)


@pytest.fixture
def fake_catalog(
    tmp_path: Path,
) -> tuple[
    Path,
    dict[str, ComponentDefinition[object, FakeImplementation, object]],
]:
    root = (tmp_path / "catalog").resolve()
    root.mkdir()

    def component(
        component_id: str,
        effect: Effect,
        parameters: tuple[str, ...],
        text: str,
    ) -> ComponentDefinition[object, FakeImplementation, object]:
        source = root / "components" / component_id / "v001" / "component.yaml"
        source.parent.mkdir(parents=True)
        source.write_text("fixture", encoding="utf-8")
        return ComponentDefinition(
            source=source,
            descriptor_sha256=("a" if component_id == "home" else "b") * 64,
            app_id="demo",
            id=component_id,
            version=1,
            effect=effect,
            timeout_seconds=5.0,
            parameters=parameters,
            precondition=None,
            already_complete=None,
            implementations={
                "default": FakeImplementation(name="default", text=text)
            },
            recovery=None,
            postcondition=object(),
        )

    return root, {
        "home": component("home", "none", (), "home"),
        "search": component("search", "read", ("query",), "${query}"),
    }


@pytest.fixture
def fake_scenario(tmp_path: Path) -> ScenarioDefinition[ParameterDeclaration]:
    return ScenarioDefinition(
        source=tmp_path / "scenario.yaml",
        app_id="demo",
        id="demo-flow",
        description="demo flow",
        parameters=(ParameterDeclaration("term", "text", None, False),),
        components=(
            ComponentUse("home", 1, {}),
            ComponentUse("search", 1, {"query": "term"}),
        ),
    )


def test_placeholder_rewrite_rejects_executable_malformed_and_unknown_tokens() -> None:
    assert rewrite_placeholders(
        "${query}", {"query": "term"}, "action"
    ) == ("${term}", {"query"})

    for value in ("$(whoami)", "`whoami`", "${bad-token}", "${missing}"):
        with pytest.raises(ValueError, match="component_parameter_invalid"):
            rewrite_placeholders(value, {"query": "term"}, "action")


def test_compose_preserves_order_and_requires_every_mapping_to_be_used(
    fake_scenario: ScenarioDefinition[ParameterDeclaration],
    fake_catalog: tuple[
        Path,
        dict[str, ComponentDefinition[object, FakeImplementation, object]],
    ],
) -> None:
    catalog_root, fake_components = fake_catalog
    adapter = FakeAdapter(catalog_root, fake_components)

    plan = compose_scenario(
        fake_scenario,
        object(),
        adapter.catalog_root,
        adapter.load_component,
        adapter,
    )

    assert [item.occurrence_id for item in plan.expanded] == [
        "c001-home",
        "c002-search",
    ]
    assert plan.aggregate_effect == "read"
    assert plan.used_scenario_parameters == ("term",)

    adapter.used_parameters_by_component["search"] = set()
    with pytest.raises(ValueError, match="unused mappings"):
        compose_scenario(
            fake_scenario,
            object(),
            adapter.catalog_root,
            adapter.load_component,
            adapter,
        )


def test_duplicate_component_references_receive_position_based_ids(
    fake_scenario: ScenarioDefinition[ParameterDeclaration],
    fake_catalog: tuple[
        Path,
        dict[str, ComponentDefinition[object, FakeImplementation, object]],
    ],
) -> None:
    catalog_root, fake_components = fake_catalog
    duplicate = ScenarioDefinition(
        source=fake_scenario.source,
        app_id=fake_scenario.app_id,
        id=fake_scenario.id,
        description=fake_scenario.description,
        parameters=fake_scenario.parameters,
        components=(ComponentUse("home", 1, {}), ComponentUse("home", 1, {})),
    )
    adapter = FakeAdapter(catalog_root, fake_components)

    plan = compose_scenario(
        duplicate,
        object(),
        catalog_root,
        adapter.load_component,
        adapter,
    )

    assert [item.occurrence_id for item in plan.expanded] == [
        "c001-home",
        "c002-home",
    ]
