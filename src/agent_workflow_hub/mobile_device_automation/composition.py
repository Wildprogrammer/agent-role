from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from .composition_contracts import (
    component_descriptor_path,
    load_component,
    load_scenario,
)
from .composition_models import AppComponentSpec, CompositionRequest, TextInputMode
from .contracts import ContractError, EFFECT_RANK, IDENTIFIER
from .models import (
    AssertionSpec,
    AutomationRequest,
    ComponentLifecycleSpec,
    ComponentRecoveryStepSpec,
    StepSpec,
)
from .renderer import compile_bundle


PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
BRACED_TOKEN = re.compile(r"\$\{([^}]*)\}")


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
    if "`" in text or "$(" in text:
        raise _parameter_error(f"{label} contains executable placeholder syntax")
    tokens = BRACED_TOKEN.findall(text)
    if "${" in text and len(tokens) != text.count("${"):
        raise _parameter_error(f"{label} contains malformed placeholder syntax")
    for token in tokens:
        if not IDENTIFIER.fullmatch(token):
            raise _parameter_error(f"{label} contains unsupported placeholder syntax")
    return set(tokens)


def _rewrite_text(
    text: str,
    *,
    parameter_map: dict[str, str],
    label: str,
) -> tuple[str, set[str]]:
    tokens = _inspect_tokens(text, label=label)
    unknown = tokens - set(parameter_map)
    if unknown:
        raise _parameter_error(
            f"{label} references undeclared component parameters: {', '.join(sorted(unknown))}"
        )
    rewritten = PLACEHOLDER.sub(
        lambda match: "${" + parameter_map[match.group(1)] + "}", text
    )
    return rewritten, tokens


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


def compose_automation_request(
    request: CompositionRequest, *, staging_root: Path
) -> AutomationRequest:
    scenario = load_scenario(request.scenario, catalog_root=request.catalog_root)
    stage = staging_root.resolve(strict=False)
    if stage.exists() and not stage.is_dir():
        raise ContractError("component_catalog_invalid: staging_root must be a directory")
    stage.mkdir(parents=True, exist_ok=True)
    scenario_parameters = {parameter.name for parameter in scenario.parameters}
    steps: list[StepSpec] = []

    for position, use in enumerate(scenario.components, start=1):
        component = load_component(
            component_descriptor_path(
                request.catalog_root, use.component_id, use.version
            ),
            catalog_root=request.catalog_root,
        )
        if component.app_id != scenario.app_id:
            raise ContractError("component_catalog_invalid: component app_id mismatch")
        parameter_map = dict(sorted(use.parameter_map.items()))
        expected_parameters = set(component.parameters)
        if set(parameter_map) != expected_parameters:
            raise _parameter_error(
                f"{component.id}@{component.version} mapping must cover exactly its declared parameters"
            )
        if not set(parameter_map.values()).issubset(scenario_parameters):
            raise _parameter_error("mapping references an unknown scenario parameter")

        used_parameters = _implementation_parameter_union(component, parameter_map)

        implementation_key, implementation = _select_implementation(
            component, request.text_input_mode
        )
        occurrence_id = f"c{position:03d}-{component.id}"
        occurrence_root = stage / occurrence_id
        try:
            occurrence_root.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise ContractError(
                f"component_catalog_invalid: duplicate staging occurrence {occurrence_id}"
            ) from exc

        action, tokens = _materialize_script(
            implementation,
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
            raise ContractError("component_catalog_invalid: component postcondition is required")

        recovery = None
        if component.recovery is not None:
            recovery_script, tokens = _materialize_script(
                component.recovery.script,
                occurrence_root / "recovery.ad",
                parameter_map=parameter_map,
                label=f"{occurrence_id} recovery",
            )
            used_parameters.update(tokens)
            recovery_postcondition, tokens = _rewrite_assertion(
                component.recovery.postcondition,
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

        if used_parameters != expected_parameters:
            unused = expected_parameters - used_parameters
            raise _parameter_error(
                f"{component.id}@{component.version} has unused mappings: {', '.join(sorted(unused))}"
            )

        lifecycle = ComponentLifecycleSpec(
            app_id=component.app_id,
            component_id=component.id,
            version=component.version,
            occurrence_id=occurrence_id,
            descriptor_sha256=component.descriptor_sha256,
            text_input_mode=request.text_input_mode,
            implementation=implementation_key,
            parameter_map=parameter_map,
            precondition=precondition,
            already_complete=already_complete,
            recovery=recovery,
        )
        steps.append(
            StepSpec(
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
        )

    aggregate_effect = max(
        (step.effect for step in steps), key=EFFECT_RANK.__getitem__
    )
    return AutomationRequest(
        source=request.source,
        name=scenario.id,
        description=scenario.description,
        source_surface=request.source_surface,
        target_platform=request.target_platform,
        device=request.device,
        parameters=scenario.parameters,
        steps=tuple(steps),
        decision=request.decision,
        effective_path_confirmed=request.effective_path_confirmed,
        aggregate_effect=aggregate_effect,
    )


def compose_bundle(request: CompositionRequest, output_root: Path) -> Path:
    with TemporaryDirectory(prefix="mobile-compose-") as temporary:
        automation = compose_automation_request(
            request, staging_root=Path(temporary)
        )
        return compile_bundle(automation, output_root)
