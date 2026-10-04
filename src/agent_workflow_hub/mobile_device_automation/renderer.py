from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import yaml

from .contracts import ContractError, plan_sha256, script_references_parameters
from .models import AssertionSpec, AutomationRequest, StepSpec


MAX_BUNDLE_VERSIONS = 999
SAFE_STEM = re.compile(r"[^a-z0-9_-]+")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _slug_stem(path: Path, fallback: str) -> str:
    value = SAFE_STEM.sub("-", path.stem.casefold()).strip("-_")
    return value or fallback


def _allocate_bundle(output_root: Path, name: str) -> Path:
    if not output_root.is_absolute():
        raise ContractError("output root must be absolute")
    root = output_root.resolve(strict=False)
    if root.exists() and not root.is_dir():
        raise ContractError("output root must be a directory")
    root.mkdir(parents=True, exist_ok=True)
    for version in range(1, MAX_BUNDLE_VERSIONS + 1):
        candidate = root / f"{name}-v{version:03d}"
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise ContractError("no bundle version is available")


def _copy_asset(
    source: Path,
    bundle: Path,
    relative: Path,
    assets: list[dict[str, str]],
) -> tuple[str, str]:
    destination = bundle / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    digest = _sha256(destination)
    posix_path = relative.as_posix()
    assets.append({"path": posix_path, "sha256": digest})
    return posix_path, digest


def _render_assertion(
    assertion: AssertionSpec | None,
    *,
    step: StepSpec,
    index: int,
    bundle: Path,
    assets: list[dict[str, str]],
) -> dict[str, object] | None:
    if assertion is None:
        return None
    if assertion.kind == "selector-visible":
        return {
            "kind": assertion.kind,
            "selector": assertion.selector,
            "timeout_seconds": assertion.timeout_seconds,
        }
    if assertion.template is None or assertion.threshold is None:
        raise ContractError(f"step {step.id} has an incomplete image assertion")
    stem = _slug_stem(assertion.template, f"{step.id}-assertion")
    relative = Path("assertions") / f"{index:03d}-{stem}{assertion.template.suffix.casefold()}"
    copied_path, digest = _copy_asset(assertion.template, bundle, relative, assets)
    return {
        "kind": assertion.kind,
        "template": copied_path,
        "source_sha256": digest,
        "threshold": assertion.threshold,
        "timeout_seconds": assertion.timeout_seconds,
    }


def _render_step(
    step: StepSpec,
    *,
    index: int,
    bundle: Path,
    assets: list[dict[str, str]],
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": step.id,
        "runner": step.runner,
        "effect": step.effect,
        "timeout_seconds": step.timeout_seconds,
    }
    if step.runner == "agent-device":
        if step.script is None:
            raise ContractError(f"step {step.id} has no semantic script")
        relative = Path("flows") / f"{index:03d}-{step.id}.ad"
        copied_path, digest = _copy_asset(step.script, bundle, relative, assets)
        item.update({"script": copied_path, "source_sha256": digest})
        if step.component is not None:
            component = step.component
            recovery_value: dict[str, object] | None = None
            if component.recovery is not None:
                recovery_relative = (
                    Path("flows") / f"{index:03d}-{step.id}-recovery.ad"
                )
                recovery_path, recovery_digest = _copy_asset(
                    component.recovery.script,
                    bundle,
                    recovery_relative,
                    assets,
                )
                recovery_value = {
                    "script": recovery_path,
                    "source_sha256": recovery_digest,
                    "postcondition": _render_assertion(
                        component.recovery.postcondition,
                        step=step,
                        index=index,
                        bundle=bundle,
                        assets=assets,
                    ),
                }
            item["component"] = {
                "app_id": component.app_id,
                "id": component.component_id,
                "version": component.version,
                "occurrence_id": component.occurrence_id,
                "descriptor_sha256": component.descriptor_sha256,
                "text_input_mode": component.text_input_mode,
                "implementation": component.implementation,
                "parameter_map": dict(sorted(component.parameter_map.items())),
                "precondition": _render_assertion(
                    component.precondition,
                    step=step,
                    index=index,
                    bundle=bundle,
                    assets=assets,
                ),
                "already_complete": _render_assertion(
                    component.already_complete,
                    step=step,
                    index=index,
                    bundle=bundle,
                    assets=assets,
                ),
                "recovery": recovery_value,
            }
    else:
        if step.template is None or step.action is None or step.threshold is None:
            raise ContractError(f"step {step.id} has an incomplete image action")
        stem = _slug_stem(step.template, step.id)
        relative = Path("images") / f"{index:03d}-{stem}{step.template.suffix.casefold()}"
        copied_path, digest = _copy_asset(step.template, bundle, relative, assets)
        item.update(
            {
                "action": step.action,
                "template": copied_path,
                "source_sha256": digest,
                "threshold": step.threshold,
            }
        )
    item["assertion"] = _render_assertion(
        step.assertion,
        step=step,
        index=index,
        bundle=bundle,
        assets=assets,
    )
    return item


def _write_yaml(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(value, handle, sort_keys=False, allow_unicode=True)


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def compile_bundle(request: AutomationRequest, output_root: Path) -> Path:
    parameter_names = [parameter.name for parameter in request.parameters]
    for step in request.steps:
        if step.effect != "delete" or step.runner != "agent-device":
            continue
        scripts = [step.script]
        if step.component is not None and step.component.recovery is not None:
            scripts.append(step.component.recovery.script)
        if any(
            script is not None
            and script_references_parameters(script, parameter_names)
            for script in scripts
        ):
            raise ContractError("destructive_parameterization_unsupported")
    bundle = _allocate_bundle(output_root, request.name)
    try:
        assets: list[dict[str, str]] = []
        steps = [
            _render_step(
                step,
                index=index,
                bundle=bundle,
                assets=assets,
            )
            for index, step in enumerate(request.steps, start=1)
        ]
        generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        manifest = {
            "schema_version": "1.0",
            "workflow": "mobile-device-automation",
            "name": request.name,
            "description": request.description,
            "target_platform": request.target_platform,
            "status": "generated-unverified",
            "decision": request.decision,
            "aggregate_effect": request.aggregate_effect,
            "effective_path_confirmed": request.effective_path_confirmed,
            "plan_sha256": plan_sha256(request),
            "generated_at": generated_at,
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
        }
        _write_yaml(bundle / "workflow.yaml", manifest)
        _write_json(
            bundle / "metadata.json",
            {
                "schema_version": "1.0",
                "workflow": "mobile-device-automation",
                "generated_at": generated_at,
                "plan_sha256": manifest["plan_sha256"],
                "tool_versions": {
                    "agent-device": "0.21.19",
                    "airtest": "1.4.3",
                },
                "assets": assets,
            },
        )
        parameter_examples = {
            parameter.name: parameter.default
            for parameter in request.parameters
            if not parameter.sensitive and parameter.default is not None
        }
        _write_yaml(bundle / "parameters.example.yaml", parameter_examples)
        return bundle
    except Exception:
        shutil.rmtree(bundle)
        raise
