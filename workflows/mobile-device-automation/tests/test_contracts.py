from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent_workflow_hub.mobile_device_automation.contracts import (
    ContractError,
    load_manifest,
    load_request,
    plan_payload,
    plan_sha256,
)


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def source_assets(tmp_path: Path) -> dict[str, Path]:
    script = tmp_path / "flow.ad"
    script.write_text(
        "context platform=android\nopen com.example.settings\n",
        encoding="utf-8",
    )
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"\x89PNG\r\n\x1a\nicon")
    result = tmp_path / "result.png"
    result.write_bytes(b"\x89PNG\r\n\x1a\nresult")
    return {"script": script, "icon": icon, "result": result}


@pytest.fixture
def request_dict(source_assets: dict[str, Path]) -> dict[str, object]:
    return {
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
            {"name": "query", "default": "Wi-Fi", "sensitive": False}
        ],
        "steps": [
            {
                "id": "open-settings",
                "runner": "agent-device",
                "effect": "read",
                "timeout_seconds": 15,
                "script": str(source_assets["script"].resolve()),
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
                "template": str(source_assets["icon"].resolve()),
                "threshold": 0.85,
                "assertion": {
                    "kind": "image-visible",
                    "template": str(source_assets["result"].resolve()),
                    "threshold": 0.85,
                    "timeout_seconds": 10,
                },
            },
        ],
        "decision": "generate-and-replay",
        "effective_path_confirmed": True,
    }


@pytest.fixture
def write_request(tmp_path: Path):
    counter = 0

    def _write(value: dict[str, object]) -> Path:
        nonlocal counter
        counter += 1
        path = tmp_path / f"request-{counter}.json"
        path.write_text(
            json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )
        return path.resolve()

    return _write


@pytest.fixture
def request_path(request_dict, write_request) -> Path:
    return write_request(request_dict)


def test_loads_android_mixed_request(request_path: Path) -> None:
    request = load_request(request_path)
    assert request.device.serial == "emulator-5554"
    assert [step.runner for step in request.steps] == ["agent-device", "airtest"]
    assert plan_sha256(request) == plan_sha256(request)


def test_rejects_ios_target(request_dict, write_request) -> None:
    request_dict["target_platform"] = "ios"
    with pytest.raises(ContractError, match="unsupported_platform"):
        load_request(write_request(request_dict))


def test_rejects_relative_ad_script(request_dict, write_request) -> None:
    request_dict["steps"][0]["script"] = "flow.ad"
    with pytest.raises(ContractError, match=r"absolute existing \.ad"):
        load_request(write_request(request_dict))


@pytest.mark.parametrize("directive", ["include child.ad", "runFlow: child.yaml"])
def test_rejects_included_ad_script(
    request_dict, write_request, tmp_path: Path, directive: str
) -> None:
    script = tmp_path / "included.ad"
    script.write_text(f"context platform=android\n{directive}\n", encoding="utf-8")
    request_dict["steps"][0]["script"] = str(script.resolve())
    with pytest.raises(ContractError, match="self-contained"):
        load_request(write_request(request_dict))


def test_rejects_literal_unicode_fill_without_test_ime(
    request_dict, write_request, tmp_path: Path
) -> None:
    script = tmp_path / "unicode.ad"
    script.write_text(
        'context platform=android\nopen com.example.app\nfill "id=search" "示例文本"\n',
        encoding="utf-8",
    )
    request_dict["steps"][0]["script"] = str(script.resolve())
    with pytest.raises(ContractError, match="test_ime_required"):
        load_request(write_request(request_dict))


def test_accepts_literal_unicode_fill_after_test_ime_open(
    request_dict, write_request, tmp_path: Path
) -> None:
    script = tmp_path / "unicode.ad"
    script.write_text(
        'context platform=android\n'
        'open com.example.app --test-ime\n'
        'fill "id=search" "示例文本"\n',
        encoding="utf-8",
    )
    request_dict["steps"][0]["script"] = str(script.resolve())
    assert load_request(write_request(request_dict)).steps[0].script == script.resolve()


def test_rejects_unicode_default_parameter_without_test_ime(
    request_dict, write_request, source_assets: dict[str, Path]
) -> None:
    source_assets["script"].write_text(
        'context platform=android\n'
        'open com.example.app\n'
        'fill "id=search" "${query}"\n',
        encoding="utf-8",
    )
    request_dict["parameters"][0]["default"] = "示例文本"
    with pytest.raises(ContractError, match="test_ime_required"):
        load_request(write_request(request_dict))


def test_rejects_unicode_default_for_runtime_uppercase_parameter_without_test_ime(
    request_dict, write_request, source_assets: dict[str, Path]
) -> None:
    source_assets["script"].write_text(
        'context platform=android\n'
        'open com.example.app\n'
        'fill "id=search" "${QUERY}"\n',
        encoding="utf-8",
    )
    request_dict["parameters"][0]["default"] = "示例文本"
    with pytest.raises(ContractError, match="test_ime_required"):
        load_request(write_request(request_dict))


