from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import AppSpec, AssertionSpec, AutomationRequest, ParameterSpec, StepSpec


class ContractError(ValueError):
    """Raised when an automation trace cannot be safely compiled."""


LEGACY_SOURCE_SURFACE = "codex-desktop-native-windows-computer-use"
CUA_SOURCE_SURFACE = "cua-driver-mcp"
SOURCE_SURFACES = frozenset({LEGACY_SOURCE_SURFACE, CUA_SOURCE_SURFACE})
SOURCE_SURFACE = LEGACY_SOURCE_SURFACE
EFFECTS = ("none", "read", "idempotent", "create", "submit", "send", "delete", "unknown")
EFFECT_RANK = {name: index for index, name in enumerate(EFFECTS)}
SAFE_RETRY_EFFECTS = {"none", "read", "idempotent"}
ACTIONS = {
    "click-image", "double-click-image", "right-click-image", "click-coordinate",
    "type-text", "press-key", "hotkey", "scroll", "drag-image", "wait-image",
    "copy-text", "paste-text", "manual-step",
}
IMAGE_ACTIONS = {
    "click-image", "double-click-image", "right-click-image", "wait-image",
}
TOP_LEVEL_FIELDS = {
    "schema_version", "name", "description", "source_surface", "target_platform",
    "apps", "parameters", "steps", "success_assertion", "decision",
    "effective_path_confirmed",
}
APP_FIELDS = {
    "alias", "target_kind", "lifecycle", "executable", "launch_executable", "launch_args",
    "window_process_executable", "window_title_regex", "startup_timeout_seconds",
    "shutdown_timeout_seconds", "force_terminate",
}
PARAMETER_FIELDS = {"name", "kind", "default", "sensitive"}
STEP_FIELDS = {
    "id", "app", "action", "effect", "optional", "retries", "timeout_seconds",
    "template", "threshold", "x", "y", "text", "key", "keys", "direction",
    "amount", "from_template", "to_template", "description",
}
ASSERTION_FIELDS = {
    "app", "template", "threshold", "timeout_seconds", "stable_seconds",
}
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
PLACEHOLDER = re.compile(r"\$\{([a-z][a-z0-9_-]{0,63})\}")


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


