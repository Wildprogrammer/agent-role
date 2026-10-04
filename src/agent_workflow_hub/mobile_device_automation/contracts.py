from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .agent_device_runtime import RuntimeFailure, runtime_parameter_name
from .models import (
    AssertionSpec,
    AutomationRequest,
    DeviceSpec,
    ParameterSpec,
    StepSpec,
    ViewportSpec,
)


class ContractError(ValueError):
    """Raised when a mobile automation request or manifest is unsafe."""


MAX_SOURCE_BYTES = 1024 * 1024
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
USB_SERIAL = re.compile(r"^[A-Za-z0-9._-]+$")
EMULATOR_SERIAL = re.compile(r"^emulator-[0-9]+$")
WIRELESS_SERIAL = re.compile(r"^([^\s:]+|\[[0-9A-Fa-f:]+\]):([0-9]{1,5})$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EFFECTS = ("none", "read", "idempotent", "create", "submit", "send", "delete")
EFFECT_RANK = {name: index for index, name in enumerate(EFFECTS)}

TOP_LEVEL_FIELDS = {
    "schema_version",
    "name",
    "description",
    "source_surface",
    "target_platform",
    "device",
    "parameters",
    "steps",
    "decision",
    "effective_path_confirmed",
}
DEVICE_FIELDS = {"serial", "transport", "viewport"}
VIEWPORT_FIELDS = {"width", "height", "orientation", "density"}
PARAMETER_FIELDS = {"name", "default", "sensitive"}
STEP_FIELDS = {
    "id",
    "runner",
    "effect",
    "timeout_seconds",
    "script",
    "action",
    "template",
    "threshold",
    "assertion",
}
ASSERTION_FIELDS = {"kind", "selector", "template", "threshold", "timeout_seconds"}

MANIFEST_FIELDS = {
    "schema_version",
    "workflow",
    "name",
    "description",
    "target_platform",
    "status",
    "decision",
    "aggregate_effect",
    "effective_path_confirmed",
    "plan_sha256",
    "generated_at",
    "device",
    "parameters",
    "steps",
}
MANIFEST_STEP_FIELDS = STEP_FIELDS | {"source_sha256", "component"}
MANIFEST_ASSERTION_FIELDS = ASSERTION_FIELDS | {"source_sha256"}
MANIFEST_COMPONENT_FIELDS = {
    "app_id",
    "id",
    "version",
    "occurrence_id",
    "descriptor_sha256",
    "text_input_mode",
    "implementation",
    "parameter_map",
    "precondition",
    "already_complete",
    "recovery",
}
MANIFEST_COMPONENT_RECOVERY_FIELDS = {
    "script",
    "source_sha256",
    "postcondition",
}


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _closed(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"unknown field in {label}: {', '.join(unknown)}")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label)
    if not IDENTIFIER.fullmatch(text):
        raise ContractError(f"{label} must be a lowercase identifier")
    return text


def _positive_number(value: Any, label: str, *, default: float | None = None) -> float:
    number = default if value is None else value
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or not math.isfinite(number)
        or number <= 0
    ):
        raise ContractError(f"{label} must be a finite number greater than zero")
    return float(number)


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{label} must be a positive integer")
    return value


def _threshold(value: Any, label: str, *, default: float = 0.85) -> float:
    number = default if value is None else value
    if (
        isinstance(number, bool)
        or not isinstance(number, (int, float))
        or not math.isfinite(number)
        or not 0 < number <= 1
    ):
        raise ContractError(f"{label} must be a finite number greater than zero and at most one")
    return float(number)


def _source_file(value: Any, label: str, suffixes: set[str]) -> Path:
    raw = Path(_text(value, label))
    suffix_description = "/".join(sorted(suffixes))
    if not raw.is_absolute() or not raw.is_file() or raw.suffix.casefold() not in suffixes:
        raise ContractError(f"{label} must be an absolute existing {suffix_description} file")
    try:
        size = raw.stat().st_size
    except OSError as exc:
        raise ContractError(f"cannot inspect {label}: {exc}") from exc
    if size > MAX_SOURCE_BYTES:
        raise ContractError(f"{label} must not exceed 1 MiB")
    return raw.resolve()


def _image(value: Any, label: str) -> Path:
    return _source_file(value, label, {".jpeg", ".jpg", ".png"})


