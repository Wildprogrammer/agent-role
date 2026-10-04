from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import yaml

from .composition_models import (
    AppComponentSpec,
    AppScenarioSpec,
    ComponentRecoverySpec,
    ComponentUseSpec,
    CompositionRequest,
)
from .contracts import (
    ContractError,
    EFFECT_RANK,
    IDENTIFIER,
    load_assertion_spec,
    load_device_spec,
    load_parameter_spec,
    load_script_asset,
    validate_runtime_parameter_names,
)


COMPONENT_FIELDS = {
    "schema_version",
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
IMPLEMENTATION_FIELDS = {"script"}
RECOVERY_FIELDS = {"script", "postcondition"}
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
    "text_input_mode",
    "source_surface",
    "target_platform",
    "device",
    "decision",
    "effective_path_confirmed",
}
IMPLEMENTATION_KEYS = {"default", "direct-ime", "pinyin-fallback"}
EXACT_USE = re.compile(r"^([a-z][a-z0-9_-]{0,63})@([1-9][0-9]*)$")
VERSION_DIRECTORY = re.compile(r"^v([0-9]{3,})$")


class _DuplicateKeyError(yaml.YAMLError):
    pass


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
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


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid(f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label)
    if not IDENTIFIER.fullmatch(text):
        raise _invalid(f"{label} must be a lowercase identifier")
    return text


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _invalid(f"{label} must be a positive integer")
    return value


def _positive_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise _invalid(f"{label} must be a finite number greater than zero")
    return float(value)


