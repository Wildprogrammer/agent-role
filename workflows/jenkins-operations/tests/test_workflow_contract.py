from __future__ import annotations

import json
from pathlib import Path
import tomllib

from agent_workflow_hub.contracts import validate_capability, validate_skill
from agent_workflow_hub.frontmatter import parse_markdown
from agent_workflow_hub.jenkins_mcp.plugins import PluginDriverCatalog


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_PATH = REPOSITORY_ROOT / "capabilities" / "app" / "jenkins" / "CAPABILITY.md"
WORKFLOW_PATH = REPOSITORY_ROOT / "workflows" / "jenkins-operations" / "SKILL.md"


def test_jenkins_workflow_documents_shared_ini_templates() -> None:
    references = WORKFLOW_PATH.parent / "references"
    ini_template = references / "jenkins.ini.example"
    policy_template = references / "jenkins-policy.yaml.example"

    assert ini_template.is_file()
    assert policy_template.is_file()
    ini_text = ini_template.read_text(encoding="utf-8")
    policy_text = policy_template.read_text(encoding="utf-8")
    for required_text in (
        "[jenkins]",
        "policy_file",
        "allow_insecure_http",
        "require_crumb",
        "username_env",
        "token_env",
        "token",
        "password",
        "[target.jenkins]",
        "host =",
        "port =",
        "confirm_writes",
    ):
        assert required_text in ini_text
    for required_text in (
        "scope.controller",
        "scope.root",
        "scope.nodes",
        "read_scopes",
        "path_prefixes",
        "expires_at",
        "max_concurrent",
    ):
        assert required_text in policy_text


def test_jenkins_operations_declares_user_managed_cru_contract() -> None:
    capability_frontmatter, capability_body = parse_markdown(CAPABILITY_PATH)
    capability = validate_capability(
        CAPABILITY_PATH,
        capability_frontmatter,
        capability_body,
    )

    assert capability.id == "app.jenkins"
    assert capability.frontmatter["installation"]["policy"] == "user-managed"
    assert capability.frontmatter["installation"]["scope"] == "system"
    assert "pip" not in capability.frontmatter["installation"]["methods"]
    assert ".exe" not in capability.frontmatter["detect"]["command"]
    capability_normalized = " ".join(capability_body.split())
    for required_text in (
        "current user language",
        "never downloads, installs, upgrades, starts, stops, reconfigures",
        "registers Jenkins",
        "Agent only provides guidance",
        "must not remove MCP host mappings or external policy files",
    ):
        assert required_text in capability_normalized

    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "mcp==1.27.2" in project["project"]["dependencies"]
    assert "workflows/jenkins-operations/tests" in project["tool"]["pytest"]["ini_options"]["testpaths"]

    workflow_frontmatter, workflow_body = parse_markdown(WORKFLOW_PATH)
    workflow = validate_skill(WORKFLOW_PATH, workflow_frontmatter, workflow_body)

    assert workflow.name == "jenkins-operations"
    assert workflow.metadata["workflow-version"] == "0.3.0"
    assert json.loads(workflow.metadata["required-capabilities"]) == ["app.jenkins"]
    assert json.loads(workflow.metadata["roles"]) == ["roles/jenkins-operator.md"]
    role_path = WORKFLOW_PATH.parent / "roles" / "jenkins-operator.md"
    role_text = role_path.read_text(encoding="utf-8")
    for required_text in (
        "https://github.com/msitarzewski/agency-agents",
        "fc5a192e7e0f2fad0d74686d9165435e410869a8",
        "license: MIT",
        "engineering/engineering-devops-automator.md",
        "local modifications",
    ):
        assert required_text in role_text

    for required_text in (
        "查、增、改",
        "不提供任意 REST 请求",
        "groovy-inline-v1",
        "jenkinsfile-scm-v1",
        "不自动安装 Jenkins",
        "不自动下载 Jenkins",
        "不自动注册或改写宿主 MCP 映射",
        "当前用户语言",
        "outcome_unknown",
        "Jenkins RBAC",
        "Token、密码、Cookie、crumb",
        "任意 XML",
        "插件、凭据、节点、全局安全、JCasC reload、Controller 重启",
        "凭据、节点、全局安全",
        "删除或批量移动",
        "trigger_build",
        "cancel_build",
        "不能被普通 CRU 预授权",
    ):
        assert required_text in workflow_body


