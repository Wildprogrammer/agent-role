from pathlib import Path

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_NAME = "requirements-analysis"


def test_workflow_is_canonical_and_catalogued() -> None:
    path = REPOSITORY_ROOT / "workflows" / WORKFLOW_NAME / "SKILL.md"
    frontmatter, body = parse_markdown(path)
    contract = validate_skill(path, frontmatter, body)

    assert contract.name == WORKFLOW_NAME
    assert frontmatter["description"].startswith("Use when")
    assert frontmatter["metadata"]["workflow-version"] == "0.2.0"
    assert "## 人工确认门" in body


def test_workflow_produces_reviewable_automation_design_before_code() -> None:
    workflow_root = REPOSITORY_ROOT / "workflows" / WORKFLOW_NAME
    skill = (workflow_root / "SKILL.md").read_text(encoding="utf-8")
    template = (workflow_root / "references" / "use-case-template.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "自动化测试设计",
        "关联功能用例",
        "前置条件",
        "测试数据",
        "执行步骤",
        "断言",
        "自动化层次",
        "不纳入自动化的理由",
    ):
        assert required in skill
        assert required in template
    assert "此阶段不生成 pytest 文件" in skill


def test_non_obvious_clarification_questions_explain_their_rationale() -> None:
    workflow_root = REPOSITORY_ROOT / "workflows" / WORKFLOW_NAME
    skill = (workflow_root / "SKILL.md").read_text(encoding="utf-8")
    role = (workflow_root / "roles" / "requirements-analyst.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "提问意图不明显",
        "先用一句话",
        "提问依据",
        "答案将影响",
        "明显问题不额外解释",
        "不得用理由诱导用户",
    ):
        assert required in skill
    for required in (
        "提问意图不明显",
        "一句话说明",
        "不得诱导用户",
    ):
        assert required in role