def _script(value: Any, label: str, effect: str) -> Path:
    path = _source_file(value, label, {".ad"})
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"{label} must be valid UTF-8: {exc}") from exc
    destructive = ("reinstall", "uninstall", "clear-data", "factory-reset")
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        lowered = line.casefold()
        command = re.split(r"[\s:=]", lowered, maxsplit=1)[0]
        if command in {"include", "runflow"}:
            raise ContractError(f"{label} must be a self-contained .ad script")
        if command in destructive and effect != "delete":
            raise ContractError(
                f"destructive semantic command {command} requires effect=delete"
            )
    return path


def validate_test_ime(script: Path, parameters: Mapping[str, str]) -> None:
    """Reject non-ASCII fill text unless the session explicitly enables test IME."""
    try:
        lines = script.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"script must be valid UTF-8: {exc}") from exc
    test_ime_enabled = False
    parameter_aliases = dict(parameters)
    parameter_aliases.update(
        {runtime_parameter_name(name): value for name, value in parameters.items()}
    )
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            continue
        if not tokens:
            continue
        command = tokens[0].casefold()
        if command == "open" and "--test-ime" in tokens[1:]:
            test_ime_enabled = True
            continue
        if command != "fill" or len(tokens) < 3:
            continue
        text_index = 2
        if len(tokens) >= 4:
            try:
                float(tokens[1])
                float(tokens[2])
            except ValueError:
                pass
            else:
                text_index = 3
        text = " ".join(tokens[text_index:])
        text = re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_-]*)\}",
            lambda match: parameter_aliases.get(match.group(1), match.group(0)),
            text,
        )
        if any(ord(character) > 127 for character in text) and not test_ime_enabled:
            raise ContractError(
                "test_ime_required: non-ASCII fill requires open --test-ime"
            )


def _device(value: Any) -> DeviceSpec:
    item = _object(value, "device")
    _closed(item, DEVICE_FIELDS, "device")
    serial = _text(item.get("serial"), "device.serial")
    transport = item.get("transport")
    if transport not in {"usb", "wireless", "emulator"}:
        raise ContractError("device.transport must be usb, wireless, or emulator")
    if transport == "usb" and not USB_SERIAL.fullmatch(serial):
        raise ContractError("device.serial is invalid for usb transport")
    if transport == "emulator" and not EMULATOR_SERIAL.fullmatch(serial):
        raise ContractError("device.serial must match emulator-N for emulator transport")
    if transport == "wireless":
        match = WIRELESS_SERIAL.fullmatch(serial)
        if not match or not 1 <= int(match.group(2)) <= 65535:
            raise ContractError("device.serial must be a valid host:port for wireless transport")

    viewport_item = _object(item.get("viewport"), "device.viewport")
    _closed(viewport_item, VIEWPORT_FIELDS, "device.viewport")
    orientation = viewport_item.get("orientation")
    if orientation not in {"portrait", "landscape"}:
        raise ContractError("device.viewport.orientation must be portrait or landscape")
    viewport = ViewportSpec(
        width=_positive_integer(viewport_item.get("width"), "device.viewport.width"),
        height=_positive_integer(viewport_item.get("height"), "device.viewport.height"),
        orientation=orientation,
        density=_positive_number(viewport_item.get("density"), "device.viewport.density"),
    )
    if (orientation == "portrait" and viewport.width > viewport.height) or (
        orientation == "landscape" and viewport.height > viewport.width
    ):
        raise ContractError("device.viewport.orientation does not match width and height")
    return DeviceSpec(serial=serial, transport=transport, viewport=viewport)


def _parameter(value: Any) -> ParameterSpec:
    item = _object(value, "parameter")
    _closed(item, PARAMETER_FIELDS, "parameter")
    name = _identifier(item.get("name"), "parameter.name")
    sensitive = item.get("sensitive", False)
    if not isinstance(sensitive, bool):
        raise ContractError(f"parameter {name} sensitive must be boolean")
    default = item.get("default")
    if default is not None and not isinstance(default, str):
        raise ContractError(f"parameter {name} default must be a string or null")
    if sensitive and default is not None:
        raise ContractError(f"sensitive parameter {name} cannot have a default")
    return ParameterSpec(name=name, default=default, sensitive=sensitive)


def validate_runtime_parameter_names(names: list[str]) -> None:
    try:
        runtime_names = [runtime_parameter_name(name) for name in names]
    except RuntimeFailure as exc:
        raise ContractError(
            "parameter name is incompatible with the agent-device runtime"
        ) from exc
    if len(runtime_names) != len(set(runtime_names)):
        raise ContractError("parameter runtime names must be unique")


