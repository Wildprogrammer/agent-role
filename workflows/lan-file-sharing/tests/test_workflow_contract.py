from __future__ import annotations

import json
from pathlib import Path

from agent_workflow_hub.catalog import load_repository_catalog
from agent_workflow_hub.contracts import workflow_entrypoints
from agent_workflow_hub.frontmatter import parse_markdown
from agent_workflow_hub.repository import REQUIRED_HEADINGS, validate_skill


SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
ROOT = SKILL.parents[2]


def test_skill_contract_is_complete_and_thin() -> None:
    frontmatter, body = parse_markdown(SKILL)
    contract = validate_skill(SKILL, frontmatter, body)

    assert contract.name == "lan-file-sharing"
    assert contract.metadata["workflow-version"] == "0.3.0"
    assert json.loads(contract.metadata["required-capabilities"]) == []
    assert json.loads(contract.metadata["capability-slots"]) == {
        "server": ["cli.dufs"],
        "tunnel": ["cli.ngrok"],
    }
    assert set(workflow_entrypoints(contract.metadata)) == {
        "doctor",
        "install",
        "run",
        "serve-basic",
        "tunnel",
    }
    for heading in REQUIRED_HEADINGS:
        assert f"## {heading}" in body


def test_skill_preserves_native_dufs_instead_of_reimplementing_it() -> None:
    body = SKILL.read_text(encoding="utf-8")

    for text in (
        "原生 argv",
        "--config",
        "--allow-upload",
        "--allow-delete",
        "--allow-all",
        "--allow-symlink",
        "认证",
        "TLS",
        "CORS",
        "shell=False",
        "前台进程",
        "不自动修改防火墙",
    ):
        assert text in body
    for obsolete in (
        "plan_sha256",
        "firewall_plan_sha256",
        "60 分钟租约",
        "starting/running/stopping",
    ):
        assert obsolete not in body


def test_skill_keeps_install_and_run_authorization_separate() -> None:
    body = SKILL.read_text(encoding="utf-8")

    assert "安装确认只授权" in body
    assert "启动确认只授权" in body
    assert "防火墙、系统服务和网络配置不属于本工作流的隐含授权" in body


def test_skill_bounds_basic_fallback_and_public_tunnel() -> None:
    body = SKILL.read_text(encoding="utf-8")

    for text in (
        "http.server",
        "临时只读下载",
        "符号链接",
        "不建议将它用于生产",
        "ngrok http <port>",
        "不读取或记录 authtoken",
        "公网授权独立于本地或 LAN 分享授权",
        "先停止 ngrok",
    ):
        assert text in body


def test_ngrok_is_optional_and_user_managed() -> None:
    capability = load_repository_catalog(ROOT).capabilities["cli.ngrok"]

    assert capability.locked_version == "3.39.9"
    assert capability.frontmatter["installation"] == {
        "policy": "user-managed",
        "scope": "system",
        "methods": ("existing", "manual", "official-artifact", "package-manager"),
    }
    assert capability.frontmatter["detect"]["command"] == "ngrok --version"
