from __future__ import annotations

import asyncio
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from threading import Event
from types import ModuleType, SimpleNamespace

import pytest


WORKFLOW_ROOT = Path(__file__).resolve().parents[1]
PLUGIN = WORKFLOW_ROOT / "adapters" / "xiaozhi_server_plugin.py"
OLLAMA_ADAPTER = WORKFLOW_ROOT / "adapters" / "ollama_nonstream_tools_llm.py"

def test_plugin_exposes_two_step_tools_and_config_driven_agent_ids() -> None:
    tree = ast.parse(PLUGIN.read_text(encoding="utf-8"))
    source = PLUGIN.read_text(encoding="utf-8")

    assert tree is not None
    for tool in (
        "lingxi_stop_speaking",
        "lingxi_exit_conversation",
        "lingxi_reject_volume",
        "agent_list_agents",
        "agent_switch_agent",
        "agent_continue_switch",
        "agent_cancel_switch",
        "agent_list_sessions",
        "agent_bind_session",
        "agent_show_binding",
        "agent_unbind_session",
        "agent_dispatch_bound",
        "agent_prepare_dispatch",
        "agent_prepare_new_session",
        "agent_prepare_compress_context",
        "agent_confirm_dispatch",
        "agent_cancel_dispatch",
        "hermes_list_profiles",
        "hermes_switch_profile",
        "hermes_list_sessions",
        "hermes_bind_session",
        "hermes_show_binding",
        "hermes_unbind_session",
        "hermes_prepare_dispatch",
        "hermes_prepare_new_session",
        "hermes_prepare_compress_context",
        "hermes_clarify_clear_context",
        "hermes_confirm_dispatch",
        "hermes_cancel_dispatch",
    ):
        assert f'@register_function("{tool}"' in source
    for profile in (
        "default",
        "specialist",
        "research-agent",
        "assistant-agent",
        "standard",
        "conversation-agent",
    ):
        assert f'"{profile}"' in source
    assert "XIAOZHI_HERMES_BRIDGE_CONFIG" in source
    assert "XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256" in source
    assert "bridge configuration changed after review" in source
    assert "confirmed=True" not in source
    assert "adapter().confirm_shared_dispatch" in source
    assert "conn.device_id" not in source
    assert "_stable_device_id(conn)" in source
    assert "Action.REQLLM" not in source
    assert 'elif status == "failed"' in source
    assert 'host_name = "DeepSeek Harness"' in source
    assert "_device_response_projection(result.get(\"output\"))" in source
    assert "result=_json_result(result)" in source