def script_references_parameters(script: Path, names: list[str]) -> bool:
    try:
        text = script.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ContractError(f"script must be valid UTF-8: {exc}") from exc
    aliases = set(names)
    aliases.update(runtime_parameter_name(name) for name in names)
    tokens = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_-]*)\}", text))
    return bool(tokens & aliases)


def _assertion(value: Any, label: str, *, manifest: bool = False) -> AssertionSpec:
    item = _object(value, label)
    allowed = MANIFEST_ASSERTION_FIELDS if manifest else ASSERTION_FIELDS
    _closed(item, allowed, label)
    kind = item.get("kind")
    timeout = _positive_number(
        item.get("timeout_seconds"), f"{label}.timeout_seconds", default=10.0
    )
    if kind == "selector-visible":
        forbidden = {"template", "threshold", "source_sha256"} & set(item)
        if forbidden:
            raise ContractError(f"{label} selector-visible has incompatible fields")
        return AssertionSpec(
            kind=kind,
            selector=_text(item.get("selector"), f"{label}.selector"),
            template=None,
            threshold=None,
            timeout_seconds=timeout,
        )
    if kind == "image-visible":
        if "selector" in item:
            raise ContractError(f"{label} image-visible must omit selector")
        template = (
            Path(_relative_bundle_path(item.get("template"), f"{label}.template"))
            if manifest
            else _image(item.get("template"), f"{label}.template")
        )
        if manifest:
            _sha256_text(item.get("source_sha256"), f"{label}.source_sha256")
        return AssertionSpec(
            kind=kind,
            selector=None,
            template=template,
            threshold=_threshold(item.get("threshold"), f"{label}.threshold"),
            timeout_seconds=timeout,
        )
    raise ContractError(f"{label}.kind is unsupported")


def load_assertion_spec(value: Any, label: str) -> AssertionSpec:
    """Load an assertion using the shared request validation rules."""
    return _assertion(value, label)


def load_script_asset(value: Any, label: str, effect: str) -> Path:
    """Load a semantic script using the shared safety and size checks."""
    return _script(value, label, effect)


def load_device_spec(value: Any) -> DeviceSpec:
    """Load a device identity using the shared request validation rules."""
    return _device(value)


def load_parameter_spec(value: Any) -> ParameterSpec:
    """Load a parameter declaration using the shared request validation rules."""
    return _parameter(value)


def _step(value: Any, seen: set[str]) -> StepSpec:
    item = _object(value, "step")
    _closed(item, STEP_FIELDS, "step")
    step_id = _identifier(item.get("id"), "step.id")
    if step_id in seen:
        raise ContractError(f"duplicate step id: {step_id}")
    seen.add(step_id)
    runner = item.get("runner")
    effect = item.get("effect", "none")
    if effect not in EFFECT_RANK:
        raise ContractError(f"step {step_id} effect is unsupported")
    timeout = _positive_number(
        item.get("timeout_seconds"), f"step {step_id} timeout_seconds", default=10.0
    )
    assertion = None
    if item.get("assertion") is not None:
        assertion = _assertion(item["assertion"], f"step {step_id} assertion")

    if runner == "agent-device":
        forbidden = {"action", "template", "threshold"} & set(item)
        if forbidden:
            raise ContractError(f"step {step_id} agent-device has incompatible fields")
        script = _script(item.get("script"), f"step {step_id} script", effect)
        return StepSpec(
            id=step_id,
            runner=runner,
            effect=effect,
            timeout_seconds=timeout,
            script=script,
            action=None,
            template=None,
            threshold=None,
            assertion=assertion,
        )

    if runner == "airtest":
        if "script" in item:
            raise ContractError(f"step {step_id} airtest must omit script")
        action = item.get("action")
        if action not in {"click-image", "wait-image"}:
            raise ContractError(f"step {step_id} action is unsupported")
        if action == "click-image" and assertion is None:
            raise ContractError(f"step {step_id} click-image requires an assertion")
        return StepSpec(
            id=step_id,
            runner=runner,
            effect=effect,
            timeout_seconds=timeout,
            script=None,
            action=action,
            template=_image(item.get("template"), f"step {step_id} template"),
            threshold=_threshold(item.get("threshold"), f"step {step_id} threshold"),
            assertion=assertion,
        )
    raise ContractError(f"step {step_id} runner is unsupported")


