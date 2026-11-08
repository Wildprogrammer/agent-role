from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from agent_workflow_hub.contracts import workflow_entrypoints
from agent_workflow_hub.frontmatter import parse_markdown
from agent_workflow_hub.repository import REQUIRED_HEADINGS, validate_skill


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
SKILL = WORKFLOW_ROOT / "SKILL.md"


def test_skill_is_complete_and_exposes_only_bounded_entrypoints() -> None:
    frontmatter, body = parse_markdown(SKILL)
    contract = validate_skill(SKILL, frontmatter, body)

    assert contract.name == "agent-session-gateway"
    assert contract.metadata["workflow-version"] == "0.9.3"
    assert contract.metadata["display-name"] == "Agent Session Gateway"
    assert json.loads(contract.metadata["required-capabilities"]) == []
    assert set(workflow_entrypoints(contract.metadata)) == {
        "doctor",
        "install-server-adapter",
        "simulate",
        "simulate-websocket",
        "start-server",
        "serve-stdio",
        "serve-xiaoai-mcp",
        "verify-server-plugin",
    }
    for heading in REQUIRED_HEADINGS:
        assert f"## {heading}" in body


def test_skill_fails_closed_and_keeps_future_work_out_of_scope() -> None:
    body = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "dispatch_enabled",
        "require_confirmation",
        "回环 HTTP",
        "不自动批准",
        "waiting_for_approval",
        "不执行 shell",
        "USB HID",
        "等待后续验证",
        "87c6df77b0220ddbd8fc804f984f83f69f3225e5",
        "Hermes Profile 白名单完全由仓库外桥接配置",
        "静音测试 TTS",
        "session.resume",
        "prompt.submit(surface=voice-live)",
        "XIAOZHI_HERMES_BINDING_STORE",
        "不能使用每次 WebSocket 重连都会变化的 `session_id`",
        "`dispatch_bound_voice` 只有独立直写策略为 `direct` 时免确认",
        "生产默认使用确定性的 `bound_agent` 路由",
        "`prepare_attachment_upload`",
        "52428800",
        "单文件上限 50 MiB",
        "Hermes 图片另受 25 MiB 上限",
        "不声称小爱客户端会把相册图片或文档字节原生注入 MCP 参数",
        "多通道 Agent 会话网关",
        "公开工作流 ID 和目录名为 `agent-session-gateway`",
        "通道适配器",
        "宿主适配器",
        "设备扩展",
        "一次性、Profile-scoped interactive ticket",
        "不启动、重启或修改 Hermes",
        "不得在原生发现失败时改走 DeepSeek Harness",
        "不是工作流资源",
        "`merged-binary.bin` 仅用于物理工厂烧录或救援",
    ):
        assert phrase in body

    upstream_reference = (
        WORKFLOW_ROOT / "references" / "xiaozhi-server-v0.9.7.md"
    ).read_text(encoding="utf-8")
    assert "production `bound_agent` path" in upstream_reference
    assert "physical-device acceptance run" in upstream_reference
    assert "Python 3.10" in upstream_reference


def test_json_schemas_are_closed_and_never_define_a_secret_value() -> None:
    config = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    request = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    response = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-response.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert config["additionalProperties"] is False
    assert request["additionalProperties"] is False
    assert response["additionalProperties"] is False
    route = config["$defs"]["profileRoute"]
    assert route["additionalProperties"] is False
    assert "api_key" not in route["properties"]
    assert route["properties"]["api_key_env"]["pattern"].startswith("^[A-Za-z_")
    for name in (
        "device-capability.schema.json",
        "device-state-event.schema.json",
        "device-operation-plan.schema.json",
        "release-manifest.schema.json",
        "device-snapshot.schema.json",
    ):
        schema = json.loads((WORKFLOW_ROOT / "references" / name).read_text(encoding="utf-8"))
        assert schema["additionalProperties"] is False