def test_plugin_agent_ids_follow_reviewed_external_config(
    monkeypatch, tmp_path: Path
) -> None:
    plugins_func = ModuleType("plugins_func")
    register = ModuleType("plugins_func.register")

    class Action:
        NONE = "none"
        ERROR = "error"
        RESPONSE = "response"

    class ActionResponse:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ToolType:
        SYSTEM_CTL = "system_ctl"

    def register_function(*_args, **_kwargs):
        return lambda function: function

    register.Action = Action
    register.ActionResponse = ActionResponse
    register.ToolType = ToolType
    register.register_function = register_function
    monkeypatch.setitem(sys.modules, "plugins_func", plugins_func)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register)

    config = tmp_path / "bridge-config.json"
    config.write_text(
        json.dumps(
            {
                "profiles": [{"name": "public-hermes"}],
                "deepseek_harness": {
                    "allowed_presets": ["public-deepseek", "public-deepseek"]
                },
            }
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(config.read_bytes()).hexdigest()
    monkeypatch.setenv("XIAOZHI_HERMES_BRIDGE_CONFIG", str(config))
    monkeypatch.setenv("XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256", digest)

    spec = importlib.util.spec_from_file_location("test_configured_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._PROFILES == ["public-hermes"]
    assert module._DSH_PRESETS == ["public-deepseek"]
    assert module._AGENT_IDS == ["public-deepseek", "public-hermes"]

    monkeypatch.setenv("XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256", "0" * 64)
    bad_spec = importlib.util.spec_from_file_location("test_tampered_plugin", PLUGIN)
    assert bad_spec is not None and bad_spec.loader is not None
    bad_module = importlib.util.module_from_spec(bad_spec)
    with pytest.raises(RuntimeError, match="changed after review"):
        bad_spec.loader.exec_module(bad_module)

def _load_plugin_module(monkeypatch, name: str):
    plugins_func = ModuleType("plugins_func")
    register = ModuleType("plugins_func.register")

    class Action:
        NONE = "none"
        ERROR = "error"
        RESPONSE = "response"

    class ActionResponse:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ToolType:
        SYSTEM_CTL = "system_ctl"

    def register_function(*_args, **_kwargs):
        return lambda function: function

    register.Action = Action
    register.ActionResponse = ActionResponse
    register.ToolType = ToolType
    register.register_function = register_function
    monkeypatch.setitem(sys.modules, "plugins_func", plugins_func)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register)

    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_control_lifecycle_is_generation_bound_and_registers_only_when_mcp_ready() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    lifecycle_start = source.index("def _install_control_lifecycle_hooks():")
    lifecycle_end = source.index("\ndef _publish_control(", lifecycle_start)
    lifecycle = source[lifecycle_start:lifecycle_end]
    registration_start = source.index("async def _register_control_connection(")
    registration_end = source.index(
        "\nasync def _unregister_control_connection(", registration_start
    )
    registration = source[registration_start:registration_end]
    unregister_start = registration_end + 1
    unregister_end = source.index("\ndef _touch_control_connection(", unregister_start)
    unregistration = source[unregister_start:unregister_end]

    assert 'int(payload.get("id", 0)) != 2' in lifecycle
    assert 'result.get("nextCursor")' in lifecycle
    assert "await conn.mcp_client.is_ready()" in lifecycle
    assert "await _register_control_connection(conn, device_id.strip())" in lifecycle
    assert "await _close_with_control_cleanup(self, original_close, ws)" in lifecycle
    assert "original_handle_text = connection_module.handleTextMessage" in lifecycle
    assert "return await original_handle_text(conn, message)" in lifecycle
    assert 'if state == "start"' in lifecycle
    assert "_begin_live_capture(conn)" in lifecycle
    assert 'elif state == "detect"' in lifecycle
    assert '_begin_live_detection(conn, payload.get("text"))' in lifecycle
    assert 'elif state == "stop"' in lifecycle
    assert "_end_live_capture(conn)" in lifecycle
    assert "connection_module.handleTextMessage = handle_text_message" in lifecycle
    assert 'if state == "sentence_start"' in lifecycle
    assert "projection_allowed = not pinned_turn or pinned_turn == current_turn" in lifecycle
    assert "_append_live_spoken_answer(conn, text, turn_id=pinned_turn)" in lifecycle
    assert "_set_live_speech(conn, True)" in lifecycle
    assert 'elif state == "stop"' in lifecycle
    assert "_set_live_speech(conn, False)" in lifecycle
    assert "ConnectionHandler.clearSpeakStatus = clear_speak_status" in lifecycle
    assert "_install_privacy_logging_hooks(ConnectionHandler)" in lifecycle

    assert "uuid.uuid4().hex" in registration
    assert "connection_generation=generation" in registration
    assert '"interrupt_state": "requested"' in registration
    assert "tool_names=tool_names" in registration
    assert "inspect=inspect_device" in registration

    assert "runtime.unregister_connection(" in unregistration
    assert "connection_generation=generation" in unregistration


def test_wake_detection_publishes_immediately_and_start_reuses_its_turn(
    monkeypatch,
) -> None:
    module = _load_plugin_module(monkeypatch, "test_wake_caption_plugin")
    calls = []

    class FakeRuntime:
        def register_live_projection_connection(self, **values):
            calls.append(("register", values))

        def start_live_turn(self, **values):
            calls.append(("start", values))
            return "wake-turn"

        def publish_live_question(self, **values):
            calls.append(("question", values))

        def set_live_capture(self, **values):
            calls.append(("capture", values))

    module._control_runtime = FakeRuntime()
    module._control_runtime_initialized = True
    conn = SimpleNamespace(device_id="esp32-s3")

    assert module._begin_live_detection(conn, "你好灵溪") == "wake-turn"
    assert module._begin_live_capture(conn) == "wake-turn"
    assert module._begin_live_capture(conn) == "wake-turn"

    assert [kind for kind, _ in calls] == [
        "register",
        "start",
        "question",
        "capture",
    ]
    assert calls[1][1]["capture_active"] is False
    assert calls[2][1]["turn_id"] == "wake-turn"
    assert calls[2][1]["text"] == "你好灵溪"
    assert calls[3][1]["active"] is True
    assert conn._lingxi_live_turn_id == "wake-turn"
    assert conn._lingxi_live_detect_turn_id == ""
    assert conn._lingxi_live_detect_pending is False


def test_wake_detection_is_not_reused_after_an_answer(monkeypatch) -> None:
    module = _load_plugin_module(monkeypatch, "test_wake_answer_plugin")
    starts = []

    class FakeRuntime:
        def register_live_projection_connection(self, **_values):
            return None

        def start_live_turn(self, **values):
            starts.append(values)
            return f"turn-{len(starts)}"

        def publish_live_question(self, **_values):
            return None

        def publish_live_answer(self, **_values):
            return None

    module._control_runtime = FakeRuntime()
    module._control_runtime_initialized = True
    conn = SimpleNamespace(device_id="esp32-s3")

    assert module._begin_live_detection(conn, "你好灵溪") == "turn-1"
    module._publish_live_answer(conn, "已经回答", turn_id="turn-1")
    assert module._begin_live_capture(conn) == "turn-2"

    assert [item["capture_active"] for item in starts] == [False, True]
    assert conn._lingxi_live_detect_pending is False
    assert conn._lingxi_live_turn_id == "turn-2"


def test_plain_listen_start_without_detection_creates_a_capture_turn(monkeypatch) -> None:
    module = _load_plugin_module(monkeypatch, "test_plain_start_plugin")
    starts = []

    class FakeRuntime:
        def register_live_projection_connection(self, **_values):
            return None

        def start_live_turn(self, **values):
            starts.append(values)
            return "capture-turn"

    module._control_runtime = FakeRuntime()
    module._control_runtime_initialized = True
    conn = SimpleNamespace(device_id="esp32-s3")

    assert module._begin_live_capture(conn) == "capture-turn"
    assert starts == [
        {
            "device_id": "esp32-s3",
            "connection_generation": conn._lingxi_connection_generation,
            "capture_active": True,
        }
    ]


def test_asr_timeout_drains_one_worker_and_rejects_overlapping_inference(
    monkeypatch,
) -> None:
    module = _load_plugin_module(monkeypatch, "test_asr_timeout_plugin")
    monkeypatch.setattr(module, "_conversion_timeout_seconds", lambda *_args: 0.01)
    module._control_runtime = None
    module._control_runtime_initialized = True
    worker_started = Event()
    worker_release = Event()
    worker_finished = Event()

    class FakeAsr:
        def __init__(self):
            self.calls = 0

        async def speech_to_text(self, *_args, **_kwargs):
            self.calls += 1

            def infer():
                worker_started.set()
                worker_release.wait(2)
                worker_finished.set()
                return "识别结果", None

            return await asyncio.to_thread(infer)

    class FakeWebSocket:
        async def send(self, _payload):
            return None

    class FakeConnection:
        session_id = "voice-session"
        device_id = "esp32-s3"
        close_after_chat = True
        client_abort = False

        def __init__(self):
            self.asr = FakeAsr()
            self.tts = None
            self.websocket = FakeWebSocket()
            self.closed = 0

        def clear_queues(self):
            return None

        def clearSpeakStatus(self):
            return None

        async def close(self):
            self.closed += 1

    conn = FakeConnection()
    module._ensure_bounded_voice_converters(conn)

    async def exercise():
        assert await conn.asr.speech_to_text([], "voice-session") == ("", None)
        assert worker_started.is_set()
        assert module._asr_conversion_task is not None
        assert not module._asr_conversion_task.done()
        assert await conn.asr.speech_to_text([], "voice-session") == ("", None)
        assert conn.asr.calls == 1
        worker_release.set()
        for _ in range(100):
            if module._asr_conversion_task is None:
                break
            await asyncio.sleep(0.01)

    asyncio.run(exercise())

    assert worker_finished.is_set()
    assert module._asr_conversion_task is None
    assert conn.closed == 2


def test_edge_tts_timeout_crosses_to_owner_loop_and_closes_fail_closed(
    monkeypatch, tmp_path
) -> None:
    module = _load_plugin_module(monkeypatch, "test_tts_timeout_plugin")
    monkeypatch.setattr(module, "_conversion_timeout_seconds", lambda *_args: 0.01)
    module._control_runtime = None
    module._control_runtime_initialized = True

    class FakeTts:
        def __init__(self):
            self.calls = 0

        async def text_to_speak(self, _text, output_file):
            self.calls += 1
            Path(output_file).write_bytes(b"partial")
            await asyncio.sleep(60)

    FakeTts.__module__ = "core.providers.tts.edge"

    class FailingWebSocket:
        async def send(self, _payload):
            raise RuntimeError("socket already closed")

    class FakeConnection:
        session_id = "voice-session"
        device_id = "esp32-s3"
        close_after_chat = True
        client_abort = False

        def __init__(self):
            self.asr = None
            self.tts = FakeTts()
            self.websocket = FailingWebSocket()
            self.queue_clear_attempts = 0
            self.speak_clear_attempts = 0
            self.closed = None

        def clear_queues(self):
            self.queue_clear_attempts += 1
            raise RuntimeError("queue already closed")

        def clearSpeakStatus(self):
            self.speak_clear_attempts += 1
            raise RuntimeError("speaker already closed")

        async def close(self):
            self.closed.set()

    conn = FakeConnection()
    output_file = tmp_path / "partial.wav"

    async def exercise():
        conn.loop = asyncio.get_running_loop()
        conn.closed = asyncio.Event()
        module._ensure_bounded_voice_converters(conn)

        def synthesize(text):
            try:
                asyncio.run(conn.tts.text_to_speak(text, str(output_file)))
            except RuntimeError as exc:
                return str(exc)
            raise AssertionError("TTS timeout was not raised")

        assert "timed out" in await asyncio.to_thread(synthesize, "测试")
        await asyncio.wait_for(conn.closed.wait(), timeout=1)
        for _ in range(100):
            if getattr(conn, "_lingxi_voice_pipeline_expiry_future", None) is None:
                break
            await asyncio.sleep(0.01)
        assert "already expired" in await asyncio.to_thread(
            synthesize, "不应重试"
        )

    asyncio.run(exercise())

    assert conn.tts.calls == 1
    assert not output_file.exists()
    assert conn.queue_clear_attempts == 1
    assert conn.speak_clear_attempts == 1
    assert conn._lingxi_voice_pipeline_expiry_future is None


def test_plugin_declares_transcript_log_redaction_without_logging_caption_payloads() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    for marker in (
        "收到listen消息",
        "识别文本",
        "大模型收到用户消息",
        "发送第一段语音",
        "发送音频消息",
        "TTS请求成功",
        "语音生成成功",
    ):
        assert marker in source
    assert "[content redacted; chars=" in source
    assert 'for factory_name in ("setup_logging", "create_connection_logger")' in source
    assert "_privacy_logger_factory(factory)" in source
    assert "def _refresh_privacy_loggers(conn=None):" in source
    assert 'if name == "logger" and value is not None:' in source
    assert "_refresh_privacy_loggers(conn)" in source
    assert "connection_module.handleTextMessage = handle_text_message" in source
    assert "runtime.publish_live_question(" in source
    assert "runtime.publish_live_answer(" in source
    assert "runtime.append_live_spoken_answer(" in source
    assert "runtime.register_live_projection_connection(" in source


def test_device_response_projection_is_bounded_and_preserves_short_answers(
    monkeypatch,
) -> None:
    plugins_func = ModuleType("plugins_func")
    register = ModuleType("plugins_func.register")

    class Action:
        NONE = "none"
        ERROR = "error"
        RESPONSE = "response"

    class ActionResponse:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ToolType:
        SYSTEM_CTL = "system_ctl"

    def register_function(*_args, **_kwargs):
        return lambda function: function

    register.Action = Action
    register.ActionResponse = ActionResponse
    register.ToolType = ToolType
    register.register_function = register_function
    monkeypatch.setitem(sys.modules, "plugins_func", plugins_func)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register)

    spec = importlib.util.spec_from_file_location("test_xiaozhi_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module._device_response_projection("**同步已完成。**") == "同步已完成。"

    verbose = """**结论：同步已完成。**

| 项目 | 结果 |
| --- | --- |
| 会话 | 成功 |

- 结果写入 C:\\Users\\YOUR_NAME\\workspace\\result.json
- [详细证据](<C:\\Users\\YOUR_NAME\\workspace\\evidence.md>)
- 证据三
- 证据四
"""
    projected = module._device_response_projection(verbose)
    assert len(projected) <= 200
    assert "同步已完成" in projected
    assert "答案包含表格" in projected
    assert "完整内容已保留在电脑端当前会话" in projected
    assert "C:\\" not in projected
    assert "|" not in projected

    table_only = module._device_response_projection("| 项目 | 结果 |\n| --- | --- |\n| A | B |")
    assert table_only.startswith("Agent 任务已完成。答案包含表格。")
    assert len(table_only) <= 200

    projector = module._StreamingVoiceProjector()
    assert projector.feed("第一句。第二") == ["第一句。"]
    assert projector.feed("第一句。第二句。") == ["第二句。"]
    assert projector.feed("第一句。第二句。", final=True) == []

    policy_conn = SimpleNamespace(config={"close_connection_no_voice_time": 840})
    module._apply_voice_session_policy(policy_conn)
    assert policy_conn.config == {
        "close_connection_no_voice_time": 12,
        "end_prompt": {"enable": False},
    }


def test_direct_bound_tool_uses_stable_device_and_projects_host_response(
    monkeypatch,
) -> None:
    plugins_func = ModuleType("plugins_func")
    register = ModuleType("plugins_func.register")

    class Action:
        ERROR = "error"
        RESPONSE = "response"

    class ActionResponse:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ToolType:
        SYSTEM_CTL = "system_ctl"

    def register_function(*_args, **_kwargs):
        return lambda function: function

    register.Action = Action
    register.ActionResponse = ActionResponse
    register.ToolType = ToolType
    register.register_function = register_function
    monkeypatch.setitem(sys.modules, "plugins_func", plugins_func)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register)

    spec = importlib.util.spec_from_file_location("test_direct_bound_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    class FakeAdapter:
        async def dispatch_bound(self, session_id, *, device_id, prompt):
            calls.append((session_id, device_id, prompt))
            conn._lingxi_live_turn_id = "turn-2"
            return {
                "status": "completed",
                "operation": "dispatch_shared",
                "output": "**同步完成。**",
            }

    module._adapter = FakeAdapter()
    live_calls = []

    class FakeRuntime:
        def publish(self, **values):
            live_calls.append(("state", values))

        def register_live_projection_connection(self, **values):
            live_calls.append(("register", values))

        def start_live_turn(self, **values):
            live_calls.append(("start", values))
            return "turn-1"

        def publish_live_question(self, **values):
            live_calls.append(("question", values))
            return True

        def publish_live_answer(self, **values):
            live_calls.append(("answer", values))
            return True

        def append_live_spoken_answer(self, **values):
            live_calls.append(("spoken", values))
            return True

    module._control_runtime = FakeRuntime()
    module._control_runtime_initialized = True
    conn = SimpleNamespace(
        session_id="voice-session",
        device_id="esp32-s3",
    )
    result = asyncio.run(module.agent_dispatch_bound(conn, "普通正文"))

    assert calls == [("voice-session", "esp32-s3", "普通正文")]
    assert result.action == "response"
    assert result.response == "同步完成。"
    assert [item[0] for item in live_calls] == [
        "register",
        "start",
        "question",
        "state",
        "answer",
        "state",
    ]
    generation = conn._lingxi_connection_generation
    assert generation
    assert live_calls[0][1] == {
        "device_id": "esp32-s3",
        "connection_generation": generation,
    }
    assert live_calls[2][1]["text"] == "普通正文"
    assert live_calls[4][1]["text"] == "**同步完成。**"
    assert live_calls[4][1]["turn_id"] == "turn-1"
    assert conn._lingxi_live_tts_turn_id == "turn-1"
    module._append_live_spoken_answer(
        conn, "旧回答的 TTS 回退", turn_id=conn._lingxi_live_tts_turn_id
    )
    assert live_calls[-1][0] == "spoken"
    assert live_calls[-1][1]["turn_id"] == "turn-1"


def test_stopped_stream_cannot_queue_old_agent_text_into_the_next_turn(
    monkeypatch,
) -> None:
    plugins_func = ModuleType("plugins_func")
    register = ModuleType("plugins_func.register")

    class Action:
        NONE = "none"
        ERROR = "error"
        RESPONSE = "response"

    class ActionResponse:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ToolType:
        SYSTEM_CTL = "system_ctl"

    def register_function(*_args, **_kwargs):
        return lambda function: function

    register.Action = Action
    register.ActionResponse = ActionResponse
    register.ToolType = ToolType
    register.register_function = register_function
    monkeypatch.setitem(sys.modules, "plugins_func", plugins_func)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register)

    dto_module = ModuleType("core.providers.tts.dto.dto")
    dto_module.ContentType = SimpleNamespace(TEXT="text")
    for name in ("core", "core.providers", "core.providers.tts", "core.providers.tts.dto"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, "core.providers.tts.dto.dto", dto_module)

    spec = importlib.util.spec_from_file_location("test_stopped_stream_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._control_runtime = None
    module._control_runtime_initialized = True

    class FakeAdapter:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def dispatch_bound(
            self, session_id, *, device_id, prompt, on_progress
        ):
            await on_progress("第一句。")
            self.started.set()
            await self.release.wait()
            await on_progress("第一句。旧回答不得回灌。")
            return {
                "status": "completed",
                "output": "第一句。旧回答不得回灌。",
            }

    class FakeTts:
        def __init__(self):
            self.calls = []

        def tts_one_sentence(
            self, conn, content_type, *, content_detail, sentence_id
        ):
            self.calls.append((content_detail, sentence_id))

    class FakeWebSocket:
        async def send(self, _payload):
            return None

    class FakeConnection:
        session_id = "voice-session"
        device_id = "esp32-s3"
        sentence_id = "sentence-old"
        close_after_chat = False
        client_abort = False

        def __init__(self):
            self.tts = FakeTts()
            self.websocket = FakeWebSocket()
            self.config = {}

        def clear_queues(self):
            return None

        def clearSpeakStatus(self):
            return None

    adapter = FakeAdapter()
    module._adapter = adapter
    conn = FakeConnection()

    async def exercise():
        task = asyncio.create_task(module.agent_dispatch_bound(conn, "长任务"))
        await adapter.started.wait()
        assert conn.tts.calls == [("第一句。", "sentence-old")]
        await module._stop_current_speech(conn)
        conn.client_abort = False
        conn.sentence_id = "sentence-new"
        adapter.release.set()
        result = await task
        assert result.action == "none"

    asyncio.run(exercise())
    assert conn.tts.calls == [("第一句。", "sentence-old")]


def test_control_failure_logging_projects_only_a_bounded_error_code() -> None:
    source = PLUGIN.read_text(encoding="utf-8")

    assert "def _control_error_label(exc):" in source
    assert 're.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)' in source
    assert "_control_error_label(exc)" in source


def test_ollama_adapter_is_loopback_safe_bounded_and_deterministic() -> None:
    tree = ast.parse(OLLAMA_ADAPTER.read_text(encoding="utf-8"))
    source = OLLAMA_ADAPTER.read_text(encoding="utf-8")

    assert tree is not None
    assert "httpx.Client(trust_env=False)" in source
    assert "httpx.Client(trust_env=True)" in source
    assert 'config.get("api_key_env")' in source
    assert 'config.get("max_tokens", 512)' in source
    assert "_forced_gateway_call" in source
    for tool in (
        "hermes_list_profiles",
        "hermes_switch_profile",
        "hermes_prepare_dispatch",
        "hermes_confirm_dispatch",
        "hermes_cancel_dispatch",
    ):
        assert f'"{tool}"' in source
    assert 'request["tool_choice"]' not in source


def test_deterministic_gateway_calls_preserve_prompt_and_stop_after_tool(monkeypatch) -> None:
    core = ModuleType("core")
    providers = ModuleType("core.providers")
    llm = ModuleType("core.providers.llm")
    base = ModuleType("core.providers.llm.base")
    base.LLMProviderBase = object
    for name, module in (
        ("core", core),
        ("core.providers", providers),
        ("core.providers.llm", llm),
        ("core.providers.llm.base", base),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("test_ollama_adapter", OLLAMA_ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    functions = [
        {
            "type": "function",
            "function": {
                "name": "lingxi_stop_speaking",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lingxi_exit_conversation",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lingxi_reject_volume",
                "parameters": {
                    "type": "object",
                    "properties": {"volume": {"type": "integer"}},
                    "required": ["volume"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_continue_pending_confirmation",
                "parameters": {
                    "type": "object",
                    "properties": {"choice": {"type": "string"}},
                    "required": ["choice"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "self_audio_speaker_get_volume",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "self_audio_speaker_set_volume_verified",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "volume": {"type": "integer", "minimum": 0, "maximum": 100}
                    },
                    "required": ["volume"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "self_audio_speaker_adjust_volume",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "delta": {"type": "integer", "minimum": -100, "maximum": 100}
                    },
                    "required": ["delta"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_list_agents",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_list_sessions",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "enum": ["hermes", "deepseek-harness"]},
                        "agent_id": {"type": "string", "enum": ["default", "assistant-agent"]},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_switch_agent",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "enum": ["hermes", "deepseek-harness"]},
                        "agent_id": {
                            "type": "string",
                            "enum": [
                                "default",
                                "assistant-agent",
                                "game-assistant",
                            ],
                        },
                    },
                    "required": ["agent_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_continue_switch",
                "parameters": {
                    "type": "object",
                    "properties": {"choice": {"type": "string"}},
                    "required": ["choice"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_cancel_switch",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_bind_session",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "host": {"type": "string", "enum": ["hermes", "deepseek-harness"]},
                        "agent_id": {"type": "string", "enum": ["default", "assistant-agent"]},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_show_binding",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "agent_dispatch_bound",
                "parameters": {
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_prepare_new_session",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_prepare_compress_context",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_clarify_clear_context",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_prepare_dispatch",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_switch_profile",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "profile": {"type": "string", "enum": ["default", "assistant-agent"]}
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_confirm_dispatch",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "hermes_cancel_dispatch",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]

    prepared = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "交给 Hermes：检查 Agent Workflow Hub 当前状态"}],
        functions,
    )
    assert prepared.function.name == "hermes_prepare_dispatch"
    assert json.loads(prepared.function.arguments) == {
        "prompt": "检查 Agent Workflow Hub 当前状态"
    }

    forwarded_list_request = module.LLMProvider._forced_gateway_call(
        [
            {
                "role": "user",
                "content": (
                    "交给当前profile：请查阅 Codex 聊天记录，列出 "
                    "2026-10-19 我们完成的三项关键验收，并给出每项证据。"
                ),
            }
        ],
        functions,
    )
    assert forwarded_list_request.function.name == "hermes_prepare_dispatch"
    assert json.loads(forwarded_list_request.function.arguments) == {
        "prompt": (
            "请查阅 Codex 聊天记录，列出 2026-10-19 "
            "我们完成的三项关键验收，并给出每项证据。"
        )
    }

    forwarded_current_agent = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "交给当前智能体：今天有执行自动同步任务吗？"}],
        functions,
    )
    assert forwarded_current_agent.function.name == "hermes_prepare_dispatch"
    assert json.loads(forwarded_current_agent.function.arguments) == {
        "prompt": "今天有执行自动同步任务吗？"
    }

    forwarded_agent_alias = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "交给智能体：查询今日同步任务。"}],
        functions,
    )
    assert forwarded_agent_alias.function.name == "hermes_prepare_dispatch"
    assert json.loads(forwarded_agent_alias.function.arguments) == {
        "prompt": "查询今日同步任务。"
    }

    physical_voice_message = [
        {
            "role": "user",
            "content": {
                "content": "交给当前智能体查询今日同步任务。",
                "language": "zh",
                "emotion": "😶",
            },
        }
    ]
    forwarded_physical_voice = module.LLMProvider._forced_gateway_call(
        physical_voice_message, functions
    )
    assert forwarded_physical_voice.function.name == "hermes_prepare_dispatch"
    assert json.loads(forwarded_physical_voice.function.arguments) == {
        "prompt": "查询今日同步任务。"
    }
    assert module.LLMProvider._without_thinking(physical_voice_message)[0][
        "content"
    ] == "/no_think 交给当前智能体查询今日同步任务。"

    encoded_physical_voice_message = [
        {
            "role": "user",
            "content": json.dumps(
                {
                    "content": "交给当前智能体查询今日同步任务。",
                    "language": "zh",
                    "emotion": "😶",
                },
                ensure_ascii=False,
            ),
        }
    ]
    forwarded_encoded_voice = module.LLMProvider._forced_gateway_call(
        encoded_physical_voice_message, functions
    )
    assert forwarded_encoded_voice.function.name == "hermes_prepare_dispatch"
    assert json.loads(forwarded_encoded_voice.function.arguments) == {
        "prompt": "查询今日同步任务。"
    }

    for incomplete_text in (
        "交给当前",
        "交给当前智能",
        "交给当前只能提",
        "交给智能体",
        "交给当前；",
    ):
        assert module.LLMProvider._incomplete_forwarding_request(
            [{"role": "user", "content": incomplete_text}]
        )
        assert module.LLMProvider._forced_gateway_call(
            [{"role": "user", "content": incomplete_text}], functions
        ) is None

    for ordinary_text in (
        "请当前阶段继续",
        "请当前台确认",
        "请Hermesian解释",
        "让当前agentic解释",
        "请智能体解释这个问题",
        "让智能体解释这个问题",
    ):
        assert not module.LLMProvider._incomplete_forwarding_request(
            [{"role": "user", "content": ordinary_text}]
        )
        assert module.LLMProvider._forced_gateway_call(
            [{"role": "user", "content": ordinary_text}], functions
        ) is None

    provider = object.__new__(module.LLMProvider)
    provider._pending_forwarding = {}
    provider._pending_forwarding_lock = module.Lock()
    clarification = list(
        provider.response_with_functions(
            "socket-session",
            [{"role": "user", "content": "交给当前"}],
            functions=functions,
        )
    )
    assert clarification == [("请在“交给当前智能体”后说出完整问题。", None)]

    continued_forwarding = list(
        provider.response_with_functions(
            "socket-session",
            [{"role": "user", "content": "今日同步任务。"}],
            functions=functions,
        )
    )
    assert continued_forwarding[0][0] is None
    continued_call = continued_forwarding[0][1][0]
    assert continued_call.function.name == "hermes_prepare_dispatch"
    assert json.loads(continued_call.function.arguments) == {
        "prompt": "今日同步任务。"
    }

    provider._arm_pending_forwarding("session-a")
    assert not provider._consume_pending_forwarding("session-b")
    assert provider._consume_pending_forwarding("session-a")

    clock = [100.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])
    provider._arm_pending_forwarding("expired-session")
    clock[0] = 131.0
    assert not provider._consume_pending_forwarding("expired-session")
    assert "expired-session" not in provider._pending_forwarding

    progress_then_confirm = list(
        provider.response_with_functions(
            "another-socket-session",
            [
                {
                    "role": "assistant",
                    "content": "准备开启新会话。请明确说“确认”后才会执行，或说“取消”。",
                },
                {"role": "user", "content": "确认"},
            ],
            functions=functions,
        )
    )
    assert progress_then_confirm[0] == ("已确认，正在处理，请稍候。", None)
    assert progress_then_confirm[1][0] is None
    assert progress_then_confirm[1][1][0].function.name == "hermes_confirm_dispatch"

    after_older_tool = module.LLMProvider._forced_gateway_call(
        [
            {"role": "user", "content": "旧请求"},
            {"role": "tool", "content": "旧结果"},
            {"role": "user", "content": "交给当前智能体：新的完整问题"},
        ],
        functions,
    )
    assert after_older_tool.function.name == "hermes_prepare_dispatch"
    assert json.loads(after_older_tool.function.arguments) == {
        "prompt": "新的完整问题"
    }

    listed_agents = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "现在有哪些智能体"}], functions
    )
    assert listed_agents.function.name == "agent_list_agents"

    listed_profiles = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "现在有哪些 Profile"}], functions
    )
    assert listed_profiles.function.name == "agent_list_agents"

    current_profile = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "当前在哪个 profi 呀？"}], functions
    )
    assert current_profile.function.name == "agent_show_binding"

    listed_dsh = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "列出 DeepSeek assistant-agent 的会话"}], functions
    )
    assert listed_dsh.function.name == "agent_list_sessions"
    assert json.loads(listed_dsh.function.arguments) == {
        "agent_id": "assistant-agent",
        "limit": 5,
        "host": "deepseek-harness",
    }
    assert module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "列出 assistant-agent 的会话"}], functions
    ) is None

    switched = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "切换到 assistant-agent Profile"}], functions
    )
    assert switched.function.name == "agent_switch_agent"
    assert json.loads(switched.function.arguments) == {"agent_id": "assistant-agent"}

    switched_spoken_alias = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "切换到游戏智能体"}], functions
    )
    assert switched_spoken_alias.function.name == "agent_switch_agent"
    assert json.loads(switched_spoken_alias.function.arguments) == {
        "agent_id": "game-assistant"
    }

    switched_explicit_host = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "切换到 DeepSeek 游戏智能体"}], functions
    )
    assert json.loads(switched_explicit_host.function.arguments) == {
        "agent_id": "game-assistant",
        "host": "deepseek-harness",
    }

    continued_switch = module.LLMProvider._forced_gateway_call(
        [
            {"role": "assistant", "content": "切换尚未完成。请说 Hermes 或 DeepSeek。"},
            {"role": "user", "content": "DeepSeek"},
        ],
        functions,
    )
    assert continued_switch.function.name == "agent_continue_switch"
    assert json.loads(continued_switch.function.arguments) == {"choice": "DeepSeek"}

    cancelled_switch = module.LLMProvider._forced_gateway_call(
        [
            {"role": "assistant", "content": "切换尚未完成。请选择已有对话。"},
            {"role": "user", "content": "取消切换"},
        ],
        functions,
    )
    assert cancelled_switch.function.name == "agent_cancel_switch"
    assert json.loads(cancelled_switch.function.arguments) == {}

    assert module.LLMProvider._forced_gateway_call(
        [
            {"role": "assistant", "content": "切换尚未完成。请选择已有对话。"},
            {"role": "user", "content": "取消切换"},
            {"role": "assistant", "content": "已取消 Agent 切换，原绑定保持不变。"},
            {"role": "user", "content": "这是普通聊天"},
        ],
        functions,
    ) is None

    follow_up = module.LLMProvider._forced_gateway_call(
        [
                {"role": "user", "content": "绑定 assistant-agent 的对话"},
                {"role": "assistant", "content": "已绑定到 assistant-agent 的对话“任务”。"},
            {"role": "user", "content": "现在我们rag数据库有哪些项目了？"},
        ],
        functions,
    )
    assert follow_up.function.name == "hermes_prepare_dispatch"
    assert json.loads(follow_up.function.arguments) == {
        "prompt": "现在我们rag数据库有哪些项目了？"
    }

    new_session = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "开启新会话"}], functions
    )
    assert new_session.function.name == "hermes_prepare_new_session"

    embedded_new_session = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "请解释正文里的开启新会话是什么意思"}],
        functions,
    )
    assert embedded_new_session is None

    forwarded_embedded_new_session = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "交给 Hermes：请在正文中写出开启新会话"}],
        functions,
    )
    assert forwarded_embedded_new_session.function.name == "hermes_prepare_dispatch"
    assert json.loads(forwarded_embedded_new_session.function.arguments) == {
        "prompt": "请在正文中写出开启新会话"
    }

    compress = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "压缩当前上下文"}], functions
    )
    assert compress.function.name == "hermes_prepare_compress_context"

    clarify = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "清除上下文"}], functions
    )
    assert clarify.function.name == "hermes_clarify_clear_context"
    restart_clarify = module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "从头开始"}], functions
    )
    assert restart_clarify.function.name == "hermes_clarify_clear_context"

    assert module.LLMProvider._forced_gateway_call(
        [
            {"role": "user", "content": "切换到 assistant-agent Profile"},
            {"role": "assistant", "content": "无法切换到 assistant-agent Profile。"},
            {"role": "user", "content": "这是普通聊天"},
        ],
        functions,
    ) is None

    assert module.LLMProvider._forced_gateway_call(
        [
            {"role": "user", "content": "确认"},
            {"role": "assistant", "tool_calls": []},
            {"role": "tool", "content": "done"},
        ],
        functions,
    ) is None
    assert module.LLMProvider._forced_gateway_call(
        [{"role": "user", "content": "不要确认"}], functions
    ) is None
    cancelled = module.LLMProvider._forced_gateway_call(
        [
            {
                "role": "assistant",
                "content": "准备压缩上下文。请明确说“确认”后才会执行，或说“取消”。",
            },
            {"role": "user", "content": "不要确认"},
        ],
        functions,
    )
    assert cancelled.function.name == "hermes_cancel_dispatch"

    bound_provider = module.LLMProvider({"mode": "bound_agent"})
    assert bound_provider.client is None
    assert bound_provider.base_url is None

    def bound_call(text):
        routed = list(
            bound_provider.response_with_functions(
                "fresh-websocket-session",
                [{"role": "user", "content": text}],
                functions=functions,
            )
        )
        assert routed[0][0] is None
        return routed[0][1][0]

    ordinary = bound_call("现在我们 RAG 数据库有哪些项目了？")
    assert ordinary.function.name == "agent_dispatch_bound"
    assert json.loads(ordinary.function.arguments) == {
        "prompt": "现在我们 RAG 数据库有哪些项目了？"
    }

    legacy_prefix = bound_call("交给当前智能体：查询今天的同步任务")
    assert legacy_prefix.function.name == "agent_dispatch_bound"
    assert json.loads(legacy_prefix.function.arguments) == {
        "prompt": "查询今天的同步任务"
    }

    embedded_lifecycle = bound_call("请解释开启新会话是什么意思")
    assert embedded_lifecycle.function.name == "agent_dispatch_bound"
    assert json.loads(embedded_lifecycle.function.arguments) == {
        "prompt": "请解释开启新会话是什么意思"
    }

    explained_switch = bound_call("请解释为什么切换到三冰智能体会失败")
    assert explained_switch.function.name == "agent_dispatch_bound"
    explained_binding = bound_call(
        "请解释绑定 DeepSeek assistant-agent 的会话 ID：session-1234 会发生什么"
    )
    assert explained_binding.function.name == "agent_dispatch_bound"
    assert bound_call("执行").function.name == "agent_dispatch_bound"

    actual_binding = bound_call(
        "绑定 DeepSeek assistant-agent 的会话 ID：session-1234"
    )
    assert actual_binding.function.name == "agent_bind_session"
    assert json.loads(actual_binding.function.arguments) == {
        "agent_id": "assistant-agent",
        "stored_session_id": "session-1234",
        "host": "deepseek-harness",
    }

    assert bound_call("开启新会话").function.name == "hermes_prepare_new_session"
    assert (
        bound_call("压缩上下文").function.name
        == "hermes_prepare_compress_context"
    )
    assert bound_call("清除上下文").function.name == "hermes_clarify_clear_context"
    confirmation = list(
        bound_provider.response_with_functions(
            "fresh-websocket-session",
            [
                {
                    "role": "assistant",
                    "content": "准备压缩上下文。请明确说“确认”后才会执行，或说“取消”。",
                },
                {"role": "user", "content": "确认"},
            ],
            functions=functions,
        )
    )
    assert confirmation[0] == ("已确认，正在处理，请稍候。", None)
    assert confirmation[1][1][0].function.name == "hermes_confirm_dispatch"
    assert bound_call("现在有哪些智能体").function.name == "agent_list_agents"
    assert bound_call("查询有哪些 profile").function.name == "agent_list_agents"

    for stop_text in ("停止", "闭嘴", "别说了", "停止播报"):
        assert bound_call(stop_text).function.name == "lingxi_stop_speaking"
    for exit_text in ("退出", "退出会话", "结束对话"):
        assert bound_call(exit_text).function.name == "lingxi_exit_conversation"

    current_volume = bound_call("当前音量是多少")
    assert current_volume.function.name == "self_audio_speaker_get_volume"
    assert json.loads(current_volume.function.arguments) == {}
    louder = bound_call("声音大一点")
    assert louder.function.name == "self_audio_speaker_adjust_volume"
    assert json.loads(louder.function.arguments) == {"delta": 10}
    quieter = bound_call("音量降低 25%")
    assert quieter.function.name == "self_audio_speaker_adjust_volume"
    assert json.loads(quieter.function.arguments) == {"delta": -25}
    absolute_volume = bound_call("音量调到 35")
    assert absolute_volume.function.name == "self_audio_speaker_set_volume_verified"
    assert json.loads(absolute_volume.function.arguments) == {"volume": 35}
    assert json.loads(bound_call("静音").function.arguments) == {"volume": 0}
    invalid_volume = bound_call("音量调到 120")
    assert invalid_volume.function.name == "lingxi_reject_volume"
    assert json.loads(invalid_volume.function.arguments) == {"volume": 120}
    for text, value in (
        ("音量调到 1000", 1000),
        ("音量调到 -1", -1),
        ("音量调到一百二十", 120),
    ):
        rejected = bound_call(text)
        assert rejected.function.name == "lingxi_reject_volume"
        assert json.loads(rejected.function.arguments) == {"volume": value}
    chinese_volume = bound_call("音量调到三十五")
    assert chinese_volume.function.name == "self_audio_speaker_set_volume_verified"
    assert json.loads(chinese_volume.function.arguments) == {"volume": 35}
    signed_relative = bound_call("音量调整-15")
    assert signed_relative.function.name == "self_audio_speaker_adjust_volume"
    assert json.loads(signed_relative.function.arguments) == {"delta": -15}
    clamped_relative = bound_call("音量提高一千")
    assert clamped_relative.function.name == "self_audio_speaker_adjust_volume"
    assert json.loads(clamped_relative.function.arguments) == {"delta": 100}

    for ordinary_text in (
        "请解释为什么声音大一点没有生效",
        "正文里写着闭嘴是什么意思",
        "如何结束对话但先不要执行",
    ):
        assert bound_call(ordinary_text).function.name == "agent_dispatch_bound"

    # P0 keeps priority, while P1 owns every non-P0 utterance until the
    # pending management operation is confirmed or cancelled.
    pending_stop = module.LLMProvider._forced_gateway_call(
        [
            {"role": "assistant", "content": "切换尚未完成。请选择已有对话。"},
            {"role": "user", "content": "闭嘴"},
        ],
        functions,
        direct_bound=True,
    )
    assert pending_stop.function.name == "lingxi_stop_speaking"
    pending_volume = module.LLMProvider._forced_gateway_call(
        [
            {
                "role": "assistant",
                "content": "准备压缩上下文。请明确说‘确认’后才会执行，或说‘取消’。",
            },
            {"role": "user", "content": "声音小一点"},
        ],
        functions,
        direct_bound=True,
    )
    assert pending_volume.function.name == "agent_continue_pending_confirmation"
    assert json.loads(pending_volume.function.arguments) == {"choice": "声音小一点"}
    pending_catalog = module.LLMProvider._forced_gateway_call(
        [
            {
                "role": "assistant",
                "content": "待确认操作仍未执行。请明确说‘确认’后才会执行，或说‘取消’。",
            },
            {"role": "user", "content": "有哪些 profile"},
        ],
        functions,
        direct_bound=True,
    )
    assert pending_catalog.function.name == "agent_continue_pending_confirmation"
    resumed_confirmation = module.LLMProvider._forced_gateway_call(
        [
            {
                "role": "assistant",
                "content": "待确认操作仍未执行。请明确说‘确认’后才会执行，或说‘取消’。",
            },
            {"role": "user", "content": "确认"},
        ],
        functions,
        direct_bound=True,
    )
    assert resumed_confirmation.function.name == "hermes_confirm_dispatch"


