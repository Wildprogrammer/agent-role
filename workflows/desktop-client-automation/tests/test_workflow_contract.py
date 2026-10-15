from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from PIL import Image

from agent_workflow_hub.catalog import load_repository_catalog
from agent_workflow_hub.desktop_client_automation.composition import compose_bundle
from agent_workflow_hub.desktop_client_automation.composition_contracts import (
    load_component,
    load_composition_request,
    load_scenario,
)
from agent_workflow_hub.contracts import workflow_entrypoints
from agent_workflow_hub.frontmatter import parse_markdown
from agent_workflow_hub.repository import REQUIRED_HEADINGS, validate_skill


SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
README = REPOSITORY_ROOT / "README.md"
REFERENCES = SKILL.parent / "references"
EXAMPLES = (
    REFERENCES / "app-component.example.yaml",
    REFERENCES / "app-scenario.example.yaml",
    REFERENCES / "app-composition-request.example.json",
)


def test_request_schema_exposes_primary_desktop_target() -> None:
    schema = json.loads(
        (SKILL.parent / "references" / "automation-request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    app = schema["$defs"]["app"]
    assert "desktop" in app["properties"]["target_kind"]["enum"]
    assert app["properties"]["display_id"]["type"] == ["string", "null"]
    assert app["properties"]["window_title_regex"]["type"] == ["string", "null"]
    assert any("then" in branch for branch in app["allOf"])


def test_public_readme_lists_desktop_client_automation() -> None:
    body = README.read_text(encoding="utf-8")
    assert "| `desktop-client-automation` |" in body


def test_public_readme_includes_desktop_automation_example() -> None:
    body = README.read_text(encoding="utf-8")
    assert "点击当前主显示器桌面上的指定图标" in body
    assert "把确认后的 Windows 操作路径固化为 Airtest 回放" in body


def test_cua_driver_capability_is_pinned_and_cross_platform() -> None:
    capability = load_repository_catalog(REPOSITORY_ROOT).capabilities[
        "mcp.cua-driver"
    ]
    assert capability.locked_version == "0.23.2"
    assert capability.frontmatter["version_requirement"] == ">=0.23.2"
    assert capability.recommended_version == "0.23.2"
    assert capability.frontmatter["official_source"] == "https://github.com/trycua/cua"
    assert capability.frontmatter["license"] == "MIT"
    assert capability.frontmatter["systems"]["os"] == {
        "windows": "documented",
        "macos": "documented",
        "linux": "documented-upstream-not-workflow-v1",
    }
    assert capability.installation["policy"] == "user-managed"
    assert capability.installation["scope"] == "system"
    assert capability.installation["methods"] == (
        "existing",
        "official-artifact",
        "manual",
    )


def test_skill_uses_cua_for_cross_host_exploration_and_airtest_for_windows_replay() -> None:
    frontmatter, body = parse_markdown(SKILL)
    contract = validate_skill(SKILL, frontmatter, body)
    assert contract.name == "desktop-client-automation"
    assert json.loads(contract.metadata["supported-hosts"]) == [
        "codex",
        "openclaw",
        "claude-code",
        "hermes",
        "opencode",
    ]
    assert json.loads(contract.metadata["required-capabilities"]) == [
        "mcp.cua-driver"
    ]
    assert "Cua Driver MCP" in body
    assert "Airtest" in body
    assert "snapshot_id" in body and "element_token" in body
    assert "后台" in body and "前台" in body
    assert "needs-replay-backend" in body
    assert "macOS" in body and "不支持 Airtest 桌面回放" in body
    assert "图片失败不得静默回退坐标" in body
    assert "不依赖或复制 `automated-test-lifecycle`" in body
    assert set(workflow_entrypoints(contract.metadata)) == {
        "doctor", "capture", "locate-image", "click-image", "compile",
        "compose", "preview", "replay"
    }
    for heading in REQUIRED_HEADINGS:
        assert f"## {heading}" in body


def test_airtest_keeps_windows_desktop_support_while_adding_mobile_profiles() -> None:
    capability = load_repository_catalog(REPOSITORY_ROOT).capabilities["python.airtest"]
    assert set(capability.hosts.values()) == {"conditional"}
    assert capability.frontmatter["systems"]["os"] == {
        "windows": "documented-desktop-and-mobile",
        "macos": "documented-mobile-unverified",
        "linux": "documented-mobile-unverified",
    }
    assert "macOS/Linux 桌面目标" in capability.body
    assert "不支持" in capability.body


def test_documents_native_desktop_target_contract() -> None:
    workflow = SKILL.read_text(encoding="utf-8")
    cua = (
        REPOSITORY_ROOT / "capabilities/mcp/cua-driver/CAPABILITY.md"
    ).read_text(encoding="utf-8")
    airtest = (
        REPOSITORY_ROOT / "capabilities/python/airtest/CAPABILITY.md"
    ).read_text(encoding="utf-8")

    for text in (
        "get_desktop_state",
        "display_id",
        "primary",
        "count=2",
        "Windows:///",
        "desktop-not-foreground",
        "不要求 UIA",
    ):
        assert text in workflow
    assert "get_desktop_state" in cua
    assert "display_id" in cua and "primary" in cua
    assert "Windows:///" in airtest
    assert "macOS/Linux 桌面目标" in airtest
    assert "不支持" in airtest


def test_skill_does_not_duplicate_cua_actions_in_python() -> None:
    body = SKILL.read_text(encoding="utf-8")
    assert "Agent 直接调用 Cua MCP" in body
    assert "Hub 不封装 Cua 点击" in body
    assert "get_window_state" in body
    assert "结构化拒绝" in body


def test_skill_records_cua_0232_webview_acceptance_rules() -> None:
    body = SKILL.read_text(encoding="utf-8")
    assert "`element_token` 存在也显式传入 `pid + window_id`" in body
    assert "`unknown_reason=untrusted_source`" in body
    assert "后置状态已确认业务结果时不得前台重试" in body


def test_skill_keeps_one_business_confirmation_and_effect_rules() -> None:
    body = SKILL.read_text(encoding="utf-8")
    assert "唯一业务确认" in body
    assert "不固化、仅生成、生成并回放" in body
    assert "最高等级" in body
    assert "结果未知时只对账" in body
    assert "图片失败不得静默回退坐标" in body
    assert "generated-unverified" in body
    assert "replay-verified" in body
    assert "replay-failed" in body


def test_skill_documents_current_computer_use_bootstrap_without_hard_coding_it() -> None:
    body = SKILL.read_text(encoding="utf-8")
    assert "[@电脑](plugin://computer-use@openai-bundled)" in body
    assert "不是工作流的永久调用语法" in body
    assert "以当前任务实际暴露的 Windows 原生应用 surface 为准" in body
    assert "不要在载入条件没有变化时反复分叉或创建任务" in body


def test_skill_documents_conditional_image_fallback_without_making_it_permanent() -> None:
    body = SKILL.read_text(encoding="utf-8")
    assert "coordinate input geometry is unavailable" in body
    assert "SetIsBorderRequired" in body
    assert "locate-image" in body
    assert "click-image" in body
    assert "不得静默回退" in body
    assert "未来" in body and "Computer Use" in body


def test_launcher_does_not_mix_python_311_packages_into_system_python() -> None:
    launcher = (SKILL.parent / "scripts" / "desktop_client_automation.py").read_text(
        encoding="utf-8"
    )
    assert "_RUNTIME_PYTHON" in launcher
    assert "subprocess.run" in launcher
    assert "site-packages" not in launcher


def test_desktop_workflow_declares_composition_contracts() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for name in (
        "app-component.schema.json",
        "app-scenario.schema.json",
        "app-composition-request.schema.json",
        " compose ",
        "component_precondition_failed",
    ):
        assert name in text


def test_public_examples_are_portable_and_secret_free() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in EXAMPLES)
    assert "C:\\Users\\" not in combined
    assert "/home/" not in combined
    assert "serial" not in combined.casefold()
    assert "bambu" not in combined.casefold()
    assert "example-desktop-app" in combined


def test_public_component_examples_match_schemas_and_loaders(
    tmp_path: Path,
) -> None:
    component_value = yaml.safe_load(EXAMPLES[0].read_text(encoding="utf-8"))
    scenario_value = yaml.safe_load(EXAMPLES[1].read_text(encoding="utf-8"))
    request_value = json.loads(EXAMPLES[2].read_text(encoding="utf-8"))
    for value, schema_name in (
        (component_value, "app-component.schema.json"),
        (scenario_value, "app-scenario.schema.json"),
        (request_value, "app-composition-request.schema.json"),
    ):
        Draft202012Validator(
            json.loads((REFERENCES / schema_name).read_text(encoding="utf-8"))
        ).validate(value)

    catalog = (tmp_path / "catalog").resolve()
    version_root = catalog / "components" / "search-items" / "v001"
    scenario_root = catalog / "scenarios"
    version_root.mkdir(parents=True)
    scenario_root.mkdir(parents=True)
    shutil.copyfile(EXAMPLES[0], version_root / "component.yaml")
    shutil.copyfile(EXAMPLES[1], scenario_root / "search-items.yaml")
    for name, color in (
        ("search-button.png", "red"),
        ("search-ready.png", "green"),
        ("results-ready.png", "blue"),
    ):
        Image.new("RGB", (12, 12), color).save(version_root / name)
    final_state = (tmp_path / "final-state.png").resolve()
    Image.new("RGB", (12, 12), "white").save(final_state)
    request_value["catalog_root"] = str(catalog)
    request_value["scenario"] = str((scenario_root / "search-items.yaml").resolve())
    request_value["success_assertion"]["template"] = str(final_state)
    request_path = (tmp_path / "request.json").resolve()
    request_path.write_text(json.dumps(request_value), encoding="utf-8")

    request = load_composition_request(request_path)
    scenario = load_scenario(request.scenario, catalog_root=request.catalog_root)
    component = load_component(
        version_root / "component.yaml", catalog_root=request.catalog_root
    )
    assert scenario.app_id == "example-desktop-app"
    assert component.id == "search-items"
    assert compose_bundle(request, (tmp_path / "outputs").resolve()).is_dir()