def load_request(path: Path) -> AutomationRequest:
    if not path.is_absolute() or not path.is_file():
        raise ContractError("request path must be an existing absolute file")
    source = path.resolve()
    try:
        root = _object(json.loads(source.read_text(encoding="utf-8")), "request")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid request JSON: {exc}") from exc
    _closed(root, TOP_LEVEL_FIELDS, "request")
    if root.get("schema_version") != "1.0":
        raise ContractError("schema_version must equal 1.0")
    if root.get("target_platform") != "android":
        raise ContractError("unsupported_platform: target_platform must equal android")
    source_surface = root.get("source_surface")
    if source_surface not in {"agent-device-mcp", "agent-device-cli"}:
        raise ContractError("source_surface is unsupported")
    decision = root.get("decision")
    if decision not in {"generate-only", "generate-and-replay"}:
        raise ContractError("decision must be generate-only or generate-and-replay")
    if root.get("effective_path_confirmed") is not True:
        raise ContractError("effective_path_confirmed must be true")

    raw_parameters = root.get("parameters", [])
    if not isinstance(raw_parameters, list):
        raise ContractError("parameters must be a list")
    parameters = tuple(_parameter(item) for item in raw_parameters)
    names = [item.name for item in parameters]
    if len(names) != len(set(names)):
        raise ContractError("parameter names must be unique")
    validate_runtime_parameter_names(names)

    raw_steps = root.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ContractError("steps must be a non-empty list")
    seen: set[str] = set()
    steps = tuple(_step(item, seen) for item in raw_steps)
    defaults = {
        parameter.name: parameter.default
        for parameter in parameters
        if parameter.default is not None
    }
    for step in steps:
        if step.runner == "agent-device" and step.script is not None:
            validate_test_ime(step.script, defaults)
    aggregate_effect = max((step.effect for step in steps), key=EFFECT_RANK.__getitem__)
    return AutomationRequest(
        source=source,
        name=_identifier(root.get("name"), "name"),
        description=_text(root.get("description"), "description"),
        source_surface=source_surface,
        target_platform="android",
        device=_device(root.get("device")),
        parameters=parameters,
        steps=steps,
        decision=decision,
        effective_path_confirmed=True,
        aggregate_effect=aggregate_effect,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _assertion_payload(assertion: AssertionSpec | None) -> dict[str, object] | None:
    if assertion is None:
        return None
    payload: dict[str, object] = {
        "kind": assertion.kind,
        "timeout_seconds": assertion.timeout_seconds,
    }
    if assertion.selector is not None:
        payload["selector"] = assertion.selector
    if assertion.template is not None:
        payload.update(
            {
                "source_sha256": _sha256_file(assertion.template),
                "threshold": assertion.threshold,
            }
        )
    return payload


def _component_payload(step: StepSpec) -> dict[str, object] | None:
    component = step.component
    if component is None:
        return None
    recovery = component.recovery
    return {
        "app_id": component.app_id,
        "id": component.component_id,
        "version": component.version,
        "occurrence_id": component.occurrence_id,
        "descriptor_sha256": component.descriptor_sha256,
        "text_input_mode": component.text_input_mode,
        "implementation": component.implementation,
        "parameter_map": dict(sorted(component.parameter_map.items())),
        "precondition": _assertion_payload(component.precondition),
        "already_complete": _assertion_payload(component.already_complete),
        "recovery": (
            None
            if recovery is None
            else {
                "source_sha256": _sha256_file(recovery.script),
                "postcondition": _assertion_payload(recovery.postcondition),
            }
        ),
    }


def plan_payload(request: AutomationRequest) -> dict[str, object]:
    steps: list[dict[str, object]] = []
    for step in request.steps:
        item: dict[str, object] = {
            "id": step.id,
            "runner": step.runner,
            "effect": step.effect,
            "timeout_seconds": step.timeout_seconds,
            "assertion": _assertion_payload(step.assertion),
        }
        component_payload = _component_payload(step)
        if component_payload is not None:
            item["component"] = component_payload
        if step.script is not None:
            item.update(
                {
                    "source_sha256": _sha256_file(step.script),
                }
            )
        if step.template is not None:
            item.update(
                {
                    "action": step.action,
                    "source_sha256": _sha256_file(step.template),
                    "threshold": step.threshold,
                }
            )
        steps.append(item)
    return {
        "schema_version": "1.0",
        "workflow": "mobile-device-automation",
        "name": request.name,
        "description": request.description,
        "target_platform": request.target_platform,
        "device": {
            "serial": request.device.serial,
            "transport": request.device.transport,
            "viewport": {
                "width": request.device.viewport.width,
                "height": request.device.viewport.height,
                "orientation": request.device.viewport.orientation,
                "density": request.device.viewport.density,
            },
        },
        "parameters": [
            {
                "name": parameter.name,
                "default": parameter.default,
                "sensitive": parameter.sensitive,
            }
            for parameter in request.parameters
        ],
        "steps": steps,
        "decision": request.decision,
        "effective_path_confirmed": request.effective_path_confirmed,
        "aggregate_effect": request.aggregate_effect,
    }


def plan_sha256(request: AutomationRequest) -> str:
    encoded = json.dumps(
        plan_payload(request),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _relative_bundle_path(value: Any, label: str) -> str:
    text = _text(value, label).replace("\\", "/")
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts or text.startswith("/"):
        raise ContractError(f"{label} must be a relative bundle path")
    return str(path)


def _sha256_text(value: Any, label: str) -> str:
    text = _text(value, label)
    if not SHA256.fullmatch(text):
        raise ContractError(f"{label} must be 64 lowercase hexadecimal characters")
    return text


def _manifest_step(value: Any, seen: set[str]) -> dict[str, object]:
    item = _object(value, "manifest step")
    _closed(item, MANIFEST_STEP_FIELDS, "manifest step")
    step_id = _identifier(item.get("id"), "manifest step.id")
    if step_id in seen:
        raise ContractError(f"duplicate step id: {step_id}")
    seen.add(step_id)
    runner = item.get("runner")
    effect = item.get("effect")
    if effect not in EFFECT_RANK:
        raise ContractError(f"manifest step {step_id} effect is unsupported")
    timeout = _positive_number(
        item.get("timeout_seconds"),
        f"manifest step {step_id} timeout_seconds",
    )
    result: dict[str, object] = {
        "id": step_id,
        "runner": runner,
        "effect": effect,
        "timeout_seconds": timeout,
    }
    assertion_value = item.get("assertion")
    if assertion_value is None:
        result["assertion"] = None
    else:
        assertion = _object(assertion_value, f"manifest step {step_id} assertion")
        _assertion(assertion, f"manifest step {step_id} assertion", manifest=True)
        result["assertion"] = assertion
    component_value = item.get("component")
    if component_value is not None:
        if runner != "agent-device":
            raise ContractError(f"manifest step {step_id} component requires agent-device")
        component = _object(component_value, f"manifest step {step_id} component")
        _closed(
            component,
            MANIFEST_COMPONENT_FIELDS,
            f"manifest step {step_id} component",
        )
        normalized_component: dict[str, object] = {
            "app_id": _identifier(
                component.get("app_id"), f"manifest step {step_id} component.app_id"
            ),
            "id": _identifier(
                component.get("id"), f"manifest step {step_id} component.id"
            ),
            "version": _positive_integer(
                component.get("version"), f"manifest step {step_id} component.version"
            ),
            "occurrence_id": _identifier(
                component.get("occurrence_id"),
                f"manifest step {step_id} component.occurrence_id",
            ),
            "descriptor_sha256": _sha256_text(
                component.get("descriptor_sha256"),
                f"manifest step {step_id} component.descriptor_sha256",
            ),
        }
        text_input_mode = component.get("text_input_mode")
        if text_input_mode not in {"direct-ime", "pinyin-fallback"}:
            raise ContractError(f"manifest step {step_id} component text mode is unsupported")
        implementation = component.get("implementation")
        if implementation not in {"default", "direct-ime", "pinyin-fallback"}:
            raise ContractError(
                f"manifest step {step_id} component implementation is unsupported"
            )
        parameter_map = _object(
            component.get("parameter_map"),
            f"manifest step {step_id} component.parameter_map",
        )
        normalized_map: dict[str, str] = {}
        for local, scenario in parameter_map.items():
            normalized_map[_identifier(local, "component parameter key")] = _identifier(
                scenario, "component parameter value"
            )
        normalized_component.update(
            {
                "text_input_mode": text_input_mode,
                "implementation": implementation,
                "parameter_map": dict(sorted(normalized_map.items())),
            }
        )
        for assertion_name in ("precondition", "already_complete"):
            raw_assertion = component.get(assertion_name)
            if raw_assertion is None:
                normalized_component[assertion_name] = None
            else:
                parsed = _assertion(
                    raw_assertion,
                    f"manifest step {step_id} component.{assertion_name}",
                    manifest=True,
                )
                if parsed.kind != "selector-visible":
                    raise ContractError(
                        f"manifest step {step_id} component assertions must be selector-visible"
                    )
                normalized_component[assertion_name] = raw_assertion
        raw_recovery = component.get("recovery")
        if raw_recovery is None:
            normalized_component["recovery"] = None
        else:
            recovery = _object(
                raw_recovery, f"manifest step {step_id} component.recovery"
            )
            _closed(
                recovery,
                MANIFEST_COMPONENT_RECOVERY_FIELDS,
                f"manifest step {step_id} component.recovery",
            )
            postcondition = recovery.get("postcondition")
            parsed = _assertion(
                postcondition,
                f"manifest step {step_id} component.recovery.postcondition",
                manifest=True,
            )
            if parsed.kind != "selector-visible":
                raise ContractError(
                    f"manifest step {step_id} recovery postcondition must be selector-visible"
                )
            normalized_component["recovery"] = {
                "script": _relative_bundle_path(
                    recovery.get("script"),
                    f"manifest step {step_id} component.recovery.script",
                ),
                "source_sha256": _sha256_text(
                    recovery.get("source_sha256"),
                    f"manifest step {step_id} component.recovery.source_sha256",
                ),
                "postcondition": postcondition,
            }
        result["component"] = normalized_component
    if runner == "agent-device":
        result["script"] = _relative_bundle_path(
            item.get("script"), f"manifest step {step_id}.script"
        )
        result["source_sha256"] = _sha256_text(
            item.get("source_sha256"), f"manifest step {step_id}.source_sha256"
        )
        if {"action", "template", "threshold"} & set(item):
            raise ContractError(f"manifest step {step_id} has incompatible fields")
    elif runner == "airtest":
        action = item.get("action")
        if action not in {"click-image", "wait-image"}:
            raise ContractError(f"manifest step {step_id} action is unsupported")
        if action == "click-image" and assertion_value is None:
            raise ContractError(f"manifest step {step_id} click-image requires an assertion")
        result.update(
            {
                "action": action,
                "template": _relative_bundle_path(
                    item.get("template"), f"manifest step {step_id}.template"
                ),
                "source_sha256": _sha256_text(
                    item.get("source_sha256"),
                    f"manifest step {step_id}.source_sha256",
                ),
                "threshold": _threshold(
                    item.get("threshold"), f"manifest step {step_id}.threshold"
                ),
            }
        )
        if "script" in item:
            raise ContractError(f"manifest step {step_id} airtest must omit script")
    else:
        raise ContractError(f"manifest step {step_id} runner is unsupported")
    return result


def load_manifest(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not path.is_file():
        raise ContractError("manifest path must be an existing absolute file")
    try:
        root = _object(yaml.safe_load(path.read_text(encoding="utf-8")), "manifest")
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ContractError(f"invalid manifest YAML: {exc}") from exc
    _closed(root, MANIFEST_FIELDS, "manifest")
    if root.get("schema_version") != "1.0":
        raise ContractError("manifest schema_version must equal 1.0")
    if root.get("workflow") != "mobile-device-automation":
        raise ContractError("manifest workflow is unsupported")
    if root.get("target_platform") != "android":
        raise ContractError("unsupported_platform: manifest target must equal android")
    if root.get("status") not in {
        "generated-unverified",
        "replay-verified",
        "replay-failed",
    }:
        raise ContractError("manifest status is unsupported")
    if root.get("decision") not in {"generate-only", "generate-and-replay"}:
        raise ContractError("manifest decision is unsupported")
    if root.get("aggregate_effect") not in EFFECT_RANK:
        raise ContractError("manifest aggregate_effect is unsupported")
    if root.get("effective_path_confirmed") is not True:
        raise ContractError("manifest effective_path_confirmed must be true")
    _identifier(root.get("name"), "manifest name")
    _text(root.get("description"), "manifest description")
    _text(root.get("generated_at"), "manifest generated_at")
    _sha256_text(root.get("plan_sha256"), "manifest plan_sha256")
    _device(root.get("device"))
    parameters_value = root.get("parameters")
    if not isinstance(parameters_value, list):
        raise ContractError("manifest parameters must be a list")
    parameters = [_parameter(value) for value in parameters_value]
    parameter_names = [value.name for value in parameters]
    if len(parameter_names) != len(set(parameter_names)):
        raise ContractError("parameter names must be unique")
    validate_runtime_parameter_names(parameter_names)
    steps_value = root.get("steps")
    if not isinstance(steps_value, list) or not steps_value:
        raise ContractError("manifest steps must be a non-empty list")
    seen: set[str] = set()
    normalized_steps = [_manifest_step(value, seen) for value in steps_value]
    result = dict(root)
    result["steps"] = normalized_steps
    return result
