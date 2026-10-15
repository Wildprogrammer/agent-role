from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Mapping

from agent_workflow_hub.automation_composition.contracts import (
    CompositionContractError,
)
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
    ScenarioDefinition,
)

from .composition_contracts import (
    component_descriptor_path,
    load_component,
    load_scenario,
)
from .composition_models import (
    DesktopAssertionSpec,
    DesktopCompositionRequest,
    DesktopImplementationSpec,
    DesktopRecoverySpec,
)
from .contracts import ContractError
from .models import (
    AssertionSpec,
    AutomationRequest,
    DesktopComponentLifecycle,
    DesktopComponentRecovery,
    DesktopComposition,
    ParameterSpec,
    StepSpec,
)
from .renderer import compile_bundle


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assertion(value: DesktopAssertionSpec | None, app_id: str) -> AssertionSpec | None:
    if value is None:
        return None
    return AssertionSpec(
        app=app_id,
        template=value.template,
        threshold=value.threshold,
        timeout_seconds=value.timeout_seconds,
        stable_seconds=value.stable_seconds,
    )


def _rewrite_value(
    value: object,
    mapping: Mapping[str, str],
    label: str,
) -> tuple[object, set[str]]:
    if isinstance(value, str):
        return rewrite_placeholders(value, mapping, label)
    if isinstance(value, list):
        rewritten: list[object] = []
        used: set[str] = set()
        for index, item in enumerate(value):
            new_item, tokens = _rewrite_value(item, mapping, f"{label}[{index}]")
            rewritten.append(new_item)
            used.update(tokens)
        return rewritten, used
    if isinstance(value, tuple):
        rewritten, used = _rewrite_value(list(value), mapping, label)
        return tuple(rewritten), used
    return value, set()


def _rewrite_steps(
    steps: tuple[StepSpec, ...],
    *,
    occurrence_id: str,
    mapping: Mapping[str, str],
) -> tuple[tuple[StepSpec, ...], set[str]]:
    result: list[StepSpec] = []
    used: set[str] = set()
    for index, step in enumerate(steps, start=1):
        values: dict[str, object] = {}
        for key, value in step.values.items():
            rewritten, tokens = _rewrite_value(
                value,
                mapping,
                f"{occurrence_id} step {index}.{key}",
            )
            values[key] = rewritten
            used.update(tokens)
        result.append(
            replace(
                step,
                id=f"{occurrence_id}-s{index:03d}",
                values=values,
                component=None,
            )
        )
    return tuple(result), used


def _image_paths(
    component: ComponentDefinition[
        DesktopAssertionSpec,
        DesktopImplementationSpec,
        DesktopRecoverySpec,
    ],
    implementation: DesktopImplementationSpec,
) -> dict[str, Path]:
    paths: dict[str, Path] = {}

    def add_assertion(label: str, assertion: DesktopAssertionSpec | None) -> None:
        if assertion is not None:
            paths[label] = assertion.template

    def add_steps(prefix: str, steps: tuple[StepSpec, ...]) -> None:
        for index, step in enumerate(steps, start=1):
            for key in ("template", "from_template", "to_template"):
                value = step.values.get(key)
                if value is not None:
                    paths[f"{prefix}/s{index:03d}/{key}"] = Path(str(value))

    add_assertion("precondition", component.precondition)
    add_assertion("already-complete", component.already_complete)
    add_assertion("postcondition", component.postcondition)
    add_steps("action", implementation.steps)
    if component.recovery is not None:
        add_steps("recovery", component.recovery.steps)
        add_assertion("recovery/postcondition", component.recovery.postcondition)
    return paths


@dataclass(frozen=True)
class _SelectedImplementation:
    key: str
    value: DesktopImplementationSpec