def test_rejects_agent_device_reserved_parameter_name(
    request_dict, write_request
) -> None:
    request_dict["parameters"][0]["name"] = "ad_secret"
    with pytest.raises(ContractError, match="agent-device runtime"):
        load_request(write_request(request_dict))


def test_rejects_parameter_names_that_collide_at_agent_device_runtime(
    request_dict, write_request
) -> None:
    request_dict["parameters"] = [
        {"name": "foo-bar", "default": "one", "sensitive": False},
        {"name": "foo_bar", "default": "two", "sensitive": False},
    ]
    with pytest.raises(ContractError, match="runtime names must be unique"):
        load_request(write_request(request_dict))


def test_workflow_documents_real_device_unicode_input_contract() -> None:
    skill = (WORKFLOW_ROOT / "SKILL.md").read_text(encoding="utf-8")
    acceptance = (WORKFLOW_ROOT / "references" / "real-device-acceptance.md").read_text(
        encoding="utf-8"
    )
    combined = skill + acceptance
    assert "--test-ime" in combined
    assert "INSTALL_FAILED_USER_RESTRICTED" in combined
    assert "test_ime_required" in combined


@pytest.mark.parametrize(
    ("path", "replacement"),
    [
        (("device", "serial"), "emulator-5556"),
        (("device", "viewport", "density"), 3.0),
        (("steps", 1, "threshold"), 0.91),
    ],
)
def test_plan_digest_changes_with_device_template_or_step(
    request_dict, write_request, path, replacement
) -> None:
    original = load_request(write_request(copy.deepcopy(request_dict)))
    changed_dict = copy.deepcopy(request_dict)
    target = changed_dict
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    changed = load_request(write_request(changed_dict))
    assert plan_sha256(original) != plan_sha256(changed)


def test_plan_payload_excludes_absolute_source_paths(request_path: Path) -> None:
    request = load_request(request_path)
    encoded = json.dumps(plan_payload(request), ensure_ascii=False)
    assert str(request.source.parent) not in encoded
    assert "source_sha256" in encoded


def test_sensitive_parameter_cannot_have_a_default(
    request_dict, write_request
) -> None:
    request_dict["parameters"] = [
        {"name": "password", "sensitive": True, "default": "secret"}
    ]
    with pytest.raises(ContractError, match="sensitive parameter"):
        load_request(write_request(request_dict))


@pytest.mark.parametrize(
    "command",
    [
        "reinstall com.example.app app.apk",
        "uninstall com.example.app",
        "clear-data com.example.app",
        "factory-reset",
    ],
)
def test_destructive_script_cannot_be_declared_read_only(
    request_dict, write_request, tmp_path: Path, command: str
) -> None:
    script = tmp_path / "destructive.ad"
    script.write_text(
        f"context platform=android\n{command}\n",
        encoding="utf-8",
    )
    request_dict["steps"][0].update(
        {"script": str(script.resolve()), "effect": "read"}
    )
    with pytest.raises(ContractError, match="destructive semantic command"):
        load_request(write_request(request_dict))


@pytest.mark.parametrize(
    ("container_path", "field"),
    [
        ((), "unexpected"),
        (("device",), "unexpected"),
        (("device", "viewport"), "unexpected"),
        (("parameters", 0), "unexpected"),
        (("steps", 0), "unexpected"),
        (("steps", 0, "assertion"), "unexpected"),
    ],
)
def test_rejects_unknown_fields(
    request_dict, write_request, container_path, field
) -> None:
    target = request_dict
    for part in container_path:
        target = target[part]
    target[field] = "surprise"
    with pytest.raises(ContractError, match="unknown field"):
        load_request(write_request(request_dict))


@pytest.mark.parametrize("step_id", ["", "UPPER", "space here"])
def test_rejects_blank_or_invalid_step_ids(
    request_dict, write_request, step_id: str
) -> None:
    request_dict["steps"][0]["id"] = step_id
    with pytest.raises(ContractError, match="step.id"):
        load_request(write_request(request_dict))


def test_rejects_duplicate_step_ids(request_dict, write_request) -> None:
    request_dict["steps"][1]["id"] = request_dict["steps"][0]["id"]
    with pytest.raises(ContractError, match="duplicate step id"):
        load_request(write_request(request_dict))


@pytest.mark.parametrize("serial", ["", "bad serial", "host:abc"])
def test_rejects_invalid_serial(request_dict, write_request, serial: str) -> None:
    request_dict["device"].update({"serial": serial, "transport": "usb"})
    with pytest.raises(ContractError, match="serial"):
        load_request(write_request(request_dict))


@pytest.mark.parametrize("serial", ["device-1", "192.0.2.10", "host:70000"])
def test_wireless_transport_requires_host_and_valid_port(
    request_dict, write_request, serial: str
) -> None:
    request_dict["device"].update({"serial": serial, "transport": "wireless"})
    with pytest.raises(ContractError, match="host:port"):
        load_request(write_request(request_dict))


