from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import yaml

from agent_workflow_hub.automation_composition.catalog import (
    component_descriptor_path,
    resolve_inside,
)
from agent_workflow_hub.automation_composition.contracts import (
    CompositionContractError,
    parse_component_reference,
    validate_parameter_mapping,
)
from agent_workflow_hub.automation_composition.engine import inspect_placeholders
from agent_workflow_hub.automation_composition.models import (
    ComponentDefinition,
    ComponentUse,
    ScenarioDefinition,
)

from .composition_models import (
    DesktopAssertionSpec,
    DesktopCompositionRequest,
    DesktopImplementationSpec,
    DesktopRecoverySpec,
)
from .contracts import (
    ACTIONS,
    EFFECT_RANK,
    IMAGE_ACTIONS,
    SOURCE_SURFACES,
    STEP_FIELDS,
    ContractError,
    _app,
    _assertion,
    _identifier,
    _parameter,
    _step,
    _text,
)
from .models import ParameterSpec, StepSpec


COMPONENT_FIELDS = {
    "schema_version",
    "platform",
    "app_id",
    "id",
    "version",
    "effect",
    "timeout_seconds",
    "parameters",
    "precondition",
    "already_complete",
    "implementations",
    "recovery",
    "postcondition",
}
IMPLEMENTATION_FIELDS = {"steps"}
RECOVERY_FIELDS = {"steps", "postcondition"}
ASSERTION_FIELDS = {
    "kind",
    "template",
    "threshold",
    "timeout_seconds",
    "stable_seconds",
}
SCENARIO_FIELDS = {
    "schema_version",
    "app_id",
    "id",
    "description",
    "parameters",
    "components",
}
COMPONENT_USE_FIELDS = {"use", "with"}
REQUEST_FIELDS = {
    "schema_version",
    "scenario",
    "catalog_root",
    "source_surface",
    "target_platform",
    "apps",
    "success_assertion",
    "decision",
    "effective_path_confirmed",
}
COMPONENT_STEP_FIELDS = STEP_FIELDS - {"id", "app"}