def _positive_number(value: Any, label: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ContractError(f"{label} must be greater than zero")
    return float(value)


def _threshold(value: Any, label: str, default: float = 0.8) -> float:
    number = default if value is None else value
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not 0 < number <= 1:
        raise ContractError(f"{label} must be greater than zero and at most one")
    return float(number)


def _existing_image(value: Any, label: str) -> Path:
    path = Path(_text(value, label))
    if not path.is_absolute() or not path.is_file():
        raise ContractError(f"{label} must be an existing absolute image path")
    if path.suffix.casefold() not in {".png", ".jpg", ".jpeg"}:
        raise ContractError(f"{label} must be PNG or JPEG")
    return path.resolve()


def _app(value: Any) -> AppSpec:
    item = _object(value, "app")
    _closed(item, APP_FIELDS, "app")
    alias = _identifier(item.get("alias"), "app.alias")
    kind = item.get("target_kind", "application")
    lifecycle = item.get("lifecycle", "reuse")
    if kind not in {"application", "system-dialog"}:
        raise ContractError(f"app {alias} has unsupported target_kind")
    if lifecycle not in {"restart", "reuse", "attach-only"}:
        raise ContractError(f"app {alias} has unsupported lifecycle")
    executable: Path | None = None
    launch_executable: Path | None = None
    window_process_executable: Path | None = None
    if kind == "system-dialog":
        if (
            lifecycle != "attach-only"
            or item.get("executable") not in {None, ""}
            or item.get("launch_executable") not in {None, ""}
            or item.get("window_process_executable") not in {None, ""}
        ):
            raise ContractError(
                "system-dialog must be attach-only and omit executable, launch_executable, "
                "and window_process_executable"
            )
    else:
        executable = Path(_text(item.get("executable"), f"app {alias} executable"))
        if not executable.is_absolute():
            raise ContractError(f"app {alias} executable must be absolute")
        executable = executable.resolve(strict=False)
        launch_value = item.get("launch_executable")
        if launch_value not in {None, ""}:
            launch_executable = Path(
                _text(launch_value, f"app {alias} launch_executable")
            )
            if not launch_executable.is_absolute():
                raise ContractError(f"app {alias} launch_executable must be absolute")
            launch_executable = launch_executable.resolve(strict=False)
        window_process_value = item.get("window_process_executable")
        if window_process_value not in {None, ""}:
            window_process_executable = Path(
                _text(
                    window_process_value,
                    f"app {alias} window_process_executable",
                )
            )
            if not window_process_executable.is_absolute():
                raise ContractError(
                    f"app {alias} window_process_executable must be absolute"
                )
            window_process_executable = window_process_executable.resolve(strict=False)
    launch_args = item.get("launch_args", [])
    if not isinstance(launch_args, list) or any(not isinstance(arg, str) for arg in launch_args):
        raise ContractError(f"app {alias} launch_args must be a string list")
    force = item.get("force_terminate", False)
    if not isinstance(force, bool):
        raise ContractError(f"app {alias} force_terminate must be boolean")
    if force and lifecycle != "restart":
        raise ContractError(f"app {alias} can force terminate only with restart lifecycle")
    return AppSpec(
        alias=alias,
        target_kind=kind,
        lifecycle=lifecycle,
        executable=executable,
        launch_executable=launch_executable,
        window_process_executable=window_process_executable,
        launch_args=tuple(launch_args),
        window_title_regex=_text(item.get("window_title_regex"), f"app {alias} window_title_regex"),
        startup_timeout_seconds=_positive_number(item.get("startup_timeout_seconds"), "startup_timeout_seconds", 30),
        shutdown_timeout_seconds=_positive_number(item.get("shutdown_timeout_seconds"), "shutdown_timeout_seconds", 10),
        force_terminate=force,
    )


def _parameter(value: Any) -> ParameterSpec:
    item = _object(value, "parameter")
    _closed(item, PARAMETER_FIELDS, "parameter")
    name = _identifier(item.get("name"), "parameter.name")
    kind = item.get("kind", "text")
    if kind not in {"text", "path"}:
        raise ContractError(f"parameter {name} has unsupported kind")
    sensitive = item.get("sensitive", False)
    if not isinstance(sensitive, bool):
        raise ContractError(f"parameter {name} sensitive must be boolean")
    default = item.get("default")
    if default is not None and not isinstance(default, str):
        raise ContractError(f"parameter {name} default must be a string")
    if sensitive and default is not None:
        raise ContractError(f"sensitive parameter {name} cannot have a stored default")
    return ParameterSpec(name, kind, default, sensitive)


def _step(
    value: Any,
    apps: set[str],
    parameters: dict[str, ParameterSpec],
    seen: set[str],
) -> StepSpec:
    item = _object(value, "step")
    _closed(item, STEP_FIELDS, "step")
    step_id = _identifier(item.get("id"), "step.id")
    if step_id in seen:
        raise ContractError(f"duplicate step id: {step_id}")
    seen.add(step_id)
    app = _identifier(item.get("app"), f"step {step_id} app")
    if app not in apps:
        raise ContractError(f"step {step_id} references unknown app: {app}")
    action = item.get("action")
    if action not in ACTIONS:
        raise ContractError(f"step {step_id} has unsupported action")
    effect = item.get("effect", "none")
    if effect not in EFFECT_RANK:
        raise ContractError(f"step {step_id} has unsupported effect")
    retries = item.get("retries", 0)
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 5:
        raise ContractError(f"step {step_id} retries must be an integer from zero to five")
    if retries and effect not in SAFE_RETRY_EFFECTS:
        raise ContractError(f"step {step_id} cannot retry a side-effecting or unknown action")
    values = {key: item[key] for key in STEP_FIELDS if key in item}
    if action in IMAGE_ACTIONS:
        values["template"] = str(_existing_image(item.get("template"), f"step {step_id} template"))
        values["threshold"] = _threshold(item.get("threshold"), f"step {step_id} threshold")
    elif action == "click-coordinate":
        for axis in ("x", "y"):
            coordinate = item.get(axis)
            if isinstance(coordinate, bool) or not isinstance(coordinate, int) or coordinate < 0:
                raise ContractError(f"step {step_id} {axis} must be a non-negative integer")
    elif action == "type-text":
        text = item.get("text")
        if not isinstance(text, str):
            raise ContractError(f"step {step_id} text must be a string")
        referenced = set(PLACEHOLDER.findall(text))
        unknown = sorted(referenced - parameters.keys())
        if unknown:
            raise ContractError(f"step {step_id} references unknown parameters: {', '.join(unknown)}")
        sensitive = sorted(name for name in referenced if parameters[name].sensitive)
        if sensitive:
            raise ContractError(
                f"step {step_id} must not automate sensitive parameters: {', '.join(sensitive)}"
            )
    elif action == "press-key":
        _text(item.get("key"), f"step {step_id} key")
    elif action == "hotkey":
        keys = item.get("keys")
        if not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key for key in keys):
            raise ContractError(f"step {step_id} keys must be a non-empty string list")
        modifiers = {"CTRL", "ALT", "SHIFT"}
        if any(key.upper() not in modifiers for key in keys[:-1]) or keys[-1].upper() in modifiers:
            raise ContractError(
                f"step {step_id} hotkey must contain zero or more modifiers followed by one key"
            )
    elif action == "scroll":
        if item.get("direction") not in {"up", "down"}:
            raise ContractError(f"step {step_id} has unsupported scroll direction")
        amount = item.get("amount", 1)
        if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 100:
            raise ContractError(f"step {step_id} scroll amount must be from one to 100")
    elif action == "drag-image":
        values["from_template"] = str(_existing_image(item.get("from_template"), f"step {step_id} from_template"))
        values["to_template"] = str(_existing_image(item.get("to_template"), f"step {step_id} to_template"))
    elif action == "manual-step":
        _text(item.get("description"), f"step {step_id} description")
    optional = item.get("optional", False)
    if not isinstance(optional, bool):
        raise ContractError(f"step {step_id} optional must be boolean")
    return StepSpec(
        id=step_id,
        app=app,
        action=action,
        effect=effect,
        optional=optional,
        retries=retries,
        timeout_seconds=_positive_number(item.get("timeout_seconds"), f"step {step_id} timeout_seconds", 10),
        values=values,
    )