def _resolved_inside(path: Path, root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise _invalid(f"{label} must resolve inside {root}", cause=exc) from exc
    return resolved


def _load_yaml(path: Path, label: str) -> tuple[Path, dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
        source = path.resolve(strict=True)
        value = yaml.load(raw.decode("utf-8"), Loader=_UniqueKeyLoader)
    except _DuplicateKeyError as exc:
        raise ContractError(f"component_parameter_invalid: {exc}") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _invalid(f"cannot load {label}", cause=exc) from exc
    return source, _mapping(value, label), raw


def _load_json(path: Path, label: str) -> tuple[Path, dict[str, Any]]:
    try:
        source = path.resolve(strict=True)
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid(f"cannot load {label}", cause=exc) from exc
    return source, _mapping(value, label)


def _absolute_path(value: Any, label: str, *, directory: bool = False) -> Path:
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


def _selector_assertion(value: Any, label: str):
    try:
        assertion = load_assertion_spec(value, label)
    except ContractError as exc:
        raise _invalid(str(exc), cause=exc) from exc
    if assertion.kind != "selector-visible":
        raise _invalid(f"{label} must be selector-visible")
    return assertion


def _component_asset(
    value: Any, label: str, version_root: Path, effect: str
) -> Path:
    raw = Path(_text(value, label))
    if raw.is_absolute():
        raise _invalid(f"{label} must be relative to the component version directory")
    asset = _resolved_inside(version_root / raw, version_root, label)
    try:
        return load_script_asset(str(asset), label, effect)
    except ContractError as exc:
        raise _invalid(str(exc), cause=exc) from exc


def component_descriptor_path(
    catalog_root: Path, component_id: str, version: int
) -> Path:
    return (
        catalog_root
        / "components"
        / component_id
        / f"v{version:03d}"
        / "component.yaml"
    )


def load_component(path: Path, *, catalog_root: Path) -> AppComponentSpec:
    if not path.is_absolute():
        raise _invalid("component path must be absolute")
    catalog = _absolute_path(catalog_root, "catalog_root", directory=True)
    if not path.exists():
        raise ContractError(f"component_not_found: {path}")
    source = _resolved_inside(path, catalog, "component descriptor")
    try:
        relative = path.absolute().relative_to(catalog_root.absolute())
    except ValueError as exc:
        raise _invalid("component descriptor must be inside the catalog", cause=exc) from exc
    parts = relative.parts
    if len(parts) != 4 or parts[0] != "components" or parts[3] != "component.yaml":
        raise _invalid("component descriptor path must use components/<id>/vNNN/component.yaml")
    expected_id = parts[1]
    version_match = VERSION_DIRECTORY.fullmatch(parts[2])
    if not IDENTIFIER.fullmatch(expected_id) or version_match is None:
        raise _invalid("component descriptor path contains an invalid identity")
    expected_version = int(version_match.group(1))
    version_root = source.parent
    source, root, raw = _load_yaml(source, "component")
    _closed(root, COMPONENT_FIELDS, "component")
    if root.get("schema_version") != "1.0":
        raise _invalid("component schema_version must equal 1.0")
    app_id = _identifier(root.get("app_id"), "component.app_id")
    component_id = _identifier(root.get("id"), "component.id")
    version = _positive_integer(root.get("version"), "component.version")
    if component_id != expected_id or version != expected_version:
        raise ContractError(
            "component_version_mismatch: descriptor identity does not match its exact path"
        )
    effect = root.get("effect")
    if effect not in EFFECT_RANK:
        raise _invalid("component.effect is unsupported")
    timeout = _positive_number(root.get("timeout_seconds"), "component.timeout_seconds")
    raw_parameters = root.get("parameters")
    if not isinstance(raw_parameters, list):
        raise _invalid("component.parameters must be a list")
    parameters = tuple(_identifier(item, "component parameter") for item in raw_parameters)
    if len(parameters) != len(set(parameters)):
        raise _invalid("component parameters must be unique")

    precondition = None
    if root.get("precondition") is not None:
        precondition = _selector_assertion(root["precondition"], "component.precondition")
    already_complete = None
    if root.get("already_complete") is not None:
        already_complete = _selector_assertion(
            root["already_complete"], "component.already_complete"
        )
    if precondition is None and (effect not in {"none", "read", "idempotent"} or already_complete is None):
        raise _invalid(
            "component requires precondition unless it is an idempotent state normalizer"
        )

    raw_implementations = _mapping(root.get("implementations"), "component.implementations")
    if not raw_implementations:
        raise _invalid("component.implementations must not be empty")
    unknown_implementations = sorted(set(raw_implementations) - IMPLEMENTATION_KEYS)
    if unknown_implementations:
        raise _invalid("component implementation key is unsupported")
    implementations: dict[str, Path] = {}
    for key, raw_implementation in raw_implementations.items():
        implementation = _mapping(
            raw_implementation, f"component.implementations.{key}"
        )
        _closed(implementation, IMPLEMENTATION_FIELDS, f"component.implementations.{key}")
        implementations[key] = _component_asset(
            implementation.get("script"),
            f"component.implementations.{key}.script",
            version_root,
            effect,
        )

    recovery = None
    if root.get("recovery") is not None:
        raw_recovery = _mapping(root["recovery"], "component.recovery")
        _closed(raw_recovery, RECOVERY_FIELDS, "component.recovery")
        recovery = ComponentRecoverySpec(
            script=_component_asset(
                raw_recovery.get("script"),
                "component.recovery.script",
                version_root,
                effect,
            ),
            postcondition=_selector_assertion(
                raw_recovery.get("postcondition"),
                "component.recovery.postcondition",
            ),
        )
    postcondition = _selector_assertion(
        root.get("postcondition"), "component.postcondition"
    )
    return AppComponentSpec(
        source=source,
        descriptor_sha256=hashlib.sha256(raw).hexdigest(),
        app_id=app_id,
        id=component_id,
        version=version,
        effect=effect,
        timeout_seconds=timeout,
        parameters=parameters,
        precondition=precondition,
        already_complete=already_complete,
        implementations=implementations,
        recovery=recovery,
        postcondition=postcondition,
    )


def load_scenario(path: Path, *, catalog_root: Path) -> AppScenarioSpec:
    if not path.is_absolute():
        raise _invalid("scenario path must be absolute")
    catalog = _absolute_path(catalog_root, "catalog_root", directory=True)
    source = _resolved_inside(path, catalog, "scenario")
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
        parameters = tuple(load_parameter_spec(item) for item in raw_parameters)
    except ContractError as exc:
        raise _invalid(str(exc), cause=exc) from exc
    parameter_names = {parameter.name for parameter in parameters}
    if len(parameter_names) != len(parameters):
        raise _invalid("scenario parameter names must be unique")
    try:
        validate_runtime_parameter_names([parameter.name for parameter in parameters])
    except ContractError as exc:
        raise _invalid(str(exc), cause=exc) from exc
    raw_components = root.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise _invalid("scenario.components must be a non-empty list")
    components: list[ComponentUseSpec] = []
    for index, raw_use in enumerate(raw_components):
        use = _mapping(raw_use, f"scenario.components[{index}]")
        _closed(use, COMPONENT_USE_FIELDS, f"scenario.components[{index}]")
        match = EXACT_USE.fullmatch(_text(use.get("use"), "component use"))
        if match is None:
            raise _invalid("component use must be an exact <id>@<version> reference")
        component_id, version_text = match.groups()
        version = int(version_text)
        component = load_component(
            component_descriptor_path(catalog, component_id, version),
            catalog_root=catalog,
        )
        if component.app_id != app_id:
            raise _invalid("scenario app_id does not match referenced component")
        raw_map = use.get("with", {})
        if not isinstance(raw_map, dict):
            raise _invalid("component with mapping must be an object")
        parameter_map: dict[str, str] = {}
        for local_name, scenario_name in raw_map.items():
            local = _identifier(local_name, "component parameter mapping key")
            target = _identifier(scenario_name, "component parameter mapping value")
            if target not in parameter_names:
                raise ContractError(
                    "component_parameter_invalid: mapping references an unknown scenario parameter"
                )
            parameter_map[local] = target
        components.append(
            ComponentUseSpec(
                component_id=component_id,
                version=version,
                parameter_map=parameter_map,
            )
        )
    return AppScenarioSpec(
        source=source,
        app_id=app_id,
        id=scenario_id,
        description=description,
        parameters=parameters,
        components=tuple(components),
    )


def load_composition_request(path: Path) -> CompositionRequest:
    if not path.is_absolute() or not path.is_file():
        raise _invalid("composition request path must be an existing absolute file")
    source, root = _load_json(path, "composition request")
    _closed(root, REQUEST_FIELDS, "composition request")
    if root.get("schema_version") != "1.0":
        raise _invalid("composition request schema_version must equal 1.0")
    catalog = _absolute_path(root.get("catalog_root"), "catalog_root", directory=True)
    scenario = _absolute_path(root.get("scenario"), "scenario")
    try:
        scenario.relative_to(catalog)
    except ValueError as exc:
        raise _invalid("scenario must resolve inside catalog_root", cause=exc) from exc
    text_input_mode = root.get("text_input_mode")
    if text_input_mode not in {"direct-ime", "pinyin-fallback"}:
        raise _invalid("text_input_mode is unsupported")
    source_surface = root.get("source_surface")
    if source_surface not in {"agent-device-mcp", "agent-device-cli"}:
        raise _invalid("source_surface is unsupported")
    if root.get("target_platform") != "android":
        raise _invalid("unsupported_platform: target_platform must equal android")
    decision = root.get("decision")
    if decision not in {"generate-only", "generate-and-replay"}:
        raise _invalid("decision is unsupported")
    if root.get("effective_path_confirmed") is not True:
        raise _invalid("effective_path_confirmed must be true")
    try:
        device = load_device_spec(root.get("device"))
    except ContractError as exc:
        raise _invalid(str(exc), cause=exc) from exc
    return CompositionRequest(
        source=source,
        scenario=scenario,
        catalog_root=catalog,
        text_input_mode=text_input_mode,
        source_surface=source_surface,
        target_platform="android",
        device=device,
        decision=decision,
        effective_path_confirmed=True,
    )
