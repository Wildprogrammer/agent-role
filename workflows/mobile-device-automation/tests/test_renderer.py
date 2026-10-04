from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from agent_workflow_hub.mobile_device_automation.contracts import (
    ContractError,
    load_manifest,
    load_request,
    plan_sha256,
)
from agent_workflow_hub.mobile_device_automation.renderer import compile_bundle
from agent_workflow_hub.mobile_device_automation.models import (
    AssertionSpec,
    ComponentLifecycleSpec,
    ComponentRecoveryStepSpec,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def automation_request(tmp_path: Path):
    script = tmp_path / "open-settings.ad"
    script.write_text("context platform=android\nopen com.example.settings\n", "utf-8")
    icon = tmp_path / "settings-icon.png"
    icon.write_bytes(b"settings-icon")
    result = tmp_path / "result-screen.png"
    result.write_bytes(b"result-screen")
    request_value = {
        "schema_version": "1.0",
        "name": "settings-demo",
        "description": "Open Settings and verify the target screen.",
        "source_surface": "agent-device-cli",
        "target_platform": "android",
        "device": {
            "serial": "emulator-5554",
            "transport": "emulator",
            "viewport": {
                "width": 1080,
                "height": 2400,
                "orientation": "portrait",
                "density": 2.75,
            },
        },
        "parameters": [
            {"name": "query", "default": "demo", "sensitive": False},
            {"name": "password", "default": None, "sensitive": True},
        ],
        "steps": [
            {
                "id": "open-settings",
                "runner": "agent-device",
                "effect": "read",
                "timeout_seconds": 15,
                "script": str(script.resolve()),
                "assertion": {
                    "kind": "selector-visible",
                    "selector": "text=Settings",
                    "timeout_seconds": 10,
                },
            },
            {
                "id": "open-network",
                "runner": "airtest",
                "effect": "read",
                "timeout_seconds": 10,
                "action": "click-image",
                "template": str(icon.resolve()),
                "threshold": 0.85,
                "assertion": {
                    "kind": "image-visible",
                    "template": str(result.resolve()),
                    "threshold": 0.85,
                    "timeout_seconds": 10,
                },
            },
        ],
        "decision": "generate-and-replay",
        "effective_path_confirmed": True,
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request_value), encoding="utf-8")
    return load_request(request_path.resolve())


@pytest.fixture
def component_request(automation_request, tmp_path: Path):
    recovery = tmp_path / "ensure-home-recovery.ad"
    recovery.write_text("context platform=android\nback\n", encoding="utf-8")
    lifecycle = ComponentLifecycleSpec(
        app_id="example-app",
        component_id="ensure-home",
        version=1,
        occurrence_id="c001-ensure-home",
        descriptor_sha256="a" * 64,
        text_input_mode="direct-ime",
        implementation="default",
        parameter_map={},
        precondition=None,
        already_complete=AssertionSpec(
            kind="selector-visible",
            selector="id=home-marker",
            template=None,
            threshold=None,
            timeout_seconds=1.0,
        ),
        recovery=ComponentRecoveryStepSpec(
            script=recovery,
            postcondition=AssertionSpec(
                kind="selector-visible",
                selector="id=home-marker",
                template=None,
                threshold=None,
                timeout_seconds=3.0,
            ),
        ),
    )
    first = replace(automation_request.steps[0], id="ensure-home", component=lifecycle)
    return replace(automation_request, steps=(first,))


def test_compile_creates_non_overwriting_mixed_bundle(automation_request, tmp_path: Path) -> None:
    first = compile_bundle(automation_request, (tmp_path / "outputs").resolve())
    second = compile_bundle(automation_request, (tmp_path / "outputs").resolve())
    assert first.name == "settings-demo-v001"
    assert second.name == "settings-demo-v002"
    manifest = yaml.safe_load((first / "workflow.yaml").read_text("utf-8"))
    assert manifest["status"] == "generated-unverified"
    assert manifest["plan_sha256"] == plan_sha256(automation_request)
    assert (first / "flows/001-open-settings.ad").is_file()
    assert (first / "images/002-settings-icon.png").is_file()
    assert (first / "assertions/002-result-screen.png").is_file()


def test_compile_rejects_parameterized_destructive_script(
    automation_request, tmp_path: Path
) -> None:
    script = tmp_path / "delete-target.ad"
    script.write_text(
        "context platform=android\nclear-data ${query}\n", encoding="utf-8"
    )
    destructive = replace(
        automation_request.steps[0], effect="delete", script=script.resolve()
    )
    request = replace(
        automation_request, steps=(destructive,), aggregate_effect="delete"
    )

    with pytest.raises(ContractError, match="destructive_parameterization_unsupported"):
        compile_bundle(request, (tmp_path / "outputs").resolve())