class _DuplicateKeyError(yaml.YAMLError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise _DuplicateKeyError(f"duplicate mapping key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _invalid(message: str, *, cause: Exception | None = None) -> ContractError:
    error = ContractError(f"component_catalog_invalid: {message}")
    if cause is not None:
        error.__cause__ = cause
    return error


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _invalid(f"{label} must be an object")
    return value


def _closed(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _invalid(f"unknown field in {label}: {', '.join(unknown)}")


def _positive_number(value: Any, label: str, default: float) -> float:
    number = default if value is None else value
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or not math.isfinite(number)
        or number <= 0
    ):
        raise _invalid(f"{label} must be a finite number greater than zero")
    return float(number)


def _threshold(value: Any, label: str, default: float = 0.8) -> float:
    number = default if value is None else value
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or not math.isfinite(number)
        or not 0 < number <= 1
    ):
        raise _invalid(f"{label} must be greater than zero and at most one")
    return float(number)


def _load_yaml(path: Path, label: str) -> tuple[Path, dict[str, Any], bytes]:
    try:
        source = path.resolve(strict=True)
        raw = source.read_bytes()
        value = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
    except _DuplicateKeyError as exc:
        raise ContractError(f"component_parameter_invalid: {exc}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _invalid(f"cannot load {label}", cause=exc) from exc
    return source, _mapping(value, label), raw


def _absolute_file(value: Any, label: str, *, directory: bool = False) -> Path:
    raw = value if isinstance(value, Path) else Path(_text(value, label))
    if not raw.is_absolute():
        raise _invalid(f"{label} must be absolute")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise _invalid(f"{label} must exist", cause=exc) from exc
    if directory and not resolved.is_dir():
        raise _invalid(f"{label} must be a directory")
    if not directory and not resolved.is_file():
        raise _invalid(f"{label} must be a file")
    return resolved


def _component_image(value: Any, version_root: Path, label: str) -> Path:
    raw = Path(_text(value, label))
    if raw.is_absolute():
        raise ContractError(
            f"component_asset_outside_catalog: {label} must be relative"
        )
    try:
        path = resolve_inside(version_root / raw, version_root, label)
    except CompositionContractError as exc:
        raise ContractError(str(exc)) from exc
    if not path.is_file() or path.suffix.casefold() not in {".png", ".jpg", ".jpeg"}:
        raise _invalid(f"{label} must be an existing PNG or JPEG")
    return path


def _desktop_assertion(
    value: Any,
    version_root: Path,
    label: str,
) -> DesktopAssertionSpec:
    item = _mapping(value, label)
    _closed(item, ASSERTION_FIELDS, label)
    if item.get("kind") != "image-visible":
        raise _invalid(f"{label}.kind must equal image-visible")
    return DesktopAssertionSpec(
        kind="image-visible",
        template=_component_image(item.get("template"), version_root, f"{label}.template"),
        threshold=_threshold(item.get("threshold"), f"{label}.threshold"),
        timeout_seconds=_positive_number(
            item.get("timeout_seconds"), f"{label}.timeout_seconds", 10
        ),
        stable_seconds=_positive_number(
            item.get("stable_seconds"), f"{label}.stable_seconds", 0.5
        ),
    )


def _component_step(
    value: Any,
    *,
    index: int,
    app_id: str,
    component_effect: str,
    parameters: tuple[str, ...],
    version_root: Path,
) -> tuple[StepSpec, set[str]]:
    item = _mapping(value, f"component step {index}")
    _closed(item, COMPONENT_STEP_FIELDS, f"component step {index}")
    action = item.get("action")
    if action not in ACTIONS or action == "manual-step":
        raise _invalid(f"component step {index} has unsupported action")
    effect = item.get("effect", component_effect)
    if effect not in EFFECT_RANK:
        raise _invalid(f"component step {index} has unsupported effect")
    if EFFECT_RANK[effect] > EFFECT_RANK[component_effect]:
        raise _invalid(f"component step {index} effect exceeds component effect")

    normalized = dict(item)
    normalized.update({"id": f"s{index:03d}", "app": app_id, "effect": effect})
    if action in IMAGE_ACTIONS:
        normalized["template"] = str(
            _component_image(
                item.get("template"), version_root, f"component step {index}.template"
            )
        )
    elif action == "drag-image":
        for key in ("from_template", "to_template"):
            normalized[key] = str(
                _component_image(
                    item.get(key), version_root, f"component step {index}.{key}"
                )
            )

    used: set[str] = set()
    if action == "type-text":
        text = item.get("text")
        if not isinstance(text, str):
            raise _invalid(f"component step {index}.text must be a string")
        try:
            used = inspect_placeholders(text, f"component step {index}.text")
        except CompositionContractError as exc:
            raise ContractError(str(exc)) from exc
        unknown = used - set(parameters)
        if unknown:
            raise ContractError(
                "component_parameter_invalid: unknown mappings: "
                + ", ".join(sorted(unknown))
            )

    parameter_map = {
        name: ParameterSpec(name=name, kind="text", default=None, sensitive=False)
        for name in parameters
    }
    try:
        step = _step(normalized, {app_id}, parameter_map, set())
    except ContractError as exc:
        raise _invalid(str(exc), cause=exc) from exc
    return step, used


def load_component(
    path: Path,
    *,
    catalog_root: Path,
) -> ComponentDefinition[
    DesktopAssertionSpec,
    DesktopImplementationSpec,
    DesktopRecoverySpec,
]:
    if not path.is_absolute():
        raise _invalid("component path must be absolute")
    catalog = _absolute_file(catalog_root, "catalog_root", directory=True)
    if not path.exists():
        raise ContractError(f"component_not_found: {path}")
    try:
        source = resolve_inside(path, catalog, "component descriptor")
    except CompositionContractError as exc:
        raise ContractError(str(exc)) from exc
    relative = source.relative_to(catalog)
    parts = relative.parts
    if len(parts) != 4 or parts[0] != "components" or parts[3] != "component.yaml":
        raise _invalid("component path must use components/<id>/vNNN/component.yaml")
    expected_id = parts[1]
    if (
        not parts[2].startswith("v")
        or len(parts[2]) < 4
        or not parts[2][1:].isdigit()
    ):
        raise _invalid("component path contains an invalid version directory")
    expected_version = int(parts[2][1:])
    source, root, raw = _load_yaml(source, "component")
    _closed(root, COMPONENT_FIELDS, "component")
    if root.get("schema_version") != "1.0":
        raise _invalid("component schema_version must equal 1.0")
    if root.get("platform") != "windows":
        raise _invalid("component platform must equal windows")
    app_id = _identifier(root.get("app_id"), "component.app_id")
    component_id = _identifier(root.get("id"), "component.id")
    version = root.get("version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version <= 0
        or component_id != expected_id
        or version != expected_version
    ):
        raise ContractError(
            "component_version_mismatch: descriptor identity does not match exact path"
        )
    effect = root.get("effect")
    if effect not in EFFECT_RANK:
        raise _invalid("component.effect is unsupported")
    timeout_seconds = _positive_number(
        root.get("timeout_seconds"), "component.timeout_seconds", 20
    )
    raw_parameters = root.get("parameters", [])
    if not isinstance(raw_parameters, list):
        raise _invalid("component.parameters must be a list")
    parameters = tuple(
        _identifier(item, "component parameter") for item in raw_parameters
    )
    if len(parameters) != len(set(parameters)):
        raise _invalid("component parameters must be unique")
    version_root = source.parent
    precondition = (
        _desktop_assertion(root["precondition"], version_root, "component.precondition")
        if root.get("precondition") is not None
        else None
    )
    already_complete = (
        _desktop_assertion(
            root["already_complete"], version_root, "component.already_complete"
        )
        if root.get("already_complete") is not None
        else None
    )
    if precondition is None and (
        effect not in {"none", "read", "idempotent"} or already_complete is None
    ):
        raise _invalid(
            "component requires precondition unless it is a state normalizer"
        )

    raw_implementations = _mapping(root.get("implementations"), "component.implementations")
    if set(raw_implementations) != {"windows-airtest"}:
        raise _invalid("component must define only windows-airtest implementation")
    raw_implementation = _mapping(
        raw_implementations["windows-airtest"],
        "component.implementations.windows-airtest",
    )
    _closed(
        raw_implementation,
        IMPLEMENTATION_FIELDS,
        "component.implementations.windows-airtest",
    )
    raw_steps = raw_implementation.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _invalid("component implementation steps must be a non-empty list")
    used_parameters: set[str] = set()
    steps: list[StepSpec] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        step, used = _component_step(
            raw_step,
            index=index,
            app_id=app_id,
            component_effect=effect,
            parameters=parameters,
            version_root=version_root,
        )
        steps.append(step)
        used_parameters.update(used)
    implementation = DesktopImplementationSpec(steps=tuple(steps))

    recovery = None
    if root.get("recovery") is not None:
        raw_recovery = _mapping(root["recovery"], "component.recovery")
        _closed(raw_recovery, RECOVERY_FIELDS, "component.recovery")
        raw_recovery_steps = raw_recovery.get("steps")
        if not isinstance(raw_recovery_steps, list) or not raw_recovery_steps:
            raise _invalid("component recovery steps must be a non-empty list")
        recovery_steps: list[StepSpec] = []
        for index, raw_step in enumerate(raw_recovery_steps, start=1):
            step, used = _component_step(
                raw_step,
                index=index,
                app_id=app_id,
                component_effect=effect,
                parameters=parameters,
                version_root=version_root,
            )
            recovery_steps.append(step)
            used_parameters.update(used)
        recovery = DesktopRecoverySpec(
            steps=tuple(recovery_steps),
            postcondition=_desktop_assertion(
                raw_recovery.get("postcondition"),
                version_root,
                "component.recovery.postcondition",
            ),
        )

    unused = set(parameters) - used_parameters
    if unused:
        raise ContractError(
            f"component_parameter_invalid: {component_id}@{version} has unused mappings: "
            + ", ".join(sorted(unused))
        )
    postcondition = _desktop_assertion(
        root.get("postcondition"), version_root, "component.postcondition"
    )
    return ComponentDefinition(
        source=source,
        descriptor_sha256=hashlib.sha256(raw).hexdigest(),
        app_id=app_id,
        id=component_id,
        version=version,
        effect=effect,
        timeout_seconds=timeout_seconds,
        parameters=parameters,
        precondition=precondition,
        already_complete=already_complete,
        implementations={"windows-airtest": implementation},
        recovery=recovery,
        postcondition=postcondition,
    )


def load_scenario(
    path: Path,
    *,
    catalog_root: Path,
) -> ScenarioDefinition[ParameterSpec]:
    if not path.is_absolute():
        raise _invalid("scenario path must be absolute")
    catalog = _absolute_file(catalog_root, "catalog_root", directory=True)
    try:
        source = resolve_inside(path, catalog, "scenario")
    except CompositionContractError as exc:
        raise ContractError(str(exc)) from exc
    source, root, _ = _load_yaml(source, "scenario")
    _closed(root, SCENARIO_FIELDS, "scenario")
    if root.get("schema_version") != "1.0":
        raise _invalid("scenario schema_version must equal 1.0")
    app_id = _identifier(root.get("app_id"), "scenario.app_id")
    scenario_id = _identifier(root.get("id"), "scenario.id")
    description = _text(root.get("description"), "scenario.description")
    raw_parameters = root.get("parameters", [])
    if not isinstance(raw_parameters, list):
        raise _invalid("scenario.parameters must be a list")
    try:
        parameters = tuple(_parameter(item) for item in raw_parameters)
    except ContractError as exc:
        raise _invalid(str(exc), cause=exc) from exc
    names = tuple(parameter.name for parameter in parameters)
    if len(names) != len(set(names)):
        raise _invalid("scenario parameter names must be unique")
    raw_components = root.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise _invalid("scenario.components must be a non-empty list")
    components: list[ComponentUse] = []
    for index, raw_use in enumerate(raw_components):
        use = _mapping(raw_use, f"scenario.components[{index}]")
        _closed(use, COMPONENT_USE_FIELDS, f"scenario.components[{index}]")
        try:
            component_id, version = parse_component_reference(
                _text(use.get("use"), "component use")
            )
        except CompositionContractError as exc:
            raise ContractError(str(exc)) from exc
        component = load_component(
            component_descriptor_path(catalog, component_id, version),
            catalog_root=catalog,
        )
        if component.app_id != app_id:
            raise _invalid("scenario app_id does not match referenced component")
        raw_map = use.get("with", {})
        if not isinstance(raw_map, dict):
            raise _invalid("component with mapping must be an object")
        try:
            parameter_map = validate_parameter_mapping(
                component_parameters=component.parameters,
                mapping=raw_map,
                scenario_parameters=names,
            )
        except CompositionContractError as exc:
            raise ContractError(str(exc)) from exc
        components.append(ComponentUse(component_id, version, parameter_map))
    return ScenarioDefinition(
        source=source,
        app_id=app_id,
        id=scenario_id,
        description=description,
        parameters=parameters,
        components=tuple(components),
    )


def load_composition_request(path: Path) -> DesktopCompositionRequest:
    if not path.is_absolute() or not path.is_file():
        raise _invalid("composition request must be an existing absolute file")
    source = path.resolve()
    try:
        root = _mapping(json.loads(source.read_text(encoding="utf-8")), "request")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid("cannot parse composition request", cause=exc) from exc
    _closed(root, REQUEST_FIELDS, "request")
    if root.get("schema_version") != "1.0":
        raise _invalid("composition request schema_version must equal 1.0")
    if root.get("target_platform") != "windows":
        raise ContractError(
            "needs-replay-backend: desktop composition currently requires windows"
        )
    source_surface = root.get("source_surface")
    if source_surface not in SOURCE_SURFACES:
        raise _invalid("composition request source_surface is unsupported")
    if root.get("effective_path_confirmed") is not True:
        raise _invalid("effective_path_confirmed must be true")
    decision = root.get("decision")
    if decision not in {"generate-only", "generate-and-replay"}:
        raise _invalid("composition request decision is unsupported")
    catalog = _absolute_file(root.get("catalog_root"), "catalog_root", directory=True)
    scenario_path = _absolute_file(root.get("scenario"), "scenario")
    try:
        scenario_path = resolve_inside(scenario_path, catalog, "scenario")
    except CompositionContractError as exc:
        raise ContractError(str(exc)) from exc
    scenario = load_scenario(scenario_path, catalog_root=catalog)
    raw_apps = root.get("apps")
    if not isinstance(raw_apps, list) or len(raw_apps) != 1:
        raise _invalid("composition request apps must contain exactly one app")
    try:
        apps = tuple(_app(item) for item in raw_apps)
    except ContractError as exc:
        raise _invalid(str(exc), cause=exc) from exc
    if apps[0].alias != scenario.app_id:
        raise _invalid("composition request app alias must match scenario app_id")
    try:
        assertion = _assertion(root.get("success_assertion"), {apps[0].alias})
    except ContractError as exc:
        raise _invalid(str(exc), cause=exc) from exc
    return DesktopCompositionRequest(
        source=source,
        scenario=scenario_path,
        catalog_root=catalog,
        source_surface=source_surface,
        target_platform="windows",
        apps=apps,
        success_assertion=assertion,
        decision=decision,
        effective_path_confirmed=True,
    )