class DesktopCompositionAdapter:
    def __init__(self, request: DesktopCompositionRequest) -> None:
        self.request = request

    def validate_component_payload(
        self,
        component: ComponentDefinition[
            DesktopAssertionSpec,
            DesktopImplementationSpec,
            DesktopRecoverySpec,
        ],
        catalog_root: Path,
    ) -> None:
        try:
            component.source.relative_to(catalog_root)
        except ValueError as exc:
            raise ContractError(
                "component_catalog_invalid: component descriptor must be inside the catalog"
            ) from exc

    def select_implementation(
        self,
        component: ComponentDefinition[
            DesktopAssertionSpec,
            DesktopImplementationSpec,
            DesktopRecoverySpec,
        ],
        request: object,
    ) -> _SelectedImplementation:
        implementation = component.implementations.get("windows-airtest")
        if implementation is None:
            raise ContractError(
                f"component_variant_unavailable: {component.id}@{component.version} has no windows-airtest implementation"
            )
        return _SelectedImplementation("windows-airtest", implementation)

    def expand_component(
        self,
        component: ComponentDefinition[
            DesktopAssertionSpec,
            DesktopImplementationSpec,
            DesktopRecoverySpec,
        ],
        use: ComponentUse,
        implementation: object,
        occurrence_id: str,
    ) -> ExpandedComponent[tuple[StepSpec, ...]]:
        if not isinstance(implementation, _SelectedImplementation):
            raise ContractError("component_catalog_invalid: unsupported desktop implementation")
        steps, used = _rewrite_steps(
            implementation.value.steps,
            occurrence_id=occurrence_id,
            mapping=use.parameter_map,
        )
        recovery = None
        if component.recovery is not None:
            recovery_steps, recovery_used = _rewrite_steps(
                component.recovery.steps,
                occurrence_id=f"{occurrence_id}-recovery",
                mapping=use.parameter_map,
            )
            used.update(recovery_used)
            recovery = DesktopComponentRecovery(
                steps=recovery_steps,
                postcondition=_assertion(
                    component.recovery.postcondition,
                    component.app_id,
                ),
            )
        postcondition = _assertion(component.postcondition, component.app_id)
        if postcondition is None:
            raise ContractError("component_catalog_invalid: postcondition is required")
        lifecycle = DesktopComponentLifecycle(
            app_id=component.app_id,
            component_id=component.id,
            version=component.version,
            occurrence_id=occurrence_id,
            descriptor_sha256=component.descriptor_sha256,
            implementation=implementation.key,
            parameter_map=dict(use.parameter_map),
            precondition=_assertion(component.precondition, component.app_id),
            already_complete=_assertion(component.already_complete, component.app_id),
            recovery=recovery,
            postcondition=postcondition,
        )
        steps = tuple(replace(step, component=lifecycle) for step in steps)
        return ExpandedComponent(
            occurrence_id=occurrence_id,
            component_id=component.id,
            version=component.version,
            descriptor_sha256=component.descriptor_sha256,
            effect=component.effect,
            parameter_map=dict(use.parameter_map),
            used_parameters=frozenset(used),
            implementation=implementation.key,
            asset_sha256={},
            payload=steps,
        )

    def collect_assets(
        self,
        component: ComponentDefinition[
            DesktopAssertionSpec,
            DesktopImplementationSpec,
            DesktopRecoverySpec,
        ],
        implementation: object,
    ) -> Mapping[str, str]:
        if not isinstance(implementation, _SelectedImplementation):
            raise ContractError("component_catalog_invalid: unsupported desktop implementation")
        return {
            label: _sha256(path)
            for label, path in sorted(
                _image_paths(component, implementation.value).items()
            )
        }

    def build_platform_request(
        self,
        scenario: ScenarioDefinition[object],
        expanded: tuple[ExpandedComponent[tuple[StepSpec, ...]], ...],
        provenance: tuple[ComponentProvenance, ...],
        aggregate_effect: Effect,
        request: object,
    ) -> AutomationRequest:
        parameters = tuple(
            ParameterSpec(
                name=parameter.name,
                kind=parameter.kind,
                default=parameter.default,
                sensitive=parameter.sensitive,
            )
            for parameter in scenario.parameters
        )
        return AutomationRequest(
            source=self.request.source,
            name=scenario.id,
            description=scenario.description,
            source_surface=self.request.source_surface,
            target_platform=self.request.target_platform,
            apps=self.request.apps,
            parameters=parameters,
            steps=tuple(
                step for component in expanded for step in component.payload
            ),
            success_assertion=self.request.success_assertion,
            decision=self.request.decision,
            effective_path_confirmed=self.request.effective_path_confirmed,
            aggregate_effect=aggregate_effect,
            composition=DesktopComposition(
                scenario_id=scenario.id,
                provenance=provenance,
            ),
        )


def compose_automation_request(
    request: DesktopCompositionRequest,
    *,
    staging_root: Path,
) -> AutomationRequest:
    staging_root.resolve(strict=False).mkdir(parents=True, exist_ok=True)
    scenario = load_scenario(request.scenario, catalog_root=request.catalog_root)
    adapter = DesktopCompositionAdapter(request)

    def load_exact_component(
        use: ComponentUse,
    ) -> ComponentDefinition[
        DesktopAssertionSpec,
        DesktopImplementationSpec,
        DesktopRecoverySpec,
    ]:
        return load_component(
            component_descriptor_path(
                request.catalog_root,
                use.component_id,
                use.version,
            ),
            catalog_root=request.catalog_root,
        )

    try:
        return compose_scenario(
            scenario,
            request,
            request.catalog_root,
            load_exact_component,
            adapter,
        )
    except CompositionContractError as exc:
        raise ContractError(str(exc)) from exc


def compose_bundle(request: DesktopCompositionRequest, output_root: Path) -> Path:
    with TemporaryDirectory(prefix="desktop-compose-") as temporary:
        automation = compose_automation_request(
            request,
            staging_root=Path(temporary),
        )
        return compile_bundle(automation, output_root)
