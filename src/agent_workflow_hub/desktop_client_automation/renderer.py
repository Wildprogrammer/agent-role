from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .models import AssertionSpec, AutomationRequest, DesktopComponentLifecycle, StepSpec


def _next_bundle(output_root: Path, name: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(name)}-v(\d{{3}})\.air$")
    versions = [
        int(match.group(1))
        for child in output_root.iterdir()
        if child.is_dir() and (match := pattern.fullmatch(child.name))
    ]
    return output_root / f"{name}-v{max(versions, default=0) + 1:03d}.air"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_asset(
    source: Path,
    assets: Path,
    label: str,
    records: list[dict[str, object]] | None = None,
) -> str:
    destination = assets / f"{label}{source.suffix.casefold()}"
    shutil.copy2(source, destination)
    relative = destination.relative_to(assets.parent).as_posix()
    if records is not None:
        records.append(
            {
                "path": relative,
                "size": destination.stat().st_size,
                "sha256": _sha256(destination),
            }
        )
    return relative


def _render_assertion(
    assertion: AssertionSpec | None,
    assets: Path,
    label: str,
    records: list[dict[str, object]] | None,
) -> dict[str, object] | None:
    if assertion is None:
        return None
    return {
        "app": assertion.app,
        "template": _copy_asset(assertion.template, assets, label, records),
        "threshold": assertion.threshold,
        "timeout_seconds": assertion.timeout_seconds,
        "stable_seconds": assertion.stable_seconds,
    }


def _render_plain_step(
    step: StepSpec,
    assets: Path,
    label: str,
    records: list[dict[str, object]] | None,
) -> dict[str, object]:
    item: dict[str, object] = {
        "id": step.id,
        "app": step.app,
        "action": step.action,
        "effect": step.effect,
        "optional": step.optional,
        "retries": step.retries,
        "timeout_seconds": step.timeout_seconds,
    }
    item.update(step.values)
    if "template" in item:
        item["template"] = _copy_asset(
            Path(str(item["template"])), assets, label, records
        )
    for key, suffix in (("from_template", "from"), ("to_template", "to")):
        if key in item:
            item[key] = _copy_asset(
                Path(str(item[key])), assets, f"{label}-{suffix}", records
            )
    return item


def _render_component(
    component: DesktopComponentLifecycle,
    assets: Path,
    index: int,
    records: list[dict[str, object]] | None,
) -> dict[str, object]:
    recovery = None
    if component.recovery is not None:
        recovery = {
            "steps": [
                _render_plain_step(
                    step,
                    assets,
                    f"{index:03d}-{component.occurrence_id}-recovery-{step_index:03d}",
                    records,
                )
                for step_index, step in enumerate(component.recovery.steps, start=1)
            ],
            "postcondition": _render_assertion(
                component.recovery.postcondition,
                assets,
                f"{index:03d}-{component.occurrence_id}-recovery-postcondition",
                records,
            ),
        }
    return {
        "app_id": component.app_id,
        "id": component.component_id,
        "version": component.version,
        "occurrence_id": component.occurrence_id,
        "descriptor_sha256": component.descriptor_sha256,
        "implementation": component.implementation,
        "parameter_map": dict(sorted(component.parameter_map.items())),
        "precondition": _render_assertion(
            component.precondition,
            assets,
            f"{index:03d}-{component.occurrence_id}-precondition",
            records,
        ),
        "already_complete": _render_assertion(
            component.already_complete,
            assets,
            f"{index:03d}-{component.occurrence_id}-already-complete",
            records,
        ),
        "recovery": recovery,
        "postcondition": _render_assertion(
            component.postcondition,
            assets,
            f"{index:03d}-{component.occurrence_id}-postcondition",
            records,
        ),
    }


def _manifest(request: AutomationRequest, assets: Path) -> dict[str, object]:
    asset_records: list[dict[str, object]] | None = (
        [] if request.composition is not None else None
    )
    apps = []
    for app in request.apps:
        item = asdict(app)
        item["executable"] = str(app.executable) if app.executable else None
        item["launch_executable"] = (
            str(app.launch_executable) if app.launch_executable else None
        )
        item["window_process_executable"] = (
            str(app.window_process_executable)
            if app.window_process_executable else None
        )
        item["launch_args"] = list(app.launch_args)
        apps.append(item)

    parameters = [asdict(parameter) for parameter in request.parameters]
    steps: list[dict[str, object]] = []
    for index, step in enumerate(request.steps, start=1):
        item = _render_plain_step(
            step,
            assets,
            f"{index:03d}-{step.id}",
            asset_records,
        )
        if step.component is not None:
            item["component"] = _render_component(
                step.component,
                assets,
                index,
                asset_records,
            )
        steps.append(item)

    assertion = _render_assertion(
        request.success_assertion,
        assets,
        "success-assertion",
        asset_records,
    )
    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "workflow": "desktop-client-automation",
        "name": request.name,
        "description": request.description,
        "source_surface": request.source_surface,
        "target_platform": request.target_platform,
        "status": "generated-unverified",
        "decision": request.decision,
        "aggregate_effect": request.aggregate_effect,
        "effective_path_confirmed": request.effective_path_confirmed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_request": str(request.source),
        "apps": apps,
        "parameters": parameters,
        "steps": steps,
        "success_assertion": assertion,
    }
    if request.composition is not None:
        manifest["component_provenance"] = request.composition.to_mapping()
        manifest["assets"] = sorted(
            asset_records or [], key=lambda item: str(item["path"])
        )
        manifest["plan_sha256"] = hashlib.sha256(
            json.dumps(
                _canonical_plan(manifest),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return manifest


def _canonical_plan(manifest: dict[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in manifest.items()
        if key
        not in {
            "generated_at",
            "plan_sha256",
            "source_request",
            "status",
        }
    }


def _script(bundle: Path) -> str:
    return '''from pathlib import Path

from _airtest_runtime import run_bundle


if __name__ == "__main__":
    raise SystemExit(run_bundle(Path(__file__).resolve().parent))
'''


def compile_bundle(request: AutomationRequest, output_root: Path) -> Path:
    if not output_root.is_absolute():
        raise ValueError("output root must be absolute")
    output_root = output_root.resolve()
    bundle = _next_bundle(output_root, request.name)
    bundle.mkdir(parents=False, exist_ok=False)
    assets = bundle / "assets"
    assets.mkdir()
    try:
        manifest = _manifest(request, assets)
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (bundle / f"{bundle.stem}.py").write_text(_script(bundle), encoding="utf-8")
        shutil.copy2(
            Path(__file__).with_name("airtest_runtime.py"),
            bundle / "_airtest_runtime.py",
        )
        if request.composition is not None:
            shutil.copy2(
                Path(__file__).parents[1]
                / "automation_composition"
                / "lifecycle.py",
                bundle / "_automation_lifecycle.py",
            )
        (bundle / "parameters.example.json").write_text(
            json.dumps(
                {
                    item.name: item.default
                    for item in request.parameters
                    if not item.sensitive
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(bundle, ignore_errors=True)
        raise
    return bundle