@pytest.mark.parametrize(
    ("container_path", "field", "value"),
    [
        (("device", "viewport"), "density", float("nan")),
        (("steps", 0), "timeout_seconds", float("inf")),
        (("steps", 1), "threshold", float("nan")),
        (("steps", 1, "assertion"), "timeout_seconds", True),
    ],
)
def test_rejects_non_finite_or_boolean_numbers(
    request_dict, write_request, container_path, field, value
) -> None:
    target = request_dict
    for part in container_path:
        target = target[part]
    target[field] = value
    with pytest.raises(ContractError, match=field):
        load_request(write_request(request_dict))


def test_rejects_duplicate_parameter_names(request_dict, write_request) -> None:
    request_dict["parameters"].append(
        {"name": "query", "default": None, "sensitive": False}
    )
    with pytest.raises(ContractError, match="parameter names must be unique"):
        load_request(write_request(request_dict))


def test_click_image_requires_postcondition(request_dict, write_request) -> None:
    del request_dict["steps"][1]["assertion"]
    with pytest.raises(ContractError, match="click-image.*assertion"):
        load_request(write_request(request_dict))


def test_rejects_unknown_effect(request_dict, write_request) -> None:
    request_dict["steps"][0]["effect"] = "unknown"
    with pytest.raises(ContractError, match="effect"):
        load_request(write_request(request_dict))


@pytest.mark.parametrize(("step_index", "field"), [(0, "script"), (1, "template")])
def test_rejects_source_files_larger_than_one_mib(
    request_dict, write_request, tmp_path: Path, step_index: int, field: str
) -> None:
    suffix = ".ad" if field == "script" else ".png"
    oversized = tmp_path / f"oversized{suffix}"
    oversized.write_bytes(b"x" * (1024 * 1024 + 1))
    request_dict["steps"][step_index][field] = str(oversized.resolve())
    with pytest.raises(ContractError, match="1 MiB"):
        load_request(write_request(request_dict))


def test_load_manifest_accepts_relative_assets_and_rejects_unknown_fields(
    tmp_path: Path,
) -> None:
    manifest = {
        "schema_version": "1.0",
        "workflow": "mobile-device-automation",
        "name": "settings-demo",
        "description": "Open Settings.",
        "target_platform": "android",
        "status": "generated-unverified",
        "decision": "generate-only",
        "aggregate_effect": "read",
        "effective_path_confirmed": True,
        "plan_sha256": "a" * 64,
        "generated_at": "2026-10-03T00:00:00Z",
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
        "parameters": [],
        "steps": [
            {
                "id": "open-settings",
                "runner": "agent-device",
                "effect": "read",
                "timeout_seconds": 10.0,
                "script": "scripts/open-settings.ad",
                "source_sha256": "b" * 64,
                "assertion": None,
            }
        ],
    }
    path = tmp_path / "workflow.yaml"
    import yaml

    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    loaded = load_manifest(path.resolve())
    assert loaded["plan_sha256"] == "a" * 64
    assert loaded["steps"][0].get("component") is None

    manifest["unexpected"] = True
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    with pytest.raises(ContractError, match="unknown field"):
        load_manifest(path.resolve())


def test_authoring_schema_validates_request_and_requires_click_assertion(
    request_dict,
) -> None:
    schema = json.loads(
        (WORKFLOW_ROOT / "references" / "automation-request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(request_dict)) == []

    del request_dict["steps"][1]["assertion"]
    assert list(validator.iter_errors(request_dict))


def test_manifest_schema_is_closed_and_accepts_generated_manifest(
    tmp_path: Path,
) -> None:
    schema = json.loads(
        (WORKFLOW_ROOT / "references" / "replay-manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema)
    manifest = {
        "schema_version": "1.0",
        "workflow": "mobile-device-automation",
        "name": "settings-demo",
        "description": "Open Settings.",
        "target_platform": "android",
        "status": "generated-unverified",
        "decision": "generate-only",
        "aggregate_effect": "read",
        "effective_path_confirmed": True,
        "plan_sha256": "a" * 64,
        "generated_at": "2026-10-03T00:00:00Z",
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
        "parameters": [],
        "steps": [
            {
                "id": "open-settings",
                "runner": "agent-device",
                "effect": "read",
                "timeout_seconds": 10.0,
                "script": "scripts/open-settings.ad",
                "source_sha256": "b" * 64,
                "assertion": None,
            }
        ],
    }
    assert list(validator.iter_errors(manifest)) == []
    manifest["unexpected"] = True
    assert list(validator.iter_errors(manifest))


def test_placeholder_example_contains_no_local_host_data() -> None:
    example_path = WORKFLOW_ROOT / "references" / "automation-request.example.json"
    example = json.loads(example_path.read_text(encoding="utf-8"))
    encoded = json.dumps(example, ensure_ascii=False)
    assert example["device"]["serial"] == "emulator-5554"
    assert "com.example.settings" in encoded
    assert "C:/path/to/flow.ad" in encoded
    assert "C:/path/to/icon.png" in encoded
    assert "Administrator" not in encoded