def test_jenkins_workflow_documents_optional_policy_and_direct_write_default() -> None:
    skill = WORKFLOW_PATH.read_text(encoding="utf-8")
    readme = (WORKFLOW_PATH.parent / "README.md").read_text(encoding="utf-8")
    ini_template = (
        WORKFLOW_PATH.parent / "references" / "jenkins.ini.example"
    ).read_text(encoding="utf-8")
    combined = f"{skill}\n{readme}\n{ini_template}"

    for required_text in (
        "[environment]",
        "[target.jenkins]",
        "policy_file 是可选的",
        "confirm_writes 默认 true",
        "confirm_writes = true",
        "直接执行",
        "Jenkins 账号/RBAC 是默认授权边界",
        "host 与 port 组成 URL",
        "默认 http",
        "缺省为 nonproduction",
        "显式写 production",
        "可匿名",
        "不打印凭据",
        "生产 Controller 必须使用 HTTPS",
        "其他 sections",
    ):
        assert required_text in combined


def test_jenkins_write_confirmation_contract_is_current_session_and_one_shot() -> None:
    skill = WORKFLOW_PATH.read_text(encoding="utf-8")
    readme = (WORKFLOW_PATH.parent / "README.md").read_text(encoding="utf-8")
    capability = CAPABILITY_PATH.read_text(encoding="utf-8")
    combined = f"{skill}\n{readme}\n{capability}"

    for required_text in (
        "confirmation_id",
        "needs_user_confirmation",
        "当前会话",
        "一次性",
        "SessionConfirmationStore",
        "single-use",
        "released individually",
        "without a valid current-session confirmation",
        "does not depend on the MCP host",
        "请求指纹",
        "策略复核",
        "生产 Controller 必须使用 HTTPS",
        "outcome_unknown",
        "不自动重试",
    ):
        assert required_text in combined
    for removed_text in (
        "approval_id",
        "HMAC",
        "JenkinsMcpHostBridge",
        "宿主原生适配器",
        "内部一次性确认桥接",
        "verified native host-confirmation adapter",
        "verified host adapter",
        "host-confirmation adapter",
        "host adapter",
    ):
        assert removed_text not in combined


def test_jenkins_workflow_documents_the_three_pipeline_sources_and_parameters() -> None:
    skill = WORKFLOW_PATH.read_text(encoding="utf-8")
    readme = (WORKFLOW_PATH.parent / "README.md").read_text(encoding="utf-8")
    policy_template = (
        WORKFLOW_PATH.parent / "references" / "jenkins-policy.yaml.example"
    ).read_text(encoding="utf-8")
    combined = f"{skill}\n{readme}\n{policy_template}"

    for required_text in (
        "groovy-inline-v1",
        "jenkinsfile-scm-v1",
        "pipeline-v1",
        "pytest-inline-v1",
        "script",
        "repository_url",
        "branch",
        "script_path",
        "credentials_id",
        "sandbox",
        "CpsFlowDefinition",
        "CpsScmFlowDefinition",
        "GitSCM",
    ):
        assert required_text in combined
    for removed_text in (
        "不提供任意 Groovy",
        "不支持 Jenkinsfile",
        "任意 Groovy、Jenkins CLI",
    ):
        assert removed_text not in combined


def test_jenkins_workflow_owns_structured_pipeline_creation_and_update() -> None:
    skill = WORKFLOW_PATH.read_text(encoding="utf-8")
    readme = (WORKFLOW_PATH.parent / "README.md").read_text(encoding="utf-8")
    policy_template = (
        WORKFLOW_PATH.parent / "references" / "jenkins-policy.yaml.example"
    ).read_text(encoding="utf-8")
    combined = f"{skill}\n{readme}\n{policy_template}"

    for required_text in (
        "创建或更新",
        "pipeline_definition",
        "template_parameters",
        "pipeline-v1",
        "pytest-inline-v1",
        "groovy-inline-v1",
        "jenkinsfile-scm-v1",
        "只替换",
        "未修改配置",
    ):
        assert required_text in combined

    assert "新增 Pipeline 专属确认" in combined


def test_jenkins_plugin_fixtures_are_owned_by_the_workflow() -> None:
    expected = {
        "pipeline_rest": "workflows/jenkins-operations/tests/fixtures/pipeline-rest-api-runs.json",
        "junit": "workflows/jenkins-operations/tests/fixtures/junit-summary.json",
        "multibranch": "workflows/jenkins-operations/tests/fixtures/multibranch-children.json",
    }

    actual = {
        name: driver.fixture_name
        for name, driver in PluginDriverCatalog._DRIVERS.items()
    }

    assert actual == expected
    for relative_path in actual.values():
        assert (REPOSITORY_ROOT / relative_path).is_file()