def load_request(path: Path) -> AutomationRequest:
    if not path.is_absolute() or not path.is_file():
        raise ContractError("request path must be an existing absolute file")
    source = path.resolve()
    try:
        root = _object(json.loads(source.read_text(encoding="utf-8")), "request")
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid request JSON: {exc}") from exc
    _closed(root, TOP_LEVEL_FIELDS, "request")
    if root.get("schema_version") != "1.0":
        raise ContractError("schema_version must equal 1.0")
    source_surface = root.get("source_surface")
    if source_surface not in SOURCE_SURFACES:
        raise ContractError(
            "source_surface must be one of: "
            + ", ".join(sorted(SOURCE_SURFACES))
        )
    if root.get("target_platform") != "windows":
        raise ContractError("target_platform must equal windows")
    confirmed = root.get("effective_path_confirmed")
    if confirmed is not True:
        raise ContractError("effective_path_confirmed must be true before compilation")
    decision = root.get("decision")
    if decision not in {"generate-only", "generate-and-replay"}:
        raise ContractError("decision must be generate-only or generate-and-replay")
    raw_apps = root.get("apps")
    if not isinstance(raw_apps, list) or not raw_apps:
        raise ContractError("apps must be a non-empty list")
    apps = tuple(_app(item) for item in raw_apps)
    aliases = [app.alias for app in apps]
    if len(aliases) != len(set(aliases)):
        raise ContractError("app aliases must be unique")
    raw_parameters = root.get("parameters", [])
    if not isinstance(raw_parameters, list):
        raise ContractError("parameters must be a list")
    parameters = tuple(_parameter(item) for item in raw_parameters)
    parameter_names = [item.name for item in parameters]
    if len(parameter_names) != len(set(parameter_names)):
        raise ContractError("parameter names must be unique")
    raw_steps = root.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ContractError("steps must be a non-empty list")
    seen: set[str] = set()
    parameter_map = {item.name: item for item in parameters}
    steps = tuple(_step(item, set(aliases), parameter_map, seen) for item in raw_steps)
    assertion_value = _object(root.get("success_assertion"), "success_assertion")
    _closed(assertion_value, ASSERTION_FIELDS, "success_assertion")
    assertion_app = _identifier(assertion_value.get("app"), "success_assertion.app")
    if assertion_app not in aliases:
        raise ContractError("success_assertion references an unknown app")
    assertion = AssertionSpec(
        app=assertion_app,
        template=_existing_image(assertion_value.get("template"), "success_assertion.template"),
        threshold=_threshold(assertion_value.get("threshold"), "success_assertion.threshold"),
        timeout_seconds=_positive_number(assertion_value.get("timeout_seconds"), "success_assertion.timeout_seconds", 20),
        stable_seconds=_positive_number(assertion_value.get("stable_seconds"), "success_assertion.stable_seconds", 1),
    )
    aggregate_effect = max((step.effect for step in steps), key=EFFECT_RANK.__getitem__)
    return AutomationRequest(
        source=source,
        name=_identifier(root.get("name"), "name"),
        description=_text(root.get("description"), "description"),
        source_surface=root["source_surface"],
        target_platform=root["target_platform"],
        apps=apps,
        parameters=parameters,
        steps=steps,
        success_assertion=assertion,
        decision=decision,
        effective_path_confirmed=True,
        aggregate_effect=aggregate_effect,
    )
