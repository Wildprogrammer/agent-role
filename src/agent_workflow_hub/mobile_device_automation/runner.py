from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any

import yaml

from .agent_device_runtime import (
    RuntimeFailure,
    bind_android_device,
    runtime_parameter_values,
)
from .contracts import (
    ContractError,
    load_manifest,
    script_references_parameters,
    validate_test_ime,
)
from .models import ViewportSpec


SHA256 = re.compile(r"^[0-9a-f]{64}$")
SECRET_TEXT = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*([:=])\s*([^\s,;]+)"
)
PASSTHROUGH_FAILURES = {
    "device_not_found",
    "ambiguous_device",
    "device_offline",
    "target_identity_changed",
    "selector_not_found",
    "selector_ambiguous",
    "image_below_threshold",
    "image_not_unique",
    "viewport_changed",
    "replay_diverged",
    "bundle_integrity_mismatch",
    "unsupported_platform",
    "destructive_authorization_required",
    "destructive_parameterization_unsupported",
    "test_ime_required",
    "component_precondition_failed",
    "component_recovery_failed",
    "component_postcondition_failed",
}
NOT_VISIBLE_FAILURES = {"selector_not_found"}


class _PostconditionFailure(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _bundle_root(bundle: Path) -> Path:
    if not bundle.is_absolute() or not bundle.is_dir():
        raise RuntimeFailure("bundle_integrity_mismatch: bundle must be an absolute directory")
    return bundle.resolve()


def _resolve_asset(bundle: Path, relative: object) -> Path:
    if not isinstance(relative, str) or not relative:
        raise RuntimeFailure("bundle_integrity_mismatch: invalid asset path")
    candidate = (bundle / relative).resolve(strict=False)
    try:
        candidate.relative_to(bundle)
    except ValueError as exc:
        raise RuntimeFailure("bundle_integrity_mismatch: asset escaped bundle") from exc
    if not candidate.is_file():
        raise RuntimeFailure("bundle_integrity_mismatch: missing asset")
    return candidate


def _canonical_assertion(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RuntimeFailure("bundle_integrity_mismatch: invalid assertion")
    result: dict[str, object] = {
        "kind": value["kind"],
        "timeout_seconds": value["timeout_seconds"],
    }
    if value["kind"] == "selector-visible":
        result["selector"] = value["selector"]
    else:
        result.update(
            {
                "source_sha256": value["source_sha256"],
                "threshold": value["threshold"],
            }
        )
    return result


def _canonical_plan(manifest: dict[str, object]) -> dict[str, object]:
    canonical_steps: list[dict[str, object]] = []
    for raw in manifest["steps"]:  # type: ignore[union-attr]
        step = dict(raw)
        item: dict[str, object] = {
            "id": step["id"],
            "runner": step["runner"],
            "effect": step["effect"],
            "timeout_seconds": step["timeout_seconds"],
            "assertion": _canonical_assertion(step.get("assertion")),
            "source_sha256": step["source_sha256"],
        }
        if step["runner"] == "airtest":
            item.update({"action": step["action"], "threshold": step["threshold"]})
        component = step.get("component")
        if isinstance(component, dict):
            recovery = component.get("recovery")
            item["component"] = {
                "app_id": component["app_id"],
                "id": component["id"],
                "version": component["version"],
                "occurrence_id": component["occurrence_id"],
                "descriptor_sha256": component["descriptor_sha256"],
                "text_input_mode": component["text_input_mode"],
                "implementation": component["implementation"],
                "parameter_map": dict(sorted(component["parameter_map"].items())),
                "precondition": _canonical_assertion(component.get("precondition")),
                "already_complete": _canonical_assertion(
                    component.get("already_complete")
                ),
                "recovery": (
                    None
                    if recovery is None
                    else {
                        "source_sha256": recovery["source_sha256"],
                        "postcondition": _canonical_assertion(
                            recovery.get("postcondition")
                        ),
                    }
                ),
            }
        canonical_steps.append(item)
    return {
        "schema_version": manifest["schema_version"],
        "workflow": manifest["workflow"],
        "name": manifest["name"],
        "description": manifest["description"],
        "target_platform": manifest["target_platform"],
        "device": manifest["device"],
        "parameters": manifest["parameters"],
        "steps": canonical_steps,
        "decision": manifest["decision"],
        "effective_path_confirmed": manifest["effective_path_confirmed"],
        "aggregate_effect": manifest["aggregate_effect"],
    }


def _manifest_plan_sha256(manifest: dict[str, object]) -> str:
    encoded = json.dumps(
        _canonical_plan(manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_assets(bundle: Path, manifest: dict[str, object]) -> None:
    for raw in manifest["steps"]:  # type: ignore[union-attr]
        step = dict(raw)
        primary_key = "script" if step["runner"] == "agent-device" else "template"
        primary = _resolve_asset(bundle, step[primary_key])
        if _sha256(primary) != step["source_sha256"]:
            raise RuntimeFailure("bundle_integrity_mismatch: primary asset hash changed")
        assertion = step.get("assertion")
        if isinstance(assertion, dict) and assertion.get("kind") == "image-visible":
            asset = _resolve_asset(bundle, assertion["template"])
            if _sha256(asset) != assertion["source_sha256"]:
                raise RuntimeFailure("bundle_integrity_mismatch: assertion asset hash changed")
        component = step.get("component")
        if isinstance(component, dict) and isinstance(component.get("recovery"), dict):
            recovery = component["recovery"]
            asset = _resolve_asset(bundle, recovery["script"])
            if _sha256(asset) != recovery["source_sha256"]:
                raise RuntimeFailure("bundle_integrity_mismatch: recovery asset hash changed")


def _load_valid_bundle(bundle: Path) -> tuple[Path, dict[str, object]]:
    root = _bundle_root(bundle)
    try:
        manifest = load_manifest((root / "workflow.yaml").resolve())
    except (ContractError, OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeFailure("bundle_integrity_mismatch: invalid manifest") from exc
    _validate_assets(root, manifest)
    parameter_names = [item["name"] for item in manifest["parameters"]]
    for step in manifest["steps"]:
        if step["effect"] != "delete" or step["runner"] != "agent-device":
            continue
        scripts = [_resolve_asset(root, step["script"])]
        component = step.get("component")
        if isinstance(component, dict) and isinstance(component.get("recovery"), dict):
            scripts.append(_resolve_asset(root, component["recovery"]["script"]))
        try:
            parameterized = any(
                script_references_parameters(script, parameter_names)
                for script in scripts
            )
        except ContractError as exc:
            raise RuntimeFailure("bundle_integrity_mismatch: invalid script") from exc
        if parameterized:
            raise RuntimeFailure("destructive_parameterization_unsupported")
    computed = _manifest_plan_sha256(manifest)
    if computed != manifest["plan_sha256"]:
        raise RuntimeFailure("bundle_integrity_mismatch: plan digest changed")
    return root, manifest


def preview_bundle(bundle: Path) -> dict[str, object]:
    _, manifest = _load_valid_bundle(bundle)
    steps = []
    for step in manifest["steps"]:  # type: ignore[union-attr]
        summary = {
            "id": step["id"],
            "runner": step["runner"],
            "effect": step["effect"],
        }
        component = step.get("component")
        if isinstance(component, dict):
            summary["component"] = {
                "app_id": component["app_id"],
                "id": component["id"],
                "version": component["version"],
                "occurrence_id": component["occurrence_id"],
            }
        steps.append(summary)
    device = manifest["device"]
    return {
        "status": "preview-ready",
        "serial": device["serial"],  # type: ignore[index]
        "transport": device["transport"],  # type: ignore[index]
        "steps": steps,
        "destructive_step_ids": [
            step["id"] for step in steps if step["effect"] == "delete"
        ],
        "plan_sha256": manifest["plan_sha256"],
    }


def _load_parameters(
    manifest: dict[str, object], parameters_path: Path | None
) -> tuple[dict[str, str], list[str]]:
    supplied: dict[str, object] = {}
    if parameters_path is not None:
        if not parameters_path.is_absolute() or not parameters_path.is_file():
            raise RuntimeFailure("invalid_parameters: parameter file must be absolute")
        try:
            loaded = yaml.safe_load(parameters_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RuntimeFailure("invalid_parameters: cannot parse parameter file") from exc
        if loaded is None:
            loaded = {}
        if not isinstance(loaded, dict):
            raise RuntimeFailure("invalid_parameters: expected a mapping")
        supplied = loaded

    definitions = manifest["parameters"]
    known = {definition["name"] for definition in definitions}  # type: ignore[union-attr]
    unknown = sorted(set(supplied) - known)
    if unknown:
        raise RuntimeFailure("invalid_parameters: unknown parameter")
    resolved: dict[str, str] = {}
    sensitive_values: list[str] = []
    for definition in definitions:  # type: ignore[union-attr]
        name = definition["name"]
        if name in supplied:
            value = supplied[name]
        else:
            value = definition["default"]
        if not isinstance(value, str):
            raise RuntimeFailure("invalid_parameters: missing parameter")
        resolved[name] = value
        if definition["sensitive"] and value:
            sensitive_values.append(value)
    return resolved, sensitive_values


def _viewport(device: dict[str, object]) -> ViewportSpec:
    value = device["viewport"]
    return ViewportSpec(
        width=value["width"],  # type: ignore[index]
        height=value["height"],  # type: ignore[index]
        orientation=value["orientation"],  # type: ignore[index]
        density=value["density"],  # type: ignore[index]
    )


def _same_viewport(left: ViewportSpec | None, right: ViewportSpec) -> bool:
    return left == right


def _evidence_directory(bundle: Path) -> Path:
    first = bundle / "evidence"
    try:
        first.mkdir(exist_ok=False)
        return first
    except FileExistsError:
        pass
    for version in range(2, 1000):
        candidate = bundle / f"evidence-v{version:03d}"
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeFailure("runtime_failed: no evidence directory available")


def _relative_evidence_paths(value: object, bundle: Path) -> list[str]:
    if not isinstance(value, dict):
        return []
    paths: list[str] = []
    for key, raw in value.items():
        if not key.endswith("_path") or not isinstance(raw, str):
            continue
        try:
            relative = Path(raw).resolve().relative_to(bundle)
        except (OSError, ValueError):
            continue
        paths.append(relative.as_posix())
    return paths


def _redact_text(text: str, sensitive_values: list[str]) -> str:
    value = " ".join(text.split())
    for secret in sensitive_values:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = SECRET_TEXT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return value[:768]


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, _PostconditionFailure):
        return "postcondition_failed"
    message = str(exc)
    token = re.split(r"[:\s]", message, maxsplit=1)[0]
    if token in PASSTHROUGH_FAILURES:
        return token
    if token in {"agent_device_missing", "needs_agent_device", "needs_airtest"}:
        return "environment_missing"
    return "runtime_failed"


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_record(path: Path, value: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def _set_manifest_status(bundle: Path, status: str) -> None:
    path = bundle / "workflow.yaml"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or "status" not in value:
            return
        value["status"] = status
        temporary = path.with_name("workflow.yaml.tmp")
        temporary.write_text(
            yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        temporary.replace(path)
    except (OSError, UnicodeError, yaml.YAMLError):
        return


def _materialize_selector(selector: str, parameters: dict[str, str]) -> str:
    aliases: dict[str, str] = {}
    runtime_values = runtime_parameter_values(parameters)
    for name, value in parameters.items():
        aliases[name] = value
    aliases.update(runtime_values)

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in aliases:
            raise RuntimeFailure("invalid_parameters: selector parameter is missing")
        encoded = json.dumps(aliases[name], ensure_ascii=False)
        quoted = (
            match.start() > 0
            and match.end() < len(selector)
            and selector[match.start() - 1] == '"'
            and selector[match.end()] == '"'
        )
        return encoded[1:-1] if quoted else encoded

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_-]*)\}", replace, selector)


def _execute_assertion(
    assertion: dict[str, object] | None,
    *,
    bundle: Path,
    serial: str,
    session: str,
    state_dir: Path | None,
    viewport: ViewportSpec,
    agent_runtime: object,
    airtest_runtime: object,
    parameters: dict[str, str],
) -> dict[str, object] | None:
    if assertion is None:
        return None
    if assertion["kind"] == "selector-visible":
        return agent_runtime.wait_selector(
            _materialize_selector(assertion["selector"], parameters),
            serial=serial,
            session=session,
            timeout_seconds=assertion["timeout_seconds"],
            state_dir=state_dir,
        )
    template = _resolve_asset(bundle, assertion["template"])
    return airtest_runtime.assert_image_visible(
        serial=serial,
        expected_viewport=viewport,
        template_path=template,
        threshold=assertion["threshold"],
        timeout_seconds=assertion["timeout_seconds"],
    )


def _probe_assertion(
    execute: Callable[[], dict[str, object] | None],
) -> tuple[bool, dict[str, object] | None]:
    try:
        return True, execute()
    except RuntimeFailure as exc:
        if _failure_code(exc) in NOT_VISIBLE_FAILURES:
            return False, None
        raise


def _run_assertion(
    assertion: dict[str, object] | None,
    **kwargs: object,
) -> dict[str, object] | None:
    try:
        return _execute_assertion(assertion, **kwargs)
    except Exception as exc:
        raise _PostconditionFailure(str(exc)) from exc


def run_bundle(
    bundle: Path,
    *,
    parameters_path: Path | None = None,
    confirmed_plan_sha256: str | None = None,
    agent_runtime: object | None = None,
    airtest_runtime: object | None = None,
) -> int:
    root = bundle.resolve(strict=False) if bundle.is_absolute() else bundle
    manifest: dict[str, object] | None = None
    evidence: Path | None = None
    records_path: Path | None = None
    summaries: list[dict[str, object]] = []
    sensitive_values: list[str] = []
    session: str | None = None
    session_state_dir: Path | None = None
    session_used = False
    primary_error: BaseException | None = None

    try:
        root, manifest = _load_valid_bundle(bundle)
        digest = manifest["plan_sha256"]
        destructive = any(
            step["effect"] == "delete" for step in manifest["steps"]  # type: ignore[union-attr]
        )
        if destructive and (
            not isinstance(confirmed_plan_sha256, str)
            or not SHA256.fullmatch(confirmed_plan_sha256)
            or confirmed_plan_sha256 != digest
        ):
            raise RuntimeFailure("destructive_authorization_required")

        if agent_runtime is None or airtest_runtime is None:
            raise RuntimeFailure("environment_missing")
        device_value = manifest["device"]
        serial = device_value["serial"]  # type: ignore[index]
        devices = agent_runtime.devices()
        selected = bind_android_device(devices, requested_serial=serial)
        expected_viewport = _viewport(device_value)  # type: ignore[arg-type]
        actual_viewport = selected.viewport
        if actual_viewport is None:
            actual_viewport = airtest_runtime.inspect_viewport(serial)
        if not _same_viewport(actual_viewport, expected_viewport):
            raise RuntimeFailure("target_identity_changed: viewport identity changed")
        parameters, sensitive_values = _load_parameters(manifest, parameters_path)
        for step in manifest["steps"]:  # type: ignore[union-attr]
            if step["runner"] == "agent-device":
                validate_test_ime(_resolve_asset(root, step["script"]), parameters)
        evidence = _evidence_directory(root)
        records_path = evidence / "tool-results.jsonl"
        records_path.touch(exist_ok=False)
        session = f"mobile-{str(digest)[:12]}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"

        for index, step in enumerate(manifest["steps"], start=1):  # type: ignore[union-attr]
            started = _utc_now()
            step_id = step["id"]
            runner_name = step["runner"]
            action_result: dict[str, object] | None = None
            recovery_result: dict[str, object] | None = None
            lifecycle_branch: str | None = None
            try:
                component = step.get("component")
                if isinstance(component, dict):
                    session_used = True
                    if session_state_dir is None:
                        session_state_dir = evidence / "ad-state"
                        session_state_dir.mkdir(exist_ok=False)

                    already_complete = component.get("already_complete")
                    already_visible = False
                    already_evidence: dict[str, object] | None = None
                    if isinstance(already_complete, dict):
                        already_visible, already_evidence = _probe_assertion(
                            lambda: _execute_assertion(
                                already_complete,
                                bundle=root,
                                serial=serial,
                                session=session,
                                state_dir=session_state_dir,
                                viewport=expected_viewport,
                                agent_runtime=agent_runtime,
                                airtest_runtime=airtest_runtime,
                                parameters=parameters,
                            )
                        )
                    if already_visible:
                        lifecycle_branch = "already-complete"
                        summaries.append(
                            {
                                "id": step_id,
                                "runner": runner_name,
                                "status": "already-complete",
                            }
                        )
                        _append_record(
                            records_path,
                            {
                                "step_id": step_id,
                                "runner": runner_name,
                                "started_at": started,
                                "finished_at": _utc_now(),
                                "status": "already-complete",
                                "source_sha256": step["source_sha256"],
                                "component": {
                                    "id": component["id"],
                                    "version": component["version"],
                                    "occurrence_id": component["occurrence_id"],
                                },
                                "lifecycle_branch": lifecycle_branch,
                                "recovery_status": None,
                                "evidence_paths": _relative_evidence_paths(
                                    already_evidence, root
                                ),
                            },
                        )
                        continue

                    precondition = component.get("precondition")
                    precondition_visible = True
                    if isinstance(precondition, dict):
                        precondition_visible, _ = _probe_assertion(
                            lambda: _execute_assertion(
                                precondition,
                                bundle=root,
                                serial=serial,
                                session=session,
                                state_dir=session_state_dir,
                                viewport=expected_viewport,
                                agent_runtime=agent_runtime,
                                airtest_runtime=airtest_runtime,
                                parameters=parameters,
                            )
                        )
                    if not precondition_visible:
                        recovery = component.get("recovery")
                        if isinstance(recovery, dict):
                            lifecycle_branch = "recovered"
                            recovery_script = _resolve_asset(root, recovery["script"])
                            recovery_result = agent_runtime.replay_ad(
                                recovery_script,
                                serial=serial,
                                session=session,
                                parameters=parameters,
                                state_dir=session_state_dir,
                            )
                            returned_state_dir = recovery_result.get("_state_dir")
                            if isinstance(returned_state_dir, str):
                                session_state_dir = Path(returned_state_dir)
                            if recovery_result.get("status") in {"diverged", "failed"}:
                                raise RuntimeFailure("component_recovery_failed")
                            try:
                                _execute_assertion(
                                    recovery.get("postcondition"),
                                    bundle=root,
                                    serial=serial,
                                    session=session,
                                    state_dir=session_state_dir,
                                    viewport=expected_viewport,
                                    agent_runtime=agent_runtime,
                                    airtest_runtime=airtest_runtime,
                                    parameters=parameters,
                                )
                            except Exception as exc:
                                raise RuntimeFailure(
                                    "component_recovery_failed"
                                ) from exc
                            if isinstance(precondition, dict):
                                precondition_visible, _ = _probe_assertion(
                                    lambda: _execute_assertion(
                                        precondition,
                                        bundle=root,
                                        serial=serial,
                                        session=session,
                                        state_dir=session_state_dir,
                                        viewport=expected_viewport,
                                        agent_runtime=agent_runtime,
                                        airtest_runtime=airtest_runtime,
                                        parameters=parameters,
                                    )
                                )
                        if not precondition_visible:
                            raise RuntimeFailure("component_precondition_failed")
                    if lifecycle_branch is None:
                        lifecycle_branch = "precondition-satisfied"

                if runner_name == "agent-device":
                    session_used = True
                    if session_state_dir is None:
                        session_state_dir = evidence / "ad-state"
                        session_state_dir.mkdir(exist_ok=False)
                    script = _resolve_asset(root, step["script"])
                    action_result = agent_runtime.replay_ad(
                        script,
                        serial=serial,
                        session=session,
                        parameters=parameters,
                        state_dir=session_state_dir,
                    )
                    returned_state_dir = action_result.get("_state_dir")
                    if isinstance(returned_state_dir, str):
                        session_state_dir = Path(returned_state_dir)
                    if action_result.get("status") in {"diverged", "failed"} or action_result.get(
                        "error_code"
                    ) == "replay_diverged":
                        raise RuntimeFailure("replay_diverged")
                elif step["action"] == "click-image":
                    template = _resolve_asset(root, step["template"])
                    evidence_stem = f"s{index:03d}"
                    action_result = airtest_runtime.click_image(
                        serial=serial,
                        expected_viewport=expected_viewport,
                        template_path=template,
                        evidence_dir=evidence / "steps" / evidence_stem,
                        action_id=step_id,
                        evidence_stem=evidence_stem,
                        threshold=step["threshold"],
                        timeout_seconds=step["timeout_seconds"],
                    )
                else:
                    template = _resolve_asset(root, step["template"])
                    action_result = airtest_runtime.locate_image(
                        serial=serial,
                        expected_viewport=expected_viewport,
                        template_path=template,
                        threshold=step["threshold"],
                        timeout_seconds=step["timeout_seconds"],
                    )
                try:
                    _run_assertion(
                        step.get("assertion"),
                        bundle=root,
                        serial=serial,
                        session=session,
                        state_dir=session_state_dir,
                        viewport=expected_viewport,
                        agent_runtime=agent_runtime,
                        airtest_runtime=airtest_runtime,
                        parameters=parameters,
                    )
                except _PostconditionFailure as exc:
                    if isinstance(component, dict):
                        raise RuntimeFailure("component_postcondition_failed") from exc
                    raise
                status = "component-verified" if isinstance(component, dict) else "passed"
                summary = {"id": step_id, "runner": runner_name, "status": status}
                summaries.append(summary)
                record: dict[str, object] = {
                        "step_id": step_id,
                        "runner": runner_name,
                        "started_at": started,
                        "finished_at": _utc_now(),
                        "status": status,
                        "source_sha256": step["source_sha256"],
                        "evidence_paths": _relative_evidence_paths(action_result, root),
                        "match": {
                            key: action_result[key]
                            for key in ("confidence", "match_count", "position", "threshold")
                            if action_result is not None and key in action_result
                        },
                    }
                if isinstance(component, dict):
                    record.update(
                        {
                            "component": {
                                "id": component["id"],
                                "version": component["version"],
                                "occurrence_id": component["occurrence_id"],
                            },
                            "lifecycle_branch": lifecycle_branch,
                            "recovery_status": (
                                recovery_result.get("status")
                                if recovery_result is not None
                                else None
                            ),
                        }
                    )
                _append_record(records_path, record)
            except Exception as exc:
                summaries.append({"id": step_id, "runner": runner_name, "status": "failed"})
                _append_record(
                    records_path,
                    {
                        "step_id": step_id,
                        "runner": runner_name,
                        "started_at": started,
                        "finished_at": _utc_now(),
                        "status": "failed",
                        "error_code": _failure_code(exc),
                        "detail": _redact_text(str(exc), sensitive_values),
                        "source_sha256": step["source_sha256"],
                        "evidence_paths": _relative_evidence_paths(action_result, root),
                    },
                )
                raise

    except Exception as exc:
        primary_error = exc
    finally:
        if session_used and session is not None and agent_runtime is not None:
            try:
                agent_runtime.close_session(
                    session,
                    serial=serial,
                    state_dir=session_state_dir,
                )
            except Exception as close_exc:
                if primary_error is None:
                    primary_error = close_exc

    if primary_error is None and manifest is not None:
        _set_manifest_status(root, "replay-verified")
        result = {
            "status": "replay-verified",
            "plan_sha256": manifest["plan_sha256"],
            "device_transport": manifest["device"]["transport"],  # type: ignore[index]
            "steps": summaries,
            "evidence_paths": [
                records_path.relative_to(root).as_posix() if records_path else ""
            ],
        }
        _write_json(root / "run-result.json", result)
        return 0

    error_code = _failure_code(primary_error or RuntimeFailure("runtime_failed"))
    _set_manifest_status(root, "replay-failed")
    result = {
        "status": "replay-failed",
        "error_code": error_code,
        "detail": _redact_text(str(primary_error or "runtime_failed"), sensitive_values),
        "plan_sha256": manifest.get("plan_sha256") if manifest else None,
        "device_transport": (
            manifest["device"]["transport"] if manifest is not None else None  # type: ignore[index]
        ),
        "steps": summaries,
        "evidence_paths": (
            [records_path.relative_to(root).as_posix()] if records_path is not None else []
        ),
    }
    try:
        root.mkdir(parents=True, exist_ok=True)
        _write_json(root / "run-result.json", result)
    except OSError:
        pass
    return 2
