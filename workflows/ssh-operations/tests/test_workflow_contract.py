from __future__ import annotations

import json
from pathlib import Path

from agent_workflow_hub.contracts import workflow_entrypoints
from agent_workflow_hub.frontmatter import parse_markdown
from agent_workflow_hub.repository import REQUIRED_HEADINGS, validate_skill

SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"


def test_skill_owns_general_ssh_without_extra_gates() -> None:
    body = SKILL.read_text(encoding="utf-8")
    assert "TOFU" in body
    assert "SFTP" in body and "SCP" in body
    assert "confirm_writes" not in body
    assert "Gate 1" not in body
    assert "automated-test-lifecycle" not in body
    assert "environment-validation 的只读限制" in body


def test_skill_contract_is_complete() -> None:
    frontmatter, body = parse_markdown(SKILL)
    contract = validate_skill(SKILL, frontmatter, body)
    assert contract.name == "ssh-operations"
    assert contract.metadata["workflow-version"] == "0.1.0"
    assert json.loads(contract.metadata["required-capabilities"]) == ["python.asyncssh"]
    assert json.loads(contract.metadata["supported-hosts"]) == [
        "codex", "openclaw", "claude-code", "hermes", "opencode"
    ]
    assert set(workflow_entrypoints(contract.metadata)) == {
        "doctor", "exec", "run-steps", "sftp", "upload", "download", "forward"
    }
    for heading in REQUIRED_HEADINGS:
        assert f"## {heading}" in body


def test_skill_records_platform_and_transport_boundaries() -> None:
    body = SKILL.read_text(encoding="utf-8")
    assert "needs-elevation" in body
    assert "Agent Forwarding" in body and "默认关闭" in body
    assert "SCP 不支持断点续传" in body
    assert "一次确认" in body
    assert "可独立使用，也可与其他工作流共享同一个 INI" in body
