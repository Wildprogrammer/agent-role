from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .models import AutomationRequest


def _next_bundle(output_root: Path, name: str) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    pattern = re.compile(rf"^{re.escape(name)}-v(\d{{3}})\.air$")
    versions = [
        int(match.group(1))
        for child in output_root.iterdir()
        if child.is_dir() and (match := pattern.fullmatch(child.name))
    ]
    return output_root / f"{name}-v{max(versions, default=0) + 1:03d}.air"


def _copy_asset(source: Path, assets: Path, label: str) -> str:
    destination = assets / f"{label}{source.suffix.casefold()}"
    shutil.copy2(source, destination)
    return destination.relative_to(assets.parent).as_posix()


def _manifest(request: AutomationRequest, assets: Path) -> dict[str, object]:
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
                Path(str(item["template"])), assets, f"{index:03d}-{step.id}"
            )
        for key, suffix in (("from_template", "from"), ("to_template", "to")):
            if key in item:
                item[key] = _copy_asset(
                    Path(str(item[key])), assets, f"{index:03d}-{step.id}-{suffix}"
                )
        steps.append(item)

    assertion = asdict(request.success_assertion)
    assertion["template"] = _copy_asset(
        request.success_assertion.template, assets, "success-assertion"
    )
    return {
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