def test_bundle_contains_relative_paths_and_no_sensitive_defaults(
    automation_request, tmp_path: Path
) -> None:
    bundle = compile_bundle(automation_request, (tmp_path / "outputs").resolve())
    text = (bundle / "workflow.yaml").read_text("utf-8")
    assert str(tmp_path) not in text
    assert "secret-value" not in text
    assert yaml.safe_load(
        (bundle / "parameters.example.yaml").read_text("utf-8")
    ) == {"query": "demo"}
    manifest = load_manifest((bundle / "workflow.yaml").resolve())
    assert manifest["steps"][0]["script"] == "flows/001-open-settings.ad"
    assert manifest["steps"][1]["template"] == "images/002-settings-icon.png"


def test_copied_assets_have_manifest_and_metadata_hashes(automation_request, tmp_path: Path) -> None:
    bundle = compile_bundle(automation_request, (tmp_path / "outputs").resolve())
    manifest = yaml.safe_load((bundle / "workflow.yaml").read_text("utf-8"))
    semantic = bundle / manifest["steps"][0]["script"]
    image = bundle / manifest["steps"][1]["template"]
    assertion = bundle / manifest["steps"][1]["assertion"]["template"]
    assert manifest["steps"][0]["source_sha256"] == _sha256(semantic)
    assert manifest["steps"][1]["source_sha256"] == _sha256(image)
    assert manifest["steps"][1]["assertion"]["source_sha256"] == _sha256(assertion)

    metadata = json.loads((bundle / "metadata.json").read_text("utf-8"))
    assert metadata["tool_versions"] == {
        "agent-device": "0.21.19",
        "airtest": "1.4.3",
    }
    assert {asset["path"] for asset in metadata["assets"]} == {
        "flows/001-open-settings.ad",
        "images/002-settings-icon.png",
        "assertions/002-result-screen.png",
    }
    assert str(tmp_path) not in json.dumps(metadata)


def test_partial_bundle_is_rolled_back_on_copy_error(
    automation_request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_root = (tmp_path / "outputs").resolve()
    original_copy = shutil.copyfile
    calls = 0

    def failing_copy(source, destination, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated copy failure")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copyfile", failing_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        compile_bundle(automation_request, output_root)
    assert output_root.is_dir()
    assert list(output_root.iterdir()) == []


def test_output_root_must_be_absolute(automation_request, tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="output root.*absolute"):
        compile_bundle(automation_request, Path("relative/outputs"))


def test_plan_digest_is_stable_across_generated_timestamps(automation_request, tmp_path: Path) -> None:
    first = compile_bundle(automation_request, (tmp_path / "one").resolve())
    second = compile_bundle(automation_request, (tmp_path / "two").resolve())
    first_manifest = yaml.safe_load((first / "workflow.yaml").read_text("utf-8"))
    second_manifest = yaml.safe_load((second / "workflow.yaml").read_text("utf-8"))
    assert first_manifest["plan_sha256"] == second_manifest["plan_sha256"]
    assert first_manifest["plan_sha256"] == plan_sha256(automation_request)


def test_component_lifecycle_is_rendered_with_recovery_assets(
    component_request, tmp_path: Path
) -> None:
    bundle = compile_bundle(component_request, (tmp_path / "component-outputs").resolve())
    manifest = yaml.safe_load((bundle / "workflow.yaml").read_text("utf-8"))
    component = manifest["steps"][0]["component"]
    assert component == {
        "app_id": "example-app",
        "id": "ensure-home",
        "version": 1,
        "occurrence_id": "c001-ensure-home",
        "descriptor_sha256": "a" * 64,
        "text_input_mode": "direct-ime",
        "implementation": "default",
        "parameter_map": {},
        "precondition": None,
        "already_complete": {
            "kind": "selector-visible",
            "selector": "id=home-marker",
            "timeout_seconds": 1.0,
        },
        "recovery": {
            "script": "flows/001-ensure-home-recovery.ad",
            "source_sha256": _sha256(bundle / "flows/001-ensure-home-recovery.ad"),
            "postcondition": {
                "kind": "selector-visible",
                "selector": "id=home-marker",
                "timeout_seconds": 3.0,
            },
        },
    }
    assert (bundle / "flows/001-ensure-home.ad").is_file()
    assert (bundle / "flows/001-ensure-home-recovery.ad").is_file()
    metadata = json.loads((bundle / "metadata.json").read_text("utf-8"))
    assert "flows/001-ensure-home-recovery.ad" in {
        asset["path"] for asset in metadata["assets"]
    }


def test_component_lifecycle_without_recovery_renders_null(
    component_request, tmp_path: Path
) -> None:
    lifecycle = replace(component_request.steps[0].component, recovery=None)
    step = replace(component_request.steps[0], component=lifecycle)
    request = replace(component_request, steps=(step,))
    bundle = compile_bundle(request, (tmp_path / "without-recovery").resolve())
    manifest = yaml.safe_load((bundle / "workflow.yaml").read_text("utf-8"))
    assert manifest["steps"][0]["component"]["recovery"] is None