def test_managed_dsh_startup_keeps_launch_token_off_disk_and_kills_with_parent() -> None:
    script = (WORKFLOW_ROOT / "scripts" / "start_xiaozhi_server.ps1").read_text(
        encoding="utf-8"
    )

    assert "RedirectStandardOutput = $true" in script
    assert "CreateKillOnClose" in script
    assert "AssignProcessToJobObject" in script
    assert "ReadLineAsync" in script
    assert "-RedirectStandardOutput" not in script
    assert ".stdout.log" not in script
    assert "DshRuntimeRoot" not in script
    assert "dshConfig.dsh_home" in script
    assert "'0.2.0-rc.2', '0.2.1-rc.5'" in script
    assert "CancelDshSessionOnStart" in script
    assert "desktop-cancel-session" in script
    assert "RemoveDshQueuedItemSessionOnStart" in script
    assert "RemoveDshQueuedItemOnStart" in script
    assert "desktop-remove-queued-item" in script
    assert "'PYTHONIOENCODING'" in script
    assert "$env:PYTHONIOENCODING = 'utf-8'" in script
    assert "'PYTHONUTF8'" in script
    assert "$env:PYTHONUTF8 = '1'" in script
    assert "[switch]$EnableLingxiControl" in script
    assert "'LINGXI_CONTROL_ENABLED'" in script
    assert "$env:LINGXI_RUNTIME_CONFIG" in script
    assert "Lingxi admin and companion ports must be different" in script
    assert "LINGXI_CONTROL_PASSWORD" in script
    assert "LINGXI_CONTROL_ADMIN_PASSWORD" in script
    assert "LINGXI_CONTROL_COMPANION_PASSWORD" in script
    assert "LingxiControlPassword" not in script
    assert "LINGXI_CONTROL_BOOTSTRAP_FILE" not in script
    assert "insecure default password" in script
    assert "function Set-ProcessEnvironmentValue" in script
    assert "[object]$Value" in script
    assert 'Remove-Item -LiteralPath "Env:$Name"' in script
    assert "turns null into an inherited empty string" in script
    assert "import plugins_func.functions.hermes_bridge; import runpy, sys" in script
    assert "runpy.run_path($serverAppJson, run_name='__main__')" in script
    assert "[switch]$EnableXiaoAiMcp" in script
    assert "XIAOAI_MCP_TOKEN" in script
    assert "XIAOAI_MCP_DEVICE_ID" in script
    assert "XIAOAI_MCP_PUBLIC_BASE_URL" in script
    assert "XIAOAI_MCP_ATTACHMENT_ROOT" in script
    assert "XIAOAI_MCP_MAX_ATTACHMENT_BYTES" in script
    assert "52428800" in script
    assert "serve_xiaoai_mcp.py" in script
    assert "XiaoAi MCP loopback port is already in use" in script
    assert "$mcpStartInfo.Environment['XIAOAI_MCP_TOKEN']" in script
    assert "$mcpStartInfo.Environment.Clear()" in script
    assert "'XIAOZHI_HERMES_BINDING_STORE'" in script
    assert "$mcpStartInfo.Environment[$dshTokenEnvironmentName]" in script
    for name in (
        "XIAOAI_MCP_MAX_BODY_BYTES",
        "XIAOAI_MCP_REQUESTS_PER_MINUTE",
        "XIAOAI_MCP_MAX_CONCURRENT_REQUESTS",
        "XIAOAI_MCP_BODY_READ_TIMEOUT_SECONDS",
        "XIAOAI_MCP_MAX_INPUT_CHARS",
        "XIAOAI_MCP_MAX_OUTPUT_CHARS",
        "XIAOAI_MCP_SEND_CACHE_SECONDS",
        "XIAOAI_MCP_UPLOAD_TTL_SECONDS",
        "XIAOAI_MCP_ATTACHMENT_TTL_SECONDS",
    ):
        assert f"'{name}'" in script
    assert "$ownedXiaoAiMcpProcess.Kill($true)" in script


