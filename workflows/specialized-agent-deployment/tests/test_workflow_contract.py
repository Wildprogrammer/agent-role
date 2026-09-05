from __future__ import annotations

import json
from pathlib import Path

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_ROOT = REPOSITORY_ROOT / "workflows" / "specialized-agent-deployment"
SKILL_PATH = WORKFLOW_ROOT / "SKILL.md"
EXPECTED_ENTRYPOINTS = {
    "preview": (
        "python <HUB_ROOT>/workflows/specialized-agent-deployment/scripts/"
        "specialized_agent_deployment.py preview --hub-root <HUB_ROOT> "
        "--request <ABSOLUTE_JSON>"
    ),
    "apply": (
        "python <HUB_ROOT>/workflows/specialized-agent-deployment/scripts/"
        "specialized_agent_deployment.py apply --hub-root <HUB_ROOT> "
        "--manifest <ABSOLUTE_JSON> --confirmed-plan-sha256 <SHA256>"
    ),
    "verify": (
        "python <HUB_ROOT>/workflows/specialized-agent-deployment/scripts/"
        "specialized_agent_deployment.py verify --hub-root <HUB_ROOT> "
        "--manifest <ABSOLUTE_JSON>"
    ),
}


def test_workflow_declares_exact_contract() -> None:
    frontmatter, body = parse_markdown(SKILL_PATH)
    contract = validate_skill(SKILL_PATH, frontmatter, body)

    assert contract.name == "specialized-agent-deployment"
    assert contract.metadata["workflow-version"] == "0.5.0"
    assert json.loads(contract.metadata["required-capabilities"]) == []
    assert "roles" not in contract.metadata
    assert json.loads(contract.metadata["entrypoints"]) == EXPECTED_ENTRYPOINTS
    assert json.loads(contract.metadata["supported-hosts"]) == [
        "codex",
        "openclaw",
        "claude-code",
        "hermes",
        "opencode",
    ]
    assert "Hermes Profile" in body
    assert "DeepSeek Harness Web 用户 Preset" in body


def test_skill_has_one_deployment_gate_and_no_extra_review() -> None:
    _, body = parse_markdown(SKILL_PATH)
    gate_section = body.split("## 人工确认", 1)[1].split("## ", 1)[0]
    assert gate_section.count("deployment_review") == 1
    assert "requirements_review" not in body
    assert "integration_review" not in body
    assert "普通澄清不是确认门" in body


def test_skill_keeps_composition_explicit_without_dependency_manifest() -> None:
    _, body = parse_markdown(SKILL_PATH)
    for required in (
        "完整读取主工作流",
        "语义识别",
        "显式传入",
        "不得新增依赖 YAML",
        "manifest 不是工作流依赖权威",
        "不得复制主流程步骤",
    ):
        assert required in body
    assert not list(WORKFLOW_ROOT.glob("*dependenc*.y*ml"))


def test_skill_preserves_easy_use_and_honest_verification() -> None:
    _, body = parse_markdown(SKILL_PATH)
    for required in (
        "澄清",
        "快照",
        "预览",
        "apply",
        "分层验证",
        "partially_verified",
        "不安装或修改宿主",
        "不自动删除",
        "不自动更新",
        "不提供 headless",
        "不创建 rule.md",
    ):
        assert required in body


def test_skill_defaults_to_system_python_without_global_package_mutation() -> None:
    _, body = parse_markdown(SKILL_PATH)
    for phrase in (
        "默认使用系统 Python",
        "system-source",
        "用户明确要求隔离",
        "不覆盖系统 Python",
        "不依赖开发目录",
        "不增加确认门",
    ):
        assert phrase in body
    reference = (WORKFLOW_ROOT / "references/standalone-runtime.md").read_text(encoding="utf-8")
    for phrase in ("system-source", "isolated", "packages/", "pip --target", "不安装、升级或卸载系统全局包"):
        assert phrase in reference


def test_request_schema_is_closed_and_limits_supported_hosts() -> None:
    schema_path = WORKFLOW_ROOT / "references" / "deployment-request.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["properties"]["host"]["enum"] == [
        "hermes",
        "deepseek-harness",
    ]
    assert set(schema["required"]) == set(schema["properties"]) - {"runtime"}
    assert len(schema["properties"]["runtime"]["oneOf"]) == 2
    enablement = schema["$defs"]["hermesEnablement"]
    assert enablement["additionalProperties"] is False
    assert set(enablement["required"]) == set(enablement["properties"])
    assert enablement["properties"]["mode"]["const"] == "full"
    assert enablement["properties"]["source_profile"]["const"] == "active"
    assert enablement["properties"]["platforms"]["minItems"] == 1
    mcp = schema["$defs"]["dshMcpServer"]
    assert mcp["additionalProperties"] is False
    assert set(mcp["required"]) == {"workflow", "server_name", "command", "args", "cwd"}


def test_skill_treats_full_enablement_as_scope_not_an_extra_gate() -> None:
    _, body = parse_markdown(SKILL_PATH)
    for required in (
        "是否需要完整启用",
        "选择哪些消息平台",
        "能力范围澄清",
        "不是新增确认门",
        "单一 Gateway",
        "multiplex_profiles",
        "profile_routes",
        "外部资源只读",
        "readiness-only",
    ):
        assert required in body
    assert body.count("deployment_review") == 1


def test_host_references_define_bounded_behavior_verification() -> None:
    material = {
        path.name: path.read_text(encoding="utf-8")
        for path in (
            WORKFLOW_ROOT / "references" / "host-behavior-verification.md",
            WORKFLOW_ROOT / "references" / "hermes.md",
            WORKFLOW_ROOT / "references" / "deepseek-harness.md",
        )
    }
    assert "不执行业务任务" in material["host-behavior-verification.md"]
    assert "0.20.6" in material["hermes.md"]
    assert "gateway.multiplex_profiles" in material["hermes.md"]
    assert "gateway.profile_routes" in material["hermes.md"]
    assert "enabled: false" in material["hermes.md"]
    assert "不发送真实消息" in material["host-behavior-verification.md"]
    assert "0.1.2-alpha.2" in material["deepseek-harness.md"]
    assert "compatible_not_runnable" in material["deepseek-harness.md"]
    assert "partially_verified" in material["deepseek-harness.md"]
