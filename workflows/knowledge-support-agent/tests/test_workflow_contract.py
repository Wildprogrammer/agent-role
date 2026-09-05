import json
from pathlib import Path

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.doctor import CAPABILITY_DETECTOR_CONTRACTS, DETECTORS
from agent_workflow_hub.frontmatter import parse_markdown


ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "workflows" / "knowledge-support-agent" / "SKILL.md"


def test_knowledge_support_agent_contract_is_minimal_and_portable() -> None:
    frontmatter, body = parse_markdown(SKILL)
    contract = validate_skill(SKILL, frontmatter, body)

    assert contract.name == "knowledge-support-agent"
    assert frontmatter["metadata"]["workflow-version"] == "0.1.0"
    assert json.loads(frontmatter["metadata"]["required-capabilities"]) == [
        "python.lancedb"
    ]
    assert json.loads(frontmatter["metadata"]["supported-hosts"]) == [
        "codex",
        "openclaw",
        "claude-code",
        "hermes",
        "opencode",
    ]
    assert set(json.loads(frontmatter["metadata"]["entrypoints"])) == {
        "health",
        "build",
        "query",
        "refresh",
        "feedback",
    }
    assert "Use when" in frontmatter["description"]
    assert "步骤" not in frontmatter["description"]


def test_skill_teaches_only_confirmed_behavior_and_ownership_boundaries() -> None:
    body = SKILL.read_text(encoding="utf-8")
    for heading in (
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
    ):
        assert f"## {heading}" in body
    normalized = " ".join(body.split())
    for required in (
        "一个 Agent 一个独立 LanceDB",
        "只读取当前 HEAD 已提交",
        "全文与向量混合检索",
        "自动降级全文检索",
        "复述用户答案并确认正确",
        "直接写入该 Agent 的 LanceDB",
        "git-operations",
        "information-collection",
        "用户指定且当前可用的宿主部署能力",
        "仅调用配置中显式选择的补充工作流",
    ):
        assert required in normalized
    assert "confirm_writes" not in body
    assert "Gate 1" not in body
    assert "HTTP 服务" in body
    assert "MCP 服务" in body


def test_workflow_adds_no_duplicate_dependency_or_rule_manifest() -> None:
    workflow = SKILL.parent
    assert not (workflow / "dependencies.yaml").exists()
    assert not (workflow / "rule.md").exists()
    assert not (workflow / "AGENTS.md").exists()


def test_lancedb_capability_has_a_read_only_hub_detector() -> None:
    detector = CAPABILITY_DETECTOR_CONTRACTS["python.lancedb"]

    assert detector.detector_type == "python-import"
    assert detector.import_name == "lancedb"
    assert "python.lancedb" in DETECTORS
