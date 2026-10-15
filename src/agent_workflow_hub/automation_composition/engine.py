from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Generic, Protocol, TypeVar

from .contracts import (
    IDENTIFIER,
    CompositionContractError,
    validate_parameter_mapping,
)
from .models import (
    EFFECT_RANK,
    ComponentDefinition,
    ComponentProvenance,
    ComponentUse,
    Effect,
    ScenarioDefinition,
)


PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
BRACED_TOKEN = re.compile(r"\$\{([^}]*)\}")

T = TypeVar("T")
O = TypeVar("O")


def _parameter_error(message: str) -> CompositionContractError:
    return CompositionContractError(f"component_parameter_invalid: {message}")


def inspect_placeholders(text: str, label: str) -> set[str]:
    if not isinstance(text, str):
        raise _parameter_error(f"{label} must be text")
    if "`" in text or "$(" in text:
        raise _parameter_error(f"{label} contains executable placeholder syntax")
    tokens = BRACED_TOKEN.findall(text)
    if "${" in text and len(tokens) != text.count("${"):
        raise _parameter_error(f"{label} contains malformed placeholder syntax")
    for token in tokens:
        if IDENTIFIER.fullmatch(token) is None:
            raise _parameter_error(f"{label} contains unsupported placeholder syntax")
    return set(tokens)


def rewrite_placeholders(
    text: str,
    mapping: Mapping[str, str],
    label: str,
) -> tuple[str, set[str]]:
    tokens = inspect_placeholders(text, label)
    unknown = tokens - set(mapping)
    if unknown:
        raise _parameter_error(
            f"{label} references undeclared component parameters: "
            + ", ".join(sorted(unknown))
        )
    for target in mapping.values():
        if not isinstance(target, str) or IDENTIFIER.fullmatch(target) is None:
            raise _parameter_error(
                f"{label} maps to an invalid scenario parameter"
            )
    rewritten = PLACEHOLDER.sub(
        lambda match: "${" + mapping[match.group(1)] + "}",
        text,
    )
    return rewritten, tokens


@dataclass(frozen=True)
class ExpandedComponent(Generic[T]):
    occurrence_id: str
    component_id: str
    version: int
    descriptor_sha256: str
    effect: Effect
    parameter_map: Mapping[str, str]
    used_parameters: frozenset[str]
    implementation: str
    asset_sha256: Mapping[str, str]
    payload: T


class CompositionAdapter(Protocol[T, O]):
    def validate_component_payload(
        self,
        component: ComponentDefinition[Any, Any, Any],
        catalog_root: Path,
    ) -> None: ...

    def select_implementation(
        self,
        component: ComponentDefinition[Any, Any, Any],
        request: object,
    ) -> object: ...

    def expand_component(
        self,
        component: ComponentDefinition[Any, Any, Any],
        use: ComponentUse,
        implementation: object,
        occurrence_id: str,
    ) -> ExpandedComponent[T]: ...

    def collect_assets(
        self,
        component: ComponentDefinition[Any, Any, Any],
        implementation: object,
    ) -> Mapping[str, str]: ...

    def build_platform_request(
        self,
        scenario: ScenarioDefinition[Any],
        expanded: tuple[ExpandedComponent[T], ...],
        provenance: tuple[ComponentProvenance, ...],
        aggregate_effect: Effect,
        request: object,
    ) -> O: ...


def compose_scenario(
    scenario: ScenarioDefinition[Any],
    request: object,
    catalog_root: Path,
    load_component: Callable[
        [ComponentUse], ComponentDefinition[Any, Any, Any]
    ],
    adapter: CompositionAdapter[T, O],
) -> O:
    if not scenario.components:
        raise CompositionContractError(
            "component_catalog_invalid: scenario must contain at least one component"
        )
    scenario_parameter_names = tuple(parameter.name for parameter in scenario.parameters)
    expanded: list[ExpandedComponent[T]] = []
    provenance: list[ComponentProvenance] = []

    for position, original_use in enumerate(scenario.components, start=1):
        component = load_component(original_use)
        if component.app_id != scenario.app_id:
            raise CompositionContractError(
                "component_catalog_invalid: component app_id mismatch"
            )
        parameter_map = validate_parameter_mapping(
            component_parameters=component.parameters,
            mapping=original_use.parameter_map,
            scenario_parameters=scenario_parameter_names,
        )
        use = replace(original_use, parameter_map=parameter_map)
        occurrence_id = f"c{position:03d}-{component.id}"
        adapter.validate_component_payload(component, catalog_root)
        implementation = adapter.select_implementation(component, request)
        item = adapter.expand_component(
            component,
            use,
            implementation,
            occurrence_id,
        )

        expected_parameters = set(component.parameters)
        used_parameters = set(item.used_parameters)
        unknown = used_parameters - expected_parameters
        unused = expected_parameters - used_parameters
        if unknown:
            raise _parameter_error(
                f"{component.id}@{component.version} reports unknown mappings: "
                + ", ".join(sorted(unknown))
            )
        if unused:
            raise _parameter_error(
                f"{component.id}@{component.version} has unused mappings: "
                + ", ".join(sorted(unused))
            )

        assets = dict(sorted(adapter.collect_assets(component, implementation).items()))
        item = replace(
            item,
            occurrence_id=occurrence_id,
            component_id=component.id,
            version=component.version,
            descriptor_sha256=component.descriptor_sha256,
            effect=component.effect,
            parameter_map=parameter_map,
            asset_sha256=assets,
        )
        expanded.append(item)
        provenance.append(
            ComponentProvenance(
                occurrence_id=occurrence_id,
                component_id=component.id,
                version=component.version,
                descriptor_sha256=component.descriptor_sha256,
                implementation=item.implementation,
                parameter_map=parameter_map,
                asset_sha256=assets,
            )
        )

    aggregate = max(
        (item.effect for item in expanded),
        key=EFFECT_RANK.__getitem__,
    )
    return adapter.build_platform_request(
        scenario,
        tuple(expanded),
        tuple(provenance),
        aggregate,
        request,
    )
