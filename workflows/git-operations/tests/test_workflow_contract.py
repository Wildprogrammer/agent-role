import json
from pathlib import Path

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_NAME = "git-operations"
WORKFLOW_SKILL = REPOSITORY_ROOT / "workflows" / WORKFLOW_NAME / "SKILL.md"
SCRIPT = WORKFLOW_SKILL.parent / "scripts" / "git_operations.py"

REQUIRED_HEADINGS = (
    "用途与触发条件",
    "非目标",
    "输入",
    "输出与命名规则",
    "依赖和运行前检查",
    "系统修改与权限影响",
    "执行步骤",
    "人工确认门",
    "失败恢复",
    "重跑、幂等与覆盖策略",
    "验收标准",
    "清理方式",
)


def test_git_operations_workflow_is_canonical_and_catalogued():
    assert WORKFLOW_SKILL.is_file()
    assert SCRIPT.is_file()

    frontmatter, body = parse_markdown(WORKFLOW_SKILL)
    contract = validate_skill(WORKFLOW_SKILL, frontmatter, body)

    assert contract.name == WORKFLOW_NAME
    assert frontmatter["description"].startswith("Use when")
    assert json.loads(frontmatter["metadata"]["required-capabilities"]) == []
    for heading in REQUIRED_HEADINGS:
        assert f"## {heading}" in body

    for subcommand in (
        "status",
        "diff",
        "log",
        "clone",
        "add",
        "commit",
        "branch-create",
        "checkout",
        "merge",
        "push",
        "push-exact",
        "ls-remote-ref",
        "head-sha",
        "list-tree",
        "show-file",
    ):
        assert subcommand in body


def test_git_operations_is_independent_from_lifecycle_gates():
    frontmatter, body = parse_markdown(WORKFLOW_SKILL)

    for gate in ("Gate 1", "Gate 2", "Gate 3"):
        assert gate not in body
    assert "automated-test-lifecycle" not in body
    assert "force" in body

    metadata = frontmatter["metadata"]
    assert json.loads(metadata["config-templates"]) == {}
    assert json.loads(metadata["config-requirements"]) == {}
    assert json.loads(metadata["entrypoints"]) == {}
