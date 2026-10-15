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
    inspect_placeholders,
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
from .composition_models import AppComponentSpec, CompositionRequest, TextInputMode
from .contracts import ContractError
from .models import (
    AssertionSpec,
    AutomationRequest,
    ComponentLifecycleSpec,
    ComponentRecoveryStepSpec,
    StepSpec,
)
from .renderer import compile_bundle


def _parameter_error(message: str) -> ContractError:
    return ContractError(f"component_parameter_invalid: {message}")


def _select_implementation(
    component: AppComponentSpec, mode: TextInputMode
) -> tuple[str, Path]:
    if mode in component.implementations:
        return mode, component.implementations[mode]
    if "default" in component.implementations:
        return "default", component.implementations["default"]
    raise ContractError(
        f"component_variant_unavailable: {component.id}@{component.version} has no {mode} or default implementation"
    )


def _inspect_tokens(text: str, *, label: str) -> set[str]:
    try:
        return inspect_placeholders(text, label)
    except CompositionContractError as exc:
        raise ContractError(str(exc)) from exc


def _rewrite_text(
    text: str,
    *,
    parameter_map: dict[str, str],
    label: str,
) -> tuple[str, set[str]]:
    try:
        return rewrite_placeholders(text, parameter_map, label)
    except CompositionContractError as exc:
        raise ContractError(str(exc)) from exc


def _rewrite_assertion(
    assertion: AssertionSpec | None,
    *,
    parameter_map: dict[str, str],
    label: str,
) -> tuple[AssertionSpec | None, set[str]]:
    if assertion is None:
        return None, set()
    if assertion.kind != "selector-visible" or assertion.selector is None:
        raise _parameter_error(f"{label} must be a selector-visible assertion")
    selector, tokens = _rewrite_text(
        assertion.selector, parameter_map=parameter_map, label=label
    )
    return replace(assertion, selector=selector), tokens


def _materialize_script(
    source: Path,
    destination: Path,
    *,
    parameter_map: dict[str, str],
    label: str,
) -> tuple[Path, set[str]]:
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _parameter_error(f"cannot read {label}") from exc
    rewritten, tokens = _rewrite_text(
        text, parameter_map=parameter_map, label=label
    )
    destination.write_text(rewritten, encoding="utf-8", newline="\n")
    return destination, tokens


def _implementation_parameter_union(
    component: AppComponentSpec, parameter_map: dict[str, str]
) -> set[str]:
    result: set[str] = set()
    for key, path in component.implementations.items():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise _parameter_error(f"cannot read {component.id} implementation {key}") from exc
        tokens = _inspect_tokens(text, label=f"{component.id} implementation {key}")
        unknown = tokens - set(parameter_map)
        if unknown:
            raise _parameter_error(
                f"{component.id} implementation {key} references undeclared parameters: "
                + ", ".join(sorted(unknown))
            )
        result.update(tokens)
    return result


@dataclass(frozen=True)
class _SelectedImplementation:
    key: str
    path: Path