def test_local_stop_and_exit_controls_are_silent_and_preserve_durable_binding(
    monkeypatch,
) -> None:
    plugins_func = ModuleType("plugins_func")
    register = ModuleType("plugins_func.register")

    class Action:
        NONE = "none"
        ERROR = "error"
        RESPONSE = "response"

    class ActionResponse:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ToolType:
        SYSTEM_CTL = "system_ctl"

    def register_function(*_args, **_kwargs):
        return lambda function: function

    register.Action = Action
    register.ActionResponse = ActionResponse
    register.ToolType = ToolType
    register.register_function = register_function
    monkeypatch.setitem(sys.modules, "plugins_func", plugins_func)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register)

    spec = importlib.util.spec_from_file_location("test_local_controls_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adapter_calls = []

    class FakeAdapter:
        async def pending_for_device(self, *, device_id):
            adapter_calls.append(("pending", device_id))
            return {
                "pending": True,
                "pending_operation": "compress_session",
                "pending_state": "prepared",
            }

        async def cancel_prepared_for_device(self, *, device_id):
            adapter_calls.append(device_id)
            return {"cancelled_count": 1}

    class FakeWebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    class FakeConnection:
        session_id = "voice-session"
        device_id = "esp32-s3"
        close_after_chat = True
        client_abort = False

        def __init__(self):
            self.websocket = FakeWebSocket()
            self.queues_cleared = 0
            self.speak_cleared = 0
            self.closed = 0

        def clear_queues(self):
            self.queues_cleared += 1

        def clearSpeakStatus(self):
            self.speak_cleared += 1

        async def close(self):
            self.closed += 1

    module._adapter = FakeAdapter()
    conn = FakeConnection()
    stopped = asyncio.run(module.lingxi_stop_speaking(conn))
    assert stopped.action == "none"
    assert conn.client_abort is True
    assert conn.close_after_chat is False
    assert conn.queues_cleared == 1
    assert conn.speak_cleared == 1
    assert conn.closed == 0
    assert conn.websocket.sent[-1] == {
        "type": "tts",
        "state": "stop",
        "session_id": "voice-session",
    }
    assert adapter_calls == []

    held = asyncio.run(
        module.agent_continue_pending_confirmation(conn, "声音小一点")
    )
    assert held.action == "response"
    assert "请明确说“确认”后才会执行" in held.response
    assert adapter_calls == [("pending", "esp32-s3")]

    conn.client_abort = False
    module._start_switch(
        "esp32-s3",
        "voice-session",
        {"stage": "host", "agent_id": "assistant-agent", "candidate_hosts": ["hermes"]},
    )
    exited = asyncio.run(module.lingxi_exit_conversation(conn))
    assert exited.action == "none"
    assert module._active_switch("esp32-s3") is None
    assert adapter_calls == [("pending", "esp32-s3"), "esp32-s3"]
    assert conn.client_abort is True
    assert conn.queues_cleared == 2
    assert conn.speak_cleared == 2
    assert conn.closed == 1

    rejected = asyncio.run(module.lingxi_reject_volume(conn, 120))
    assert rejected.action == "response"
    assert rejected.response == "音量只能设置为零到一百。"


def test_stop_and_exit_close_even_when_device_cleanup_is_already_broken(
    monkeypatch,
) -> None:
    module = _load_plugin_module(monkeypatch, "test_failed_device_cleanup_plugin")
    module._control_runtime = None
    module._control_runtime_initialized = True

    class FakeAdapter:
        async def cancel_prepared_for_device(self, *, device_id):
            return {"status": "ok", "cancelled_count": 0}

    class FailingWebSocket:
        def __init__(self):
            self.attempts = 0

        async def send(self, _payload):
            self.attempts += 1
            raise RuntimeError("socket already closed")

    class FakeConnection:
        session_id = "voice-session"
        device_id = "esp32-s3"
        close_after_chat = True
        client_abort = False
        _lingxi_live_detect_pending = True
        _lingxi_live_detect_turn_id = "wake-turn"

        def __init__(self):
            self.websocket = FailingWebSocket()
            self.queue_clear_attempts = 0
            self.speak_clear_attempts = 0
            self.closed = 0

        def clear_queues(self):
            self.queue_clear_attempts += 1
            raise RuntimeError("queue already closed")

        def clearSpeakStatus(self):
            self.speak_clear_attempts += 1
            raise RuntimeError("speaker already closed")

        async def close(self):
            self.closed += 1

    module._adapter = FakeAdapter()
    conn = FakeConnection()

    stopped = asyncio.run(module.lingxi_stop_speaking(conn))
    assert stopped.action == "none"
    assert conn.closed == 0

    conn.client_abort = False
    exited = asyncio.run(module.lingxi_exit_conversation(conn))
    assert exited.action == "none"
    assert conn.closed == 1
    assert conn.websocket.attempts == 2
    assert conn.queue_clear_attempts == 2
    assert conn.speak_clear_attempts == 2
    assert conn._lingxi_live_detect_pending is False
    assert conn._lingxi_live_detect_turn_id == ""


def test_cancelled_stop_still_clears_all_local_speech_state(monkeypatch) -> None:
    module = _load_plugin_module(monkeypatch, "test_cancelled_stop_plugin")
    state_changes = []
    monkeypatch.setattr(
        module,
        "_set_live_capture",
        lambda _conn, active: state_changes.append(("capture", active)),
    )
    monkeypatch.setattr(
        module,
        "_set_live_speech",
        lambda _conn, active: state_changes.append(("speech", active)),
    )
    monkeypatch.setattr(
        module,
        "_publish_control",
        lambda _conn, state, **values: state_changes.append(
            (state, values.get("operation"))
        ),
    )

    class BlockingWebSocket:
        def __init__(self):
            self.started = asyncio.Event()

        async def send(self, _payload):
            self.started.set()
            await asyncio.Event().wait()

    class FakeConnection:
        session_id = "voice-session"
        close_after_chat = True
        client_abort = False
        _lingxi_live_detect_pending = True
        _lingxi_live_detect_turn_id = "wake-turn"

        def __init__(self):
            self.websocket = BlockingWebSocket()
            self.queues_cleared = 0
            self.speak_cleared = 0

        def clear_queues(self):
            self.queues_cleared += 1

        def clearSpeakStatus(self):
            self.speak_cleared += 1

    conn = FakeConnection()

    async def exercise():
        task = asyncio.create_task(module._stop_current_speech(conn))
        await conn.websocket.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert conn.queues_cleared == 1
    assert conn.speak_cleared == 1
    assert conn._lingxi_live_detect_pending is False
    assert state_changes == [
        ("capture", False),
        ("speech", False),
        ("idle", "stop_speaking"),
    ]


def test_cancelled_exit_closes_without_cancelling_shared_switch_future(
    monkeypatch,
) -> None:
    module = _load_plugin_module(monkeypatch, "test_cancelled_exit_plugin")

    class FakeConnection:
        session_id = "voice-session"
        device_id = "esp32-s3"

        def __init__(self):
            self.closed = 0

        async def close(self):
            self.closed += 1

    async def cancel_during_binding_wait():
        binding_done = asyncio.get_running_loop().create_future()
        monkeypatch.setattr(
            module,
            "_cancel_switch_for_exit",
            lambda _device_id: (True, binding_done),
        )
        conn = FakeConnection()
        task = asyncio.create_task(module.lingxi_exit_conversation(conn))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert conn.closed == 1
        assert not binding_done.cancelled()
        binding_done.set_result({"status": "cancelled_and_restored"})

    asyncio.run(cancel_during_binding_wait())


def test_cancelled_exit_during_prepared_cleanup_still_closes(monkeypatch) -> None:
    module = _load_plugin_module(monkeypatch, "test_cancelled_prepared_plugin")
    monkeypatch.setattr(
        module, "_cancel_switch_for_exit", lambda _device_id: (False, None)
    )

    class BlockingAdapter:
        def __init__(self):
            self.started = asyncio.Event()

        async def cancel_prepared_for_device(self, *, device_id):
            self.started.set()
            await asyncio.Event().wait()

    class FakeConnection:
        session_id = "voice-session"
        device_id = "esp32-s3"

        def __init__(self):
            self.closed = 0

        async def close(self):
            self.closed += 1

    async def exercise():
        adapter = BlockingAdapter()
        module._adapter = adapter
        conn = FakeConnection()
        task = asyncio.create_task(module.lingxi_exit_conversation(conn))
        await adapter.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert conn.closed == 1

    asyncio.run(exercise())


def test_control_close_logs_unregister_failure_and_preserves_cancellation(
    monkeypatch,
) -> None:
    module = _load_plugin_module(monkeypatch, "test_control_close_plugin")
    warnings = []
    close_calls = []

    class FakeLogger:
        def bind(self, **_values):
            return self

        def warning(self, value):
            warnings.append(value)

    conn = SimpleNamespace(
        logger=FakeLogger(),
        _lingxi_live_detect_pending=True,
        _lingxi_live_detect_turn_id="wake-turn",
    )

    async def original_close(live_conn, ws):
        close_calls.append((live_conn, ws))
        return "closed"

    async def failed_unregister(_conn):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(module, "_unregister_control_connection", failed_unregister)
    assert (
        asyncio.run(module._close_with_control_cleanup(conn, original_close, "ws"))
        == "closed"
    )
    assert len(close_calls) == 1
    assert warnings and "RuntimeError" in warnings[0]
    assert conn._lingxi_live_detect_pending is False

    async def cancelled_unregister(_conn):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        module, "_unregister_control_connection", cancelled_unregister
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(module._close_with_control_cleanup(conn, original_close, None))
    assert len(close_calls) == 2


def test_agent_catalog_marks_current_binding_and_unavailable_entries(monkeypatch) -> None:
    plugins_func = ModuleType("plugins_func")
    register = ModuleType("plugins_func.register")

    class Action:
        ERROR = "error"
        RESPONSE = "response"

    class ActionResponse:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ToolType:
        SYSTEM_CTL = "system_ctl"

    def register_function(*_args, **_kwargs):
        return lambda function: function

    register.Action = Action
    register.ActionResponse = ActionResponse
    register.ToolType = ToolType
    register.register_function = register_function
    monkeypatch.setitem(sys.modules, "plugins_func", plugins_func)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register)

    spec = importlib.util.spec_from_file_location("test_agent_catalog_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeAdapter:
        async def list_agents(self, session_id):
            assert session_id == "voice-session"
            return {
                "agents": [
                    {"host": "hermes", "id": "default", "available": True},
                    {
                        "host": "deepseek-harness",
                        "id": "standard",
                        "available": False,
                    },
                    {
                        "host": "deepseek-harness",
                        "id": "assistant-agent",
                        "available": True,
                    },
                ]
            }

        async def show_binding(self, session_id, *, device_id):
            assert (session_id, device_id) == ("voice-session", "esp32-s3")
            return {
                "bound": True,
                "host": "deepseek-harness",
                "profile": "assistant-agent",
                "stored_session_id": "session-1",
            }

    module._adapter = FakeAdapter()
    conn = SimpleNamespace(session_id="voice-session", device_id="esp32-s3")
    listed = asyncio.run(module.agent_list_agents(conn))

    assert listed.action == "response"
    assert "hermes：default" in listed.response
    assert "deepseek-harness：standard（不可用）、assistant-agent（当前绑定）" in listed.response
    assert "当前绑定：deepseek-harness/assistant-agent" in listed.response

    class BindingFailureAdapter(FakeAdapter):
        async def show_binding(self, session_id, *, device_id):
            raise module.XiaozhiAdapterError("binding store unavailable")

    module._adapter = BindingFailureAdapter()
    degraded = asyncio.run(module.agent_list_agents(conn))
    payload = json.loads(degraded.result)
    assert payload["binding"] == {"bound": None, "status": "unavailable"}
    assert "当前绑定状态暂时不可读取" in degraded.response


def test_gateway_adapter_remote_mode_reads_only_named_environment_secret(monkeypatch) -> None:
    core = ModuleType("core")
    providers = ModuleType("core.providers")
    llm = ModuleType("core.providers.llm")
    base = ModuleType("core.providers.llm.base")
    base.LLMProviderBase = object
    for name, module in (
        ("core", core),
        ("core.providers", providers),
        ("core.providers.llm", llm),
        ("core.providers.llm.base", base),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("test_remote_gateway_adapter", OLLAMA_ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(module, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: kwargs)
    monkeypatch.setenv("TEST_DEEPSEEK_KEY", "secret-never-written")
    provider = module.LLMProvider(
        {
            "mode": "remote",
            "model_name": "deepseek-flash",
            "base_url": "https://api.deepseek.com",
            "api_key_env": "TEST_DEEPSEEK_KEY",
        }
    )

    assert provider.mode == "remote"
    assert provider.base_url == "https://api.deepseek.com"
    assert provider._extra_body == {"thinking": {"type": "disabled"}}
    assert captured["api_key"] == "secret-never-written"
    assert captured["http_client"] == {"trust_env": True}
    assert provider._model_dialogue([{"role": "user", "content": "你好"}]) == [
        {"role": "user", "content": "你好"}
    ]

    monkeypatch.delenv("TEST_DEEPSEEK_KEY")
    with pytest.raises(ValueError, match="TEST_DEEPSEEK_KEY"):
        module.LLMProvider(
            {
                "mode": "remote",
                "model_name": "deepseek-flash",
                "base_url": "https://api.deepseek.com",
                "api_key_env": "TEST_DEEPSEEK_KEY",
            }
        )


def test_agent_hot_switch_requires_host_and_session_disambiguation(monkeypatch) -> None:
    plugins_func = ModuleType("plugins_func")
    register = ModuleType("plugins_func.register")

    class Action:
        ERROR = "error"
        RESPONSE = "response"

    class ActionResponse:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ToolType:
        SYSTEM_CTL = "system_ctl"

    def register_function(*_args, **_kwargs):
        return lambda function: function

    register.Action = Action
    register.ActionResponse = ActionResponse
    register.ToolType = ToolType
    register.register_function = register_function
    monkeypatch.setitem(sys.modules, "plugins_func", plugins_func)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register)

    spec = importlib.util.spec_from_file_location("test_switch_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # This fixture intentionally exposes the same synthetic name on both hosts.
    monkeypatch.setattr(module, "_PROFILES", [*module._PROFILES, "game-assistant"])
    monkeypatch.setattr(module, "_DSH_PRESETS", [*module._DSH_PRESETS, "game-assistant"])
    clock = {"now": 100.0}
    monkeypatch.setattr(module, "monotonic", lambda: clock["now"])

    calls = []

    class FakeAdapter:
        async def cancel_prepared_for_device(self, *, device_id):
            calls.append(("cancel-device", device_id))
            return {"cancelled_count": 1}

        async def list_sessions(self, session_id, *, host, profile, limit):
            calls.append(("list", session_id, host, profile, limit))
            return {
                "sessions": [
                    {"id": "session-latest", "title": "三兵最近对话"},
                    {"id": "session-older", "title": "三兵旧对话"},
                ]
            }

        async def show_binding(self, session_id, *, device_id):
            return {
                "status": "ok",
                "bound": True,
                "host": "hermes",
                "profile": "default",
                "stored_session_id": "old-session",
            }

        async def bind_session(
            self, session_id, *, device_id, host, profile, stored_session_id
        ):
            calls.append(
                ("bind", session_id, device_id, host, profile, stored_session_id)
            )
            return {
                "status": "ok",
                "host": host,
                "profile": profile,
                "stored_session_id": stored_session_id,
                "title": (
                    "三兵旧对话"
                    if stored_session_id == "session-older"
                    else "三兵最近对话"
                ),
            }

    module._adapter = FakeAdapter()
    conn = SimpleNamespace(session_id="voice-session", device_id="esp32-s3")
    ambiguous = asyncio.run(
        module.agent_switch_agent(
            conn, "game-assistant"
        )
    )
    assert ambiguous.action == "response"
    assert "切换尚未完成" in ambiguous.response
    assert "已取消原待确认操作" in ambiguous.response
    assert calls == [("cancel-device", "esp32-s3")]

    for blocked in (
        module.agent_dispatch_bound(conn, "不得提交到旧绑定"),
        module.agent_prepare_dispatch(conn, "不得提交到旧绑定"),
        module.agent_prepare_new_session(conn),
        module.agent_prepare_compress_context(conn),
        module.agent_confirm_dispatch(conn),
        module.agent_prepare_dispatch(
            SimpleNamespace(session_id="other-session", device_id="esp32-s3"),
            "另一连接也不得提交",
        ),
    ):
        guarded = asyncio.run(blocked)
        assert guarded.action == "response"
        assert "切换尚未完成" in guarded.response
    assert calls == [("cancel-device", "esp32-s3")]

    other_connection = asyncio.run(
        module.agent_continue_switch(
            SimpleNamespace(session_id="new-voice-session", device_id="esp32-s3"),
            "第二项",
        )
    )
    assert "请选择已有对话" in other_connection.response
    active = module._active_switch("esp32-s3")
    assert active["deadline"] == 280.0
    assert calls == [
        ("cancel-device", "esp32-s3"),
        (
            "list",
            "new-voice-session",
            "deepseek-harness",
            "game-assistant",
            5,
        ),
    ]

    clock["now"] = 280.0

    async def choose_twice():
        return await asyncio.gather(
            module.agent_continue_switch(conn, "第二个"),
            module.agent_continue_switch(conn, "第二个"),
        )

    responses = asyncio.run(choose_twice())
    assert all(response.action == "response" for response in responses)
    assert sum("deepseek-harness" in response.response for response in responses) == 1
    assert sum("三兵旧对话" in response.response for response in responses) == 1
    assert sum(
        "已被处理" in response.response or "没有等待选择" in response.response
        for response in responses
    ) == 1
    assert calls == [
        ("cancel-device", "esp32-s3"),
        (
            "list",
            "new-voice-session",
            "deepseek-harness",
            "game-assistant",
            5,
        ),
        (
            "bind",
            "voice-session",
            "esp32-s3",
            "deepseek-harness",
            "game-assistant",
            "session-older",
        ),
    ]

    clock["now"] = 1000.0
    asyncio.run(module.agent_switch_agent(conn, "game-assistant"))
    calls_before_expiry = list(calls)
    clock["now"] = 1180.001
    expired = asyncio.run(module.agent_continue_switch(conn, "DeepSeek"))
    assert "没有等待选择" in expired.response
    assert calls == calls_before_expiry

    race_calls = []

    class RacingAdapter:
        def __init__(self):
            self.stale_list_started = asyncio.Event()
            self.release_stale_list = asyncio.Event()

        async def cancel_prepared_for_device(self, *, device_id):
            race_calls.append(("cancel-device", device_id))
            return {"cancelled_count": 0}

        async def list_sessions(self, session_id, *, host, profile, limit):
            race_calls.append(("list", profile))
            if profile == "game-assistant":
                self.stale_list_started.set()
                await self.release_stale_list.wait()
                return {"sessions": [{"id": "stale-session", "title": "旧请求"}]}
            return {"sessions": [{"id": "new-session", "title": "新请求"}]}

        async def show_binding(self, session_id, *, device_id):
            return {
                "status": "ok",
                "bound": True,
                "host": "hermes",
                "profile": "default",
                "stored_session_id": "old-session",
            }

        async def bind_session(
            self, session_id, *, device_id, host, profile, stored_session_id
        ):
            race_calls.append(("bind", profile, stored_session_id))
            return {
                "status": "ok",
                "host": host,
                "profile": profile,
                "stored_session_id": stored_session_id,
                "title": "新请求",
            }

    async def replace_resolving_switch():
        adapter = RacingAdapter()
        module._adapter = adapter
        stale_task = asyncio.create_task(
            module.agent_switch_agent(
                conn, "game-assistant", host="deepseek-harness"
            )
        )
        await adapter.stale_list_started.wait()
        replacement = await module.agent_switch_agent(
            conn, "conversation-agent", host="deepseek-harness"
        )
        adapter.release_stale_list.set()
        stale = await stale_task
        return replacement, stale

    replacement, stale = asyncio.run(replace_resolving_switch())
    assert "请选择已有对话" in replacement.response
    assert "已失效" in stale.response
    assert race_calls == [
        ("cancel-device", "esp32-s3"),
        ("list", "game-assistant"),
        ("cancel-device", "esp32-s3"),
        ("list", "conversation-agent"),
    ]

    selected = asyncio.run(module.agent_continue_switch(conn, "第一个"))
    assert "已切换到 deepseek-harness 的 conversation-agent" in selected.response
    assert race_calls == [
        ("cancel-device", "esp32-s3"),
        ("list", "game-assistant"),
        ("cancel-device", "esp32-s3"),
        ("list", "conversation-agent"),
        ("bind", "conversation-agent", "new-session"),
    ]


def test_exit_tombstones_inflight_hot_switch_and_restores_binding(monkeypatch) -> None:
    plugins_func = ModuleType("plugins_func")
    register = ModuleType("plugins_func.register")

    class Action:
        NONE = "none"
        ERROR = "error"
        RESPONSE = "response"

    class ActionResponse:
        def __init__(self, **values):
            self.__dict__.update(values)

    class ToolType:
        SYSTEM_CTL = "system_ctl"

    def register_function(*_args, **_kwargs):
        return lambda function: function

    register.Action = Action
    register.ActionResponse = ActionResponse
    register.ToolType = ToolType
    register.register_function = register_function
    monkeypatch.setitem(sys.modules, "plugins_func", plugins_func)
    monkeypatch.setitem(sys.modules, "plugins_func.register", register)

    spec = importlib.util.spec_from_file_location("test_switch_exit_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class FakeWebSocket:
        async def send(self, _payload):
            return None

    class FakeConnection:
        session_id = "voice-session"
        device_id = "esp32-s3"
        close_after_chat = True
        client_abort = False

        def __init__(self):
            self.websocket = FakeWebSocket()
            self.closed = 0

        def clear_queues(self):
            return None

        def clearSpeakStatus(self):
            return None

        async def close(self):
            self.closed += 1

    class RacingAdapter:
        def __init__(self):
            self.bind_started = asyncio.Event()
            self.release_bind = asyncio.Event()
            self.bind_calls = []

        async def show_binding(self, session_id, *, device_id):
            return {
                "status": "ok",
                "bound": True,
                "host": "hermes",
                "profile": "default",
                "stored_session_id": "old-session",
            }

        async def bind_session(
            self, session_id, *, device_id, host, profile, stored_session_id
        ):
            self.bind_calls.append((host, profile, stored_session_id))
            if stored_session_id == "new-session":
                self.bind_started.set()
                await self.release_bind.wait()
            return {"status": "ok", "stored_session_id": stored_session_id}

        async def cancel_prepared_for_device(self, *, device_id):
            return {"status": "ok", "cancelled_count": 0}

    async def run_race():
        adapter = RacingAdapter()
        module._adapter = adapter
        conn = FakeConnection()
        module._start_switch(
            conn.device_id,
            conn.session_id,
            {
                "stage": "session",
                "host": "deepseek-harness",
                "agent_id": "conversation-agent",
                "sessions": [{"id": "new-session", "title": "新对话"}],
            },
        )
        switch_task = asyncio.create_task(module.agent_continue_switch(conn, "第一个"))
        await adapter.bind_started.wait()
        exit_task = asyncio.create_task(module.lingxi_exit_conversation(conn))
        await asyncio.sleep(0)
        assert not exit_task.done()
        adapter.release_bind.set()
        switched, exited = await asyncio.gather(switch_task, exit_task)
        return adapter, conn, switched, exited

    adapter, conn, switched, exited = asyncio.run(run_race())
    assert switched.action == "none"
    assert exited.action == "none"
    assert adapter.bind_calls == [
        ("deepseek-harness", "conversation-agent", "new-session"),
        ("hermes", "default", "old-session"),
    ]
    assert module._active_switch("esp32-s3") is None
    assert conn.closed == 1
