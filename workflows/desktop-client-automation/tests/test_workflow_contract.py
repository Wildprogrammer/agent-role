from __future__ import annotations

import json
from pathlib import Path

from agent_workflow_hub.catalog import load_repository_catalog
from agent_workflow_hub.contracts import workflow_entrypoints
from agent_workflow_hub.frontmatter import parse_markdown
from agent_workflow_hub.repository import REQUIRED_HEADINGS, validate_skill


SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
README = REPOSITORY_ROOT / "README.md"


def test_public_readme_lists_desktop_client_automation() -> None:
    body = README.read_text(encoding="utf-8")
    assert "| `desktop-client-automation` |" in body


def test_public_readme_includes_desktop_automation_example() -> None:
    body = README.read_text(encoding="utf-8")
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
        "doctor", "capture", "locate-image", "click-image", "compile", "replay"
    }
    for heading in REQUIRED_HEADINGS:
        assert f"## {heading}" in body


def test_airtest_is_host_independent_but_remains_windows_desktop_only() -> None:
    capability = load_repository_catalog(REPOSITORY_ROOT).capabilities["python.airtest"]
    assert set(capability.hosts.values()) == {"conditional"}
    assert capability.frontmatter["systems"]["os"] == {
        "windows": "documented",
        "macos": "unsupported-desktop-v1",
        "linux": "unsupported-desktop-v1",
    }


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
