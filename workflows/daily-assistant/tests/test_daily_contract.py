from pathlib import Path

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


def test_daily_assistant_skill_declares_local_non_mutating_boundaries():
    path = Path("workflows/daily-assistant/SKILL.md")
    frontmatter, body = parse_markdown(path)
    contract = validate_skill(path, frontmatter, body)

    assert frontmatter["name"] == "daily-assistant"
    assert frontmatter["metadata"]["required-capabilities"] == "[]"
    assert "不持久化用户输入原文" in contract.body
    assert "用户明确请求" in contract.body
    assert "24:00" in contract.body
    assert "不写入 Obsidian" in contract.body
    assert "不直连禅道" in contract.body
    assert "不得创建或启用真实提醒自动化" in contract.body


def test_reminder_reference_is_advisory_only():
    body = Path("workflows/daily-assistant/references/automation.md").read_text(
        encoding="utf-8"
    )

    assert all(
        time in body for time in ("10:00", "11:30", "14:30", "16:10", "17:10")
    )
    assert "24:00" in body
    assert "不生成日报" in body
    assert "用户另行明确确认" in body


def test_skill_limits_reusable_learning_to_confirmed_experience():
    path = Path("workflows/daily-assistant/SKILL.md")
    _frontmatter, body = parse_markdown(path)

    assert "读取已确认经验" in body
    assert "确认可复用" in body
