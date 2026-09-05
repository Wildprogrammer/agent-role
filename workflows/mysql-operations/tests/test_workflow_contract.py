from __future__ import annotations

import json
from pathlib import Path
import tomllib

import yaml

from agent_workflow_hub.contracts import validate_skill
from agent_workflow_hub.frontmatter import parse_markdown


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPOSITORY_ROOT / "workflows" / "mysql-operations" / "SKILL.md"
README_PATH = WORKFLOW_PATH.parent / "README.md"
ROLE_PATH = WORKFLOW_PATH.parent / "roles" / "mysql-operator.md"
INI_PATH = WORKFLOW_PATH.parent / "references" / "mysql.ini.example"
POLICY_PATH = WORKFLOW_PATH.parent / "references" / "mysql-policy.yaml.example"
ENV_INI_PATH = WORKFLOW_PATH.parent / "references" / "mysql-environment.ini.example"

EXPECTED_TOOLS = frozenset(
    {
        "mysql_get_capabilities",
        "mysql_list_schemas",
        "mysql_list_tables",
        "mysql_describe_table",
        "mysql_read_query",
        "mysql_explain_query",
        "mysql_insert",
        "mysql_update",
        "mysql_delete",
        "mysql_execute_transaction",
        "mysql_plan_migration",
        "mysql_apply_migration",
        "mysql_schema_snapshot",
        "mysql_execute_sql",
    }
)


def test_mysql_workflow_is_a_standalone_validated_contract() -> None:
    frontmatter, body = parse_markdown(WORKFLOW_PATH)
    workflow = validate_skill(WORKFLOW_PATH, frontmatter, body)

    assert workflow.name == "mysql-operations"
    assert json.loads(workflow.metadata["required-capabilities"]) == []
    assert json.loads(workflow.metadata["roles"]) == ["roles/mysql-operator.md"]
    assert "独立" in body
    assert "metadata/read" in body
    assert "automated-test-lifecycle" in body
    assert "不接入" in body

    root_skill = (REPOSITORY_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "mysql-operations" in root_skill
    project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "workflows/mysql-operations/tests" in project["tool"]["pytest"]["ini_options"]["testpaths"]


def test_mysql_workflow_documents_the_fixed_tool_surface_and_safety_gates() -> None:
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (WORKFLOW_PATH, README_PATH)
    )

    documented_tools = {tool for tool in EXPECTED_TOOLS if tool in text}
    assert documented_tools == EXPECTED_TOOLS
    for required in (
        "mysql_execute_sql",
        "DELIMITER",
        "SOURCE",
        "客户端命令",
        "默认关闭",
        "仅显式配置",
        "账号权限",
        "由 MySQL 账号权限决定",
        "policy_not_applicable",
        "outcome_unknown",
        "不自动重试",
        "connection_string",
        "不猜测",
        "truncated",
        "脱敏",
        "read_only_environments",
        "require_confirmation",
        "max_result_rows",
        "confirmation_id",
    ):
        assert required in text
    for removed in (
        "固定 13",
        "没有 mysql_execute_sql",
        "不存在 raw SQL 工具",
        "LOAD DATA",
        "INTO OUTFILE",
        "文件导入/导出",
        "脚本执行",
    ):
        assert removed not in text


def test_mysql_environment_example_keeps_credentials_external() -> None:
    text = ENV_INI_PATH.read_text(encoding="utf-8")
    assert "[environment]" in text and "[target.mysql]" in text
    assert "host = " in text and "port = " in text
    assert "username = " not in text and "password = " not in text


def test_mysql_examples_keep_configuration_external_and_policy_optional() -> None:
    ini_text = INI_PATH.read_text(encoding="utf-8")
    for required in (
        "[mysql]",
        "policy_file = mysql-policy.yaml",
        "migrations_dir = migrations",
        "migration_ledger_table = agent_workflow_hub_migrations",
        "read_only_environments = production,staging",
        "username_env = MYSQL_USERNAME",
        "password_env = MYSQL_PASSWORD",
        "仓库外",
        "direct/env",
    ):
        assert required in ini_text
    assert "policy_file" in ini_text
    assert "username = " not in ini_text
    assert "password = " not in ini_text

    policy_text = POLICY_PATH.read_text(encoding="utf-8")
    assert "可选" in policy_text
    policy = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    assert policy["version"] == 1
    assert policy["rules"]
    assert all(
        set(rule["actions"]) <= {"metadata", "read", "explain"}
        for rule in policy["rules"]
    )
    assert all(rule["max_return_rows"] > 0 for rule in policy["rules"])


def test_mysql_operator_role_is_local_and_preserves_data_safety_boundaries() -> None:
    role_text = ROLE_PATH.read_text(encoding="utf-8")
    for required in (
        "source: local",
        "数据安全",
        "事务",
        "测试数据生命周期",
        "不授予执行权限",
        "不替代策略",
        "不替代用户确认",
    ):
        assert required in role_text