class MobileCompositionAdapter:
    def __init__(self, request: CompositionRequest, stage: Path) -> None:
        self.request = request
        self.stage = stage

    def validate_component_payload(
        self,
        component: ComponentDefinition[
            AssertionSpec, Path, object
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
        component: ComponentDefinition[AssertionSpec, Path, object],
        request: object,
    ) -> _SelectedImplementation:
        key, path = _select_implementation(component, self.request.text_input_mode)
        return _SelectedImplementation(key=key, path=path)

    def expand_component(
        self,
        component: ComponentDefinition[AssertionSpec, Path, object],
        use: ComponentUse,
        implementation: object,
        occurrence_id: str,
    ) -> ExpandedComponent[StepSpec]:
        if not isinstance(implementation, _SelectedImplementation):
            raise ContractError(
                "component_catalog_invalid: unsupported mobile implementation"
            )
        parameter_map = dict(use.parameter_map)
        used_parameters = _implementation_parameter_union(component, parameter_map)
        occurrence_root = self.stage / occurrence_id
        try:
            occurrence_root.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise ContractError(
                f"component_catalog_invalid: duplicate staging occurrence {occurrence_id}"
            ) from exc

        action, tokens = _materialize_script(
            implementation.path,
            occurrence_root / "action.ad",
            parameter_map=parameter_map,
            label=f"{occurrence_id} action",
        )
        used_parameters.update(tokens)
        precondition, tokens = _rewrite_assertion(
            component.precondition,
            parameter_map=parameter_map,
            label=f"{occurrence_id} precondition",
        )
        used_parameters.update(tokens)
        already_complete, tokens = _rewrite_assertion(
            component.already_complete,
            parameter_map=parameter_map,
            label=f"{occurrence_id} already_complete",
        )
        used_parameters.update(tokens)
        postcondition, tokens = _rewrite_assertion(
            component.postcondition,
            parameter_map=parameter_map,
            label=f"{occurrence_id} postcondition",
        )
        used_parameters.update(tokens)
        if postcondition is None:
            raise ContractError(
                "component_catalog_invalid: component postcondition is required"
            )

        recovery = None
        mobile_recovery = component.recovery
        if mobile_recovery is not None:
            recovery_script, tokens = _materialize_script(
                mobile_recovery.script,
                occurrence_root / "recovery.ad",
                parameter_map=parameter_map,
                label=f"{occurrence_id} recovery",
            )
            used_parameters.update(tokens)
            recovery_postcondition, tokens = _rewrite_assertion(
                mobile_recovery.postcondition,
                parameter_map=parameter_map,
                label=f"{occurrence_id} recovery postcondition",
            )
            used_parameters.update(tokens)
            if recovery_postcondition is None:
                raise ContractError(
                    "component_catalog_invalid: recovery postcondition is required"
                )
            recovery = ComponentRecoveryStepSpec(
                script=recovery_script,
                postcondition=recovery_postcondition,
            )

        lifecycle = ComponentLifecycleSpec(
            app_id=component.app_id,
            component_id=component.id,
            version=component.version,
            occurrence_id=occurrence_id,
            descriptor_sha256=component.descriptor_sha256,
            text_input_mode=self.request.text_input_mode,
            implementation=implementation.key,
            parameter_map=parameter_map,
            precondition=precondition,
            already_complete=already_complete,
            recovery=recovery,
        )
        step = StepSpec(
            id=occurrence_id,
            runner="agent-device",
            effect=component.effect,
            timeout_seconds=component.timeout_seconds,
            script=action,
            action=None,
            template=None,
            threshold=None,
            assertion=postcondition,
            component=lifecycle,
        )
        return ExpandedComponent(
            occurrence_id=occurrence_id,
            component_id=component.id,
            version=component.version,
            descriptor_sha256=component.descriptor_sha256,
            effect=component.effect,
            parameter_map=parameter_map,
            used_parameters=frozenset(used_parameters),
            implementation=implementation.key,
            asset_sha256={},
            payload=step,
        )

    def collect_assets(
        self,
        component: ComponentDefinition[AssertionSpec, Path, object],
        implementation: object,
    ) -> Mapping[str, str]:
        if not isinstance(implementation, _SelectedImplementation):
            raise ContractError(
                "component_catalog_invalid: unsupported mobile implementation"
            )
        assets = {
            "action.ad": hashlib.sha256(implementation.path.read_bytes()).hexdigest()
        }
        mobile_recovery = component.recovery
        if mobile_recovery is not None:
            assets["recovery.ad"] = hashlib.sha256(
                mobile_recovery.script.read_bytes()
            ).hexdigest()
        return assets

    def build_platform_request(
        self,
        scenario: ScenarioDefinition[object],
        expanded: tuple[ExpandedComponent[StepSpec], ...],
        provenance: tuple[ComponentProvenance, ...],
        aggregate_effect: Effect,
        request: object,
    ) -> AutomationRequest:
        return AutomationRequest(
            source=self.request.source,
            name=scenario.id,
            description=scenario.description,
            source_surface=self.request.source_surface,
            target_platform=self.request.target_platform,
            device=self.request.device,
            parameters=scenario.parameters,
            steps=tuple(item.payload for item in expanded),
            decision=self.request.decision,
            effective_path_confirmed=self.request.effective_path_confirmed,
            aggregate_effect=aggregate_effect,
        )


def compose_automation_request(
    request: CompositionRequest, *, staging_root: Path
) -> AutomationRequest:
    scenario = load_scenario(request.scenario, catalog_root=request.catalog_root)
    stage = staging_root.resolve(strict=False)
    if stage.exists() and not stage.is_dir():
        raise ContractError("component_catalog_invalid: staging_root must be a directory")
    stage.mkdir(parents=True, exist_ok=True)
    adapter = MobileCompositionAdapter(request, stage)

    def load_exact_component(use: ComponentUse) -> AppComponentSpec:
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


def compose_bundle(request: CompositionRequest, output_root: Path) -> Path:
    with TemporaryDirectory(prefix="mobile-compose-") as temporary:
        automation = compose_automation_request(
            request, staging_root=Path(temporary)
        )
        return compile_bundle(automation, output_root)
