from __future__ import annotations

import json
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from agent_workflow_hub.mobile_device_automation.composition_contracts import (
    load_component,
    load_composition_request,
    load_scenario,
)
from agent_workflow_hub.mobile_device_automation.composition import compose_bundle
from agent_workflow_hub.contracts import workflow_entrypoints
from agent_workflow_hub.frontmatter import parse_markdown
from agent_workflow_hub.repository import REQUIRED_HEADINGS, validate_skill


SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
ROOT = SKILL.parents[2]
README = ROOT / "README.md"
EXAMPLE = SKILL.parent / "references" / "automation-request.example.json"
RUNBOOK = SKILL.parent / "references" / "real-device-acceptance.md"
REFERENCES = SKILL.parent / "references"


@dataclass(frozen=True)
class MaterializedCatalog:
    request: Path
    catalog_root: Path


def materialize_example_catalog(
    reference_root: Path, tmp_path: Path
) -> MaterializedCatalog:
    catalog = (tmp_path / "example-app").resolve()
    version = catalog / "components" / "ensure-home" / "v001"
    scenarios = catalog / "scenarios"
    version.mkdir(parents=True)
    scenarios.mkdir(parents=True)
    shutil.copyfile(reference_root / "app-component.example.yaml", version / "component.yaml")
    shutil.copyfile(
        reference_root / "app-component-action.example.ad",
        version / "app-component-action.example.ad",
    )
    shutil.copyfile(reference_root / "app-scenario.example.yaml", scenarios / "home.yaml")
    request_value = json.loads(
        (reference_root / "app-composition-request.example.json").read_text("utf-8")
    )
    request_value["catalog_root"] = str(catalog)
    request_value["scenario"] = str((scenarios / "home.yaml").resolve())
    request = (tmp_path / "composition-request.json").resolve()
    request.write_text(json.dumps(request_value), encoding="utf-8")
    return MaterializedCatalog(request=request, catalog_root=catalog)


def test_mobile_workflow_metadata_and_entrypoints() -> None:
    frontmatter, body = parse_markdown(SKILL)
    contract = validate_skill(SKILL, frontmatter, body)

    assert contract.name == "mobile-device-automation"
    assert json.loads(contract.metadata["required-capabilities"]) == [
        "cli.agent-device"
    ]
    assert json.loads(contract.metadata["capability-slots"]) == {
        "image-replay": ["python.airtest"]
    }
    assert set(workflow_entrypoints(contract.metadata)) == {
        "doctor",
        "devices",
        "capture",
        "locate-image",
        "click-image",
        "compile",
        "compose",
        "preview",
        "replay",
    }
    for heading in REQUIRED_HEADINGS:
        assert f"## {heading}" in body


def test_workflow_documents_driver_and_safety_boundaries() -> None:
    body = SKILL.read_text(encoding="utf-8")

    for required in (
        "agent-device",
        "Airtest",
        "USB ADB",
        "无线 ADB",
        "模拟器",
        "图片失败不得静默回退",
        "ambiguous_device",
        "plan_sha256",
        "DeviceFarmer/STF",
        "首版不支持 iOS",
        "replay-verified",
        "Hub 不代理通用 MCP 工具",
        "状态转换组件",
        "精确不可变版本",
        "编译期展开",
        "最多执行一次恢复",
        "direct-ime",
        "pinyin-fallback",
    ):
        assert required in body


def test_root_catalog_and_pytest_entry_register_the_workflow() -> None:
    root_skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert root_skill.count("`mobile-device-automation`") == 1

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    testpaths = pyproject["tool"]["pytest"]["ini_options"]["testpaths"]
    assert testpaths.count("workflows/mobile-device-automation/tests") == 1


def test_public_readme_lists_mobile_device_automation() -> None:
    body = README.read_text(encoding="utf-8")
    assert "| `mobile-device-automation` |" in body
    assert "连接已授权的 Android 真机" in body


def test_example_uses_only_public_placeholders() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    example = json.loads(text)

    assert example["device"]["serial"] == "emulator-5554"
    assert "C:/path/to/" in text
    assert "Administrator" not in text
    assert "\\Users\\" not in text and "/Users/" not in text
    assert not re.search(r"\b(?:10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b", text)


def test_runtime_outputs_are_named_but_repository_ignored() -> None:
    body = SKILL.read_text(encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "workspace/workflows/mobile-device-automation/" in body
    assert "workflows/mobile-device-automation/outputs/" in body
    assert "workspace/" in ignore
    assert "workflows/*/outputs/" in ignore


def test_real_device_acceptance_is_safe_and_complete() -> None:
    body = RUNBOOK.read_text(encoding="utf-8")

    for required in (
        "非生产 Android",
        "USB ADB",
        "无线 ADB",
        "模拟器",
        "doctor",
        "devices",
        "Settings",
        "selector",
        "Home",
        "capture",
        "Airtest",
        "compile",
        "preview",
        "fresh session",
        "replay",
        "关闭会话",
        "卸载应用",
        "清除应用数据",
        "恢复出厂设置",
        "账户变更",
        "支付",
        "发送消息",
        "个人截图",
        "两个不同查询参数",
        "相同组件 ID 和版本",
        "component-verified",
        "already-complete",
    ):
        assert required in body


def test_public_component_examples_match_schemas_and_production_loaders(
    tmp_path: Path,
) -> None:
    component_value = yaml.safe_load(
        (REFERENCES / "app-component.example.yaml").read_text("utf-8")
    )
    scenario_value = yaml.safe_load(
        (REFERENCES / "app-scenario.example.yaml").read_text("utf-8")
    )
    request_value = json.loads(
        (REFERENCES / "app-composition-request.example.json").read_text("utf-8")
    )
    for value, schema_name in (
        (component_value, "app-component.schema.json"),
        (scenario_value, "app-scenario.schema.json"),
        (request_value, "app-composition-request.schema.json"),
    ):
        schema = json.loads((REFERENCES / schema_name).read_text("utf-8"))
        Draft202012Validator(schema).validate(value)

    materialized = materialize_example_catalog(REFERENCES, tmp_path)
    request = load_composition_request(materialized.request)
    scenario = load_scenario(request.scenario, catalog_root=request.catalog_root)
    component = load_component(
        materialized.catalog_root
        / "components"
        / "ensure-home"
        / "v001"
        / "component.yaml",
        catalog_root=materialized.catalog_root,
    )
    assert scenario.app_id == "example-app"
    assert component.id == "ensure-home"
    bundle = compose_bundle(request, (tmp_path / "outputs").resolve())
    replay_schema = json.loads(
        (REFERENCES / "replay-manifest.schema.json").read_text("utf-8")
    )
    replay_manifest = yaml.safe_load((bundle / "workflow.yaml").read_text("utf-8"))
    Draft202012Validator(replay_schema).validate(replay_manifest)


def test_public_component_examples_are_neutral_and_private_path_free() -> None:
    filenames = (
        "app-component.example.yaml",
        "app-component-action.example.ad",
        "app-scenario.example.yaml",
        "app-composition-request.example.json",
    )
    text = "\n".join((REFERENCES / name).read_text("utf-8") for name in filenames)
    assert "example-app" in text
    assert "com.example.app" in text
    assert "workspace/" not in text
    assert "Administrator" not in text
    assert "美团" not in text and "奶茶" not in text and "炸鸡" not in text
    assert not re.search(r"\b(?:10|127|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b", text)