def test_actual_schemas_accept_the_examples_and_protocol_responses() -> None:
    config_schema = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    request_schema = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    response_schema = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-response.schema.json").read_text(
            encoding="utf-8"
        )
    )
    example = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-config.json.example").read_text(
            encoding="utf-8"
        )
    )

    jsonschema.Draft202012Validator(config_schema).validate(example)
    jsonschema.Draft202012Validator(request_schema).validate(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "voice:1",
            "operation": "dispatch",
            "input": "switch task",
            "confirmed": True,
        }
    )
    jsonschema.Draft202012Validator(request_schema).validate(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:42",
            "operation": "bind_session",
            "device_id": "esp32-s3",
            "host": "hermes",
            "profile": "default",
            "stored_session_id": "stored-1",
            "binding_revision": 0,
        }
    )
    shared_write = {
        "schema_version": "1.0",
        "caller_id": "xiaozhi-backend",
        "session_id": "socket:2",
        "operation": "dispatch_bound_voice",
        "device_id": "esp32-s3",
        "host": "hermes",
        "profile": "default",
        "stored_session_id": "stored-1",
        "binding_revision": 1,
        "input": "continue",
    }
    jsonschema.Draft202012Validator(request_schema).validate(shared_write)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(request_schema).validate(
            {**shared_write, "confirmed": True}
        )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(request_schema).validate(
            {key: value for key, value in shared_write.items() if key != "binding_revision"}
        )
    confirmed_shared_write = {
        **shared_write,
        "operation": "dispatch_shared",
        "confirmed": True,
    }
    jsonschema.Draft202012Validator(request_schema).validate(confirmed_shared_write)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(request_schema).validate(
            {
                key: value
                for key, value in confirmed_shared_write.items()
                if key != "confirmed"
            }
        )
    jsonschema.Draft202012Validator(response_schema).validate(
        {
            "schema_version": "1.0",
            "status": "outcome_unknown",
            "operation": "dispatch",
            "session_id": "voice:1",
            "run_id": "run-7",
            "error": "polling bound reached",
        }
    )
    recovery = {
        "schema_version": "1.0",
        "status": "conflict",
        "operation": "create_session",
        "session_id": "socket:2",
        "device_id": "esp32-s3",
        "host": "hermes",
        "profile": "default",
        "old_stored_session_id": "stored-1",
        "old_binding_revision": 1,
        "stored_session_id": "stored-new",
        "runtime_session_id": "runtime-new",
        "error": "binding changed",
    }
    jsonschema.Draft202012Validator(response_schema).validate(recovery)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(response_schema).validate(
            {key: value for key, value in recovery.items() if key != "old_binding_revision"}
        )
    jsonschema.Draft202012Validator(request_schema).validate(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:1",
            "operation": "bind_session",
            "device_id": "esp32-s3",
            "profile": "default",
            "stored_session_id": "stored-1",
        }
    )
    jsonschema.Draft202012Validator(response_schema).validate(
        {
            "schema_version": "1.0",
            "status": "completed",
            "operation": "dispatch_shared",
            "session_id": "socket:2",
            "device_id": "esp32-s3",
            "host": "hermes",
            "stored_session_id": "stored-1",
            "runtime_session_id": "runtime-1",
            "output": "answer",
        }
    )


def test_schema_and_parser_both_reject_whitespace_input_and_hostname_alias() -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import (
        BridgeConfig,
        BridgeContractError,
        BridgeRequest,
    )
    import pytest

    config_schema = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    request_schema = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-request.schema.json").read_text(
            encoding="utf-8"
        )
    )
    example = json.loads(
        (WORKFLOW_ROOT / "references" / "bridge-config.json.example").read_text(
            encoding="utf-8"
        )
    )
    example["profiles"][0]["api_server_base_url"] = "http://localhost:9119/p/default"
    bad_request = {
        "schema_version": "1.0",
        "caller_id": "xiaozhi-backend",
        "session_id": "voice:1",
        "operation": "dispatch",
        "input": "   ",
        "confirmed": True,
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(config_schema).validate(example)
    with pytest.raises(BridgeContractError):
        BridgeConfig.from_mapping(example)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(request_schema).validate(bad_request)
    with pytest.raises(BridgeContractError):
        BridgeRequest.from_mapping(bad_request)
