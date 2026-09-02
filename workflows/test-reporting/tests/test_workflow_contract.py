from pathlib import Path

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_NAME = "test-reporting"


def test_workflow_is_canonical_and_catalogued() -> None:
    path = REPOSITORY_ROOT / "workflows" / WORKFLOW_NAME / "SKILL.md"
    frontmatter, body = parse_markdown(path)
    contract = validate_skill(path, frontmatter, body)

    assert contract.name == WORKFLOW_NAME
    assert frontmatter["description"].startswith("Use when")
    assert "## 人工确认门" in body
