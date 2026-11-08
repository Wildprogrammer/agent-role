from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import shutil
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.control_events import ControlEventStore
from agent_workflow_hub.xiaozhi_hermes_bridge.control_http import (
    ControlHttpServers,
    LingxiRequestHandler,
    ScopedAuth,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.control_plane import (
    ControlPlaneError,
    DeviceControlCoordinator,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.inprocess_control import (
    InProcessDeviceControlBackend,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.live_companion import (
    ANSWER_HEAD_LIMIT,
    ANSWER_LIMIT,
    QUESTION_LIMIT,
    SUBSCRIPTION_TTL_SECONDS,
    TransientCompanionBroker,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.runtime_settings import (
    RuntimeSettingsError,
    RuntimeSettingsManager,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.control_runtime import (
    LingxiControlRuntime,
    resolve_control_passwords,
)
from agent_workflow_hub.xiaozhi_hermes_bridge import control_runtime as control_runtime_module


RUNTIME_YAML = b"""# preserve this operator comment
selected_module:
  VAD: SileroVAD
  ASR: FunASR
  LLM: BoundAgentGatewayLLM
  TTS: EdgeTTS
tool_call_timeout: 300
LLM:
  BoundAgentGatewayLLM: {}
  DeepSeekGatewayLLM: {}
  OllamaLLM: {}
TTS:
  EdgeTTS:
    voice: zh-CN-XiaoyiNeural
  WindowsSapiTTS:
    voice: Huihui
ASR:
  FunASR: {}
VAD:
  SileroVAD:
    min_silence_duration_ms: 800
unrelated:
  keep: exactly
"""


def test_windows_client_abort_is_not_reported_as_a_control_failure(monkeypatch) -> None:
    handler = object.__new__(LingxiRequestHandler)

    def aborted_write(*_args, **_kwargs):
        raise ConnectionAbortedError(10053, "client closed the socket")

    monkeypatch.setattr(handler, "_json", aborted_write)
    handler._error(
        ControlPlaneError(
            "authentication_required",
            "browser session is not authenticated",
            http_status=401,
        )
    )
    handler._error(ConnectionAbortedError(10053, "client closed the socket"))


def _settings(tmp_path: Path) -> RuntimeSettingsManager:
    config = (tmp_path / "runtime.yaml").resolve()
    config.write_bytes(RUNTIME_YAML)
    return RuntimeSettingsManager(config, backup_dir=(tmp_path / "backups").resolve())


def test_control_events_are_bounded_versioned_and_transcript_free(tmp_path: Path) -> None:
    store = ControlEventStore((tmp_path / "events.json").resolve(), runtime_epoch="epoch-a")
    for index in range(140):
        store.publish(state="thinking", device_id="esp32-s3", operation=f"op-{index}")

    value = store.snapshot(after_event_id=135)

    assert value["last_event_id"] == 140
    assert value["state_version"] == 140
    assert [item["event_id"] for item in value["events"]] == [136, 137, 138, 139, 140]
    encoded = store.path.read_text(encoding="utf-8")
    assert "transcript" not in encoded
    assert "prompt" not in encoded
    assert len(json.loads(encoded)["events"]) == 128


def test_live_companion_is_bounded_generation_safe_and_non_replayable() -> None:
    now = [1000.0]
    broker = TransientCompanionBroker(clock=lambda: now[0])
    broker.register_connection(device_id="esp32-s3", connection_generation="generation-1")
    old_turn = broker.start_turn(
        device_id="esp32-s3",
        connection_generation="generation-1",
        capture_active=False,
    )
    assert old_turn
    assert broker.publish_question(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=old_turn,
        text="old private question",
    )
    subscription = broker.subscribe(
        session_token="browser-a",
        device_id="esp32-s3",
        connection_generation="generation-1",
    )
    assert broker.publish_answer(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=old_turn,
        text="old private answer",
    )
    now[0] += 0.3
    hidden = broker.poll(
        session_token="browser-a",
        subscription_id=subscription["subscription"],
        after_sequence=subscription["cursor"],
    )
    assert hidden["projection"]["question"] == ""
    assert hidden["projection"]["answer"] == ""

    turn = broker.start_turn(
        device_id="esp32-s3",
        connection_generation="generation-1",
        capture_active=True,
    )
    assert turn and turn != old_turn
    assert broker.publish_question(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=turn,
        text="问" * (QUESTION_LIMIT + 20),
    )
    answer_text = (
        "开" * ANSWER_HEAD_LIMIT
        + "中" * 20
        + "尾" * (ANSWER_LIMIT - ANSWER_HEAD_LIMIT)
    )
    assert broker.publish_answer(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=turn,
        text=answer_text,
    )
    now[0] += 0.3
    visible = broker.poll(
        session_token="browser-a",
        subscription_id=subscription["subscription"],
        after_sequence=hidden["cursor"],
    )
    projection = visible["projection"]
    assert len(projection["question"]) == QUESTION_LIMIT
    assert len(projection["answer"]) == ANSWER_LIMIT
    assert projection["question_truncated"] is True
    assert projection["answer_truncated"] is True
    assert projection["answer"] == (
        answer_text[:ANSWER_HEAD_LIMIT]
        + answer_text[-(ANSWER_LIMIT - ANSWER_HEAD_LIMIT) :]
    )
    assert projection["answer_head_length"] == ANSWER_HEAD_LIMIT
    assert projection["answer_omitted_characters"] == 20
    line_break_turn = broker.start_turn(
        device_id="esp32-s3",
        connection_generation="generation-1",
        capture_active=False,
    )
    assert line_break_turn
    assert broker.publish_answer(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=line_break_turn,
        text="路径 C:\\work\\agent\n链接 https://example.test/item\n**原始格式**",
    )
    now[0] += 0.3
    verbatim = broker.poll(
        session_token="browser-a",
        subscription_id=subscription["subscription"],
        after_sequence=visible["cursor"],
    )
    assert verbatim["projection"]["answer"] == (
        "路径 C:\\work\\agent\n链接 https://example.test/item\n**原始格式**"
    )
    assert broker.publish_answer(
        device_id="esp32-s3",
        connection_generation="stale-generation",
        turn_id=turn,
        text="must be rejected",
    ) is False

    broker.register_connection(device_id="esp32-s3", connection_generation="generation-2")
    now[0] += 0.3
    with pytest.raises(ControlPlaneError) as rejected:
        broker.poll(
            session_token="browser-a",
            subscription_id=subscription["subscription"],
            after_sequence=visible["cursor"],
        )
    assert rejected.value.code == "subscription_unavailable"


def test_live_companion_resumes_same_page_without_replaying_to_new_page() -> None:
    now = [1000.0]
    broker = TransientCompanionBroker(clock=lambda: now[0])
    broker.register_connection(device_id="esp32-s3", connection_generation="generation-1")
    subscription = broker.subscribe(
        session_token="browser-a",
        device_id="esp32-s3",
        connection_generation="generation-1",
    )
    turn = broker.start_turn(
        device_id="esp32-s3",
        connection_generation="generation-1",
        capture_active=True,
    )
    assert turn
    assert broker.publish_question(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=turn,
        text="本页已经看到的问题",
    )
    now[0] += 0.3
    first = broker.poll(
        session_token="browser-a",
        subscription_id=subscription["subscription"],
        after_sequence=subscription["cursor"],
    )
    assert first["projection"]["question"] == "本页已经看到的问题"

    now[0] += SUBSCRIPTION_TTL_SECONDS + 0.1
    assert broker.publish_answer(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=turn,
        text="订阅暂停期间到达的答案",
    )
    with pytest.raises(ControlPlaneError) as expired:
        broker.poll(
            session_token="browser-a",
            subscription_id=subscription["subscription"],
            after_sequence=first["cursor"],
        )
    assert expired.value.code == "subscription_unavailable"

    resumed = broker.subscribe(
        session_token="browser-a",
        device_id="esp32-s3",
        connection_generation="generation-1",
        resume_token=subscription["resume_token"],
        after_sequence=first["cursor"],
    )
    assert resumed["resumed"] is True
    now[0] += 0.3
    recovered = broker.poll(
        session_token="browser-a",
        subscription_id=resumed["subscription"],
        after_sequence=resumed["cursor"],
    )
    assert recovered["projection"]["turn_id"] == turn
    assert recovered["projection"]["answer"] == "订阅暂停期间到达的答案"

    new_page = broker.subscribe(
        session_token="browser-a",
        device_id="esp32-s3",
        connection_generation="generation-1",
    )
    assert broker.publish_answer(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=turn,
        text="仍然属于旧回合的后续内容",
    )
    now[0] += 0.3
    hidden = broker.poll(
        session_token="browser-a",
        subscription_id=new_page["subscription"],
        after_sequence=new_page["cursor"],
    )
    assert hidden["projection"]["turn_id"] == ""
    assert hidden["projection"]["answer"] == ""


def test_live_companion_resume_token_is_context_bound_and_revocable() -> None:
    broker = TransientCompanionBroker()
    broker.register_connection(device_id="esp32-s3", connection_generation="generation-1")
    subscription = broker.subscribe(
        session_token="browser-a",
        device_id="esp32-s3",
        connection_generation="generation-1",
    )
    with pytest.raises(ControlPlaneError) as wrong_session:
        broker.subscribe(
            session_token="browser-b",
            device_id="esp32-s3",
            connection_generation="generation-1",
            resume_token=subscription["resume_token"],
            after_sequence=subscription["cursor"],
        )
    assert wrong_session.value.code == "resume_unavailable"

    broker.unsubscribe(
        session_token="browser-a",
        subscription_id=subscription["subscription"],
        resume_token=subscription["resume_token"],
    )
    with pytest.raises(ControlPlaneError) as revoked:
        broker.subscribe(
            session_token="browser-a",
            device_id="esp32-s3",
            connection_generation="generation-1",
            resume_token=subscription["resume_token"],
            after_sequence=subscription["cursor"],
        )
    assert revoked.value.code == "resume_unavailable"


def test_live_companion_keeps_turn_tombstone_for_late_result() -> None:
    now = [1000.0]
    broker = TransientCompanionBroker(clock=lambda: now[0])
    broker.register_connection(device_id="esp32-s3", connection_generation="generation-1")
    subscription = broker.subscribe(
        session_token="browser-a",
        device_id="esp32-s3",
        connection_generation="generation-1",
    )
    turn = broker.start_turn(
        device_id="esp32-s3",
        connection_generation="generation-1",
        capture_active=True,
    )
    assert turn
    cursor = subscription["cursor"]
    for offset in (29.0, 58.0, 87.0):
        now[0] = 1000.0 + offset
        heartbeat = broker.poll(
            session_token="browser-a",
            subscription_id=subscription["subscription"],
            after_sequence=cursor,
        )
        cursor = heartbeat["cursor"]
    now[0] = 1091.0
    assert broker.publish_question(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=turn,
        text="迟到但仍属于同一回合的问题",
    )
    now[0] += 0.3
    projection = broker.poll(
        session_token="browser-a",
        subscription_id=subscription["subscription"],
        after_sequence=cursor,
    )
    assert projection["projection"]["turn_id"] == turn
    assert projection["projection"]["question"] == "迟到但仍属于同一回合的问题"


def test_live_companion_clears_completed_text_sixty_seconds_after_tts_stop() -> None:
    now = [1000.0]
    broker = TransientCompanionBroker(clock=lambda: now[0])
    broker.register_connection(device_id="esp32-s3", connection_generation="generation-1")
    subscription = broker.subscribe(
        session_token="browser-a",
        device_id="esp32-s3",
        connection_generation="generation-1",
    )
    turn = broker.start_turn(
        device_id="esp32-s3",
        connection_generation="generation-1",
        capture_active=False,
    )
    assert turn
    assert broker.publish_answer(
        device_id="esp32-s3",
        connection_generation="generation-1",
        turn_id=turn,
        text="播报结束后临时保留的正文",
    )
    now[0] += 0.3
    shown = broker.poll(
        session_token="browser-a",
        subscription_id=subscription["subscription"],
        after_sequence=subscription["cursor"],
    )
    assert shown["projection"]["answer"] == "播报结束后临时保留的正文"
    assert broker.set_speech(
        device_id="esp32-s3",
        connection_generation="generation-1",
        active=True,
    )
    assert broker.set_speech(
        device_id="esp32-s3",
        connection_generation="generation-1",
        active=False,
    )
    cursor = shown["cursor"]
    stopped_at = now[0]
    for offset in (29.0, 58.0):
        now[0] = stopped_at + offset
        heartbeat = broker.poll(
            session_token="browser-a",
            subscription_id=subscription["subscription"],
            after_sequence=cursor,
        )
        cursor = heartbeat["cursor"]
    now[0] = stopped_at + 60.1
    cleared = broker.poll(
        session_token="browser-a",
        subscription_id=subscription["subscription"],
        after_sequence=cursor,
    )
    assert cleared["projection"]["turn_id"] == turn
    assert cleared["projection"]["answer"] == ""
    assert cleared["projection"]["speech_active"] is False


def test_runtime_settings_preview_apply_and_rollback_preserve_unrelated_yaml(
    tmp_path: Path,
) -> None:
    manager = _settings(tmp_path)
    before = manager.path.read_bytes()
    initial = manager.snapshot()
    plan = manager.preview(
        {
            "llm_mode": "remote",
            "tts_voice": "zh-CN-XiaoxiaoNeural",
            "vad_min_silence_ms": 1000,
        },
        expected_configured_sha256=initial["configured_sha256"],
    )

    assert manager.path.read_bytes() == before
    applied = manager.apply(
        plan_id=plan["plan_id"], candidate_sha256=plan["candidate_sha256"]
    )
    changed = manager.path.read_text(encoding="utf-8")
    assert "# preserve this operator comment" in changed
    assert "keep: exactly" in changed
    assert 'LLM: "DeepSeekGatewayLLM"' in changed
    assert 'voice: "zh-CN-XiaoxiaoNeural"' in changed
    assert "min_silence_duration_ms: 1000" in changed
    assert applied["pending_restart"] is True

    rolled_back = manager.rollback(
        backup_id=applied["backup_id"],
        expected_configured_sha256=applied["configured_sha256"],
    )
    assert manager.path.read_bytes() == before
    assert rolled_back["configured_sha256"] == hashlib.sha256(before).hexdigest()


def test_runtime_settings_reject_unknown_patch_and_stale_plan(tmp_path: Path) -> None:
    manager = _settings(tmp_path)
    digest = manager.snapshot()["configured_sha256"]
    with pytest.raises(RuntimeSettingsError, match="unknown fields"):
        manager.preview({"api_key": "secret"}, expected_configured_sha256=digest)

    plan = manager.preview(
        {"tool_call_timeout_seconds": 301},
        expected_configured_sha256=digest,
    )
    manager.path.write_bytes(RUNTIME_YAML + b"# external edit\n")
    with pytest.raises(RuntimeSettingsError, match="changed after preview"):
        manager.apply(
            plan_id=plan["plan_id"], candidate_sha256=plan["candidate_sha256"]
        )


class FakeAdapter:
    def __init__(self) -> None:
        self.binding: dict[str, object] = {"status": "ok", "bound": False}
        self.pending: dict[str, object] = {"status": "ok", "pending": False}
        self.confirmed: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    async def show_binding(self, _session_id: str, *, device_id: str):
        return {"device_id": device_id, **self.binding}

    async def pending_for_device(self, *, device_id: str):
        return {"device_id": device_id, **self.pending}

    async def list_agents(self, _session_id: str):
        return {
            "agents": [
                {"host": "hermes", "id": "default", "name": "Default", "available": True},
                {"host": "hermes", "id": "bad<script>", "name": "unsafe"},
            ]
        }

    async def list_sessions(self, _session_id: str, *, host: str, profile: str, limit: int):
        return {"sessions": [{"id": "chat-1", "title": "<img src=x onerror=alert(1)>", "cwd": "C:/secret", "preview": "secret", "message_count": 4}]}

    async def bind_session(self, _session_id: str, **values: object):
        self.binding = {
            "status": "ok",
            "bound": True,
            "device_id": values["device_id"],
            "host": values["host"],
            "profile": values["profile"],
            "stored_session_id": values["stored_session_id"],
            "title": "Safe",
            "binding_revision": values["expected_binding_revision"] + 1,
        }
        return self.binding

    async def unbind_session(self, _session_id: str, **_values: object):
        self.binding = {"status": "ok", "bound": False}
        return {"status": "ok", "removed": True}

    async def confirm_shared_dispatch(self, session_id: str, *, device_id: str):
        self.confirmed.append((session_id, device_id))
        self.pending = {"status": "ok", "pending": False}
        return {"status": "completed", "operation": "create_session"}

    async def cancel_dispatch(self, session_id: str):
        self.cancelled.append(session_id)
        self.pending = {"status": "ok", "pending": False}
        return {"status": "ok", "operation": "cancel_dispatch"}


def test_inprocess_backend_projects_data_and_consumes_exact_action() -> None:
    async def exercise() -> None:
        adapter = FakeAdapter()
        backend = InProcessDeviceControlBackend(adapter)  # type: ignore[arg-type]
        stopped: list[bool] = []
        volumes: list[int] = []

        async def stop():
            stopped.append(True)
            return {"status": "ok"}

        async def volume(value: int):
            volumes.append(value)
            return {"status": "ok"}

        async def inspect_device(operation: str):
            if operation == "device_status":
                return {
                    "audio_speaker": {"volume": 70},
                    "network": {"type": "wifi", "ssid": "private", "signal": "strong"},
                }
            return {
                "input_enabled": True,
                "pcm_seen": True,
                "input_sample_rate": 16000,
                "last_peak": 120,
            }

        await backend.register_connection(
            device_id="esp32-s3",
            session_id="voice-1",
            connection_generation="generation-1",
            stop=stop,
            set_volume=volume,
            inspect=inspect_device,
            tool_names=(
                "self_get_device_status",
                "self_audio_get_diagnostics",
                "self_audio_speaker_set_volume_verified",
            ),
            alive=lambda: True,
        )
        rows = await backend.list_sessions(host="hermes", agent="default", limit=20)
        assert rows[0]["title"] == "<img src=x onerror=alert(1)>"
        snapshot = await backend.snapshot()
        assert snapshot["devices"][0]["capabilities"]["set_volume"] is True  # type: ignore[index]
        assert snapshot["devices"][0]["device_status"]["network"] == {  # type: ignore[index]
            "type": "wifi",
            "signal": "strong",
        }
        bound = await backend.bind(
            device_id="esp32-s3",
            host="hermes",
            agent="default",
            stored_session_id="chat-1",
            expected_binding_revision=0,
            expected_generation="generation-1",
        )
        assert bound["binding_revision"] == 1
        adapter.pending = {
            "status": "ok",
            "pending": True,
            "pending_state": "prepared",
            "pending_operation": "create_session",
            "pending_session_id": "voice-1",
            "action_id": "action-1",
            "expires_in_seconds": 120,
        }
        with pytest.raises(ControlPlaneError, match="changed"):
            await backend.consume_action(
                    device_id="esp32-s3",
                    action_id="stale",
                    decision="confirm",
                    expected_generation="generation-1",
            )
        result = await backend.consume_action(
            device_id="esp32-s3",
            action_id="action-1",
            decision="confirm",
            expected_generation="generation-1",
        )
        assert result["status"] == "completed"
        diagnostics = await backend.audio_diagnostics(
            device_id="esp32-s3", expected_generation="generation-1"
        )
        assert diagnostics["diagnostics"]["last_peak"] == 120  # type: ignore[index]
        with pytest.raises(ControlPlaneError, match="reconnected"):
            await backend.stop_speaking(
                device_id="esp32-s3", expected_generation="stale-generation"
            )
        await backend.stop_speaking(
            device_id="esp32-s3", expected_generation="generation-1"
        )
        await backend.set_volume(
            device_id="esp32-s3", volume=42, expected_generation="generation-1"
        )
        assert stopped == [True]
        assert volumes == [42]

    asyncio.run(exercise())


class FakeControlCoordinator:
    def __init__(self) -> None:
        self.bind_calls: list[dict[str, object]] = []
        self.diagnostic_calls: list[dict[str, object]] = []
        self.stop_calls: list[dict[str, object]] = []

    def snapshot(self):
        return {"schema_version": "1.0", "runtime": {"status": "ok", "devices": []}, "settings": {}}

    def list_agents(self):
        return {"schema_version": "1.0", "agents": []}

    def list_sessions(self, **_values: object):
        return {"schema_version": "1.0", "sessions": []}

    def bind(self, **values: object):
        self.bind_calls.append(values)
        return {"schema_version": "1.0", "status": "ok"}

    def unbind(self, **_values: object):
        return {"schema_version": "1.0", "status": "ok"}

    def consume_action(self, **_values: object):
        return {"schema_version": "1.0", "status": "ok"}

    def stop_speaking(self, **_values: object):
        self.stop_calls.append(_values)
        return {"schema_version": "1.0", "status": "ok"}

    def set_volume(self, **_values: object):
        return {"schema_version": "1.0", "status": "ok"}

    def audio_diagnostics(self, **values: object):
        self.diagnostic_calls.append(values)
        return {"schema_version": "1.0", "status": "ok", "diagnostics": {}}

    def events(self, **_values: object):
        return {"schema_version": "1.0", "events": []}

    def config_preview(self, *_args: object, **_values: object):
        return {"status": "ok"}

    def config_apply(self, **_values: object):
        return {"status": "ok"}

    def config_rollback(self, **_values: object):
        return {"status": "ok"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_control_runtime_serves_login_before_owner_loop_attaches(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_config = (tmp_path / "runtime.yaml").resolve()
    runtime_config.write_bytes(RUNTIME_YAML)
    monkeypatch.setattr(
        control_runtime_module,
        "default_control_event_store_path",
        lambda: (tmp_path / "events.json").resolve(),
    )
    admin_port, companion_port = _free_port(), _free_port()
    while companion_port == admin_port:
        companion_port = _free_port()
    runtime = LingxiControlRuntime(
        FakeAdapter(),  # type: ignore[arg-type]
        runtime_config=runtime_config,
        backup_dir=(tmp_path / "backups").resolve(),
        admin_port=admin_port,
        companion_port=companion_port,
        admin_password="admin",
        companion_password="admin",
    )
    try:
        status, _, _ = _request(admin_port, "GET", "/")
        assert status == 200
        cookie, csrf = _login(admin_port, "admin")
        assert cookie.startswith("lingxi_admin_session=")
        assert csrf
    finally:
        runtime.servers.close()


def _request(
    port: int,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    cookie: str = "",
    csrf: str = "",
    origin: str | None = None,
    host: str | None = None,
):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-Lingxi-CSRF"] = csrf
    if origin is not None:
        headers["Origin"] = origin
    connection.request(
        method,
        path,
        body=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers,
    )
    response = connection.getresponse()
    payload = response.read()
    result = (
        json.loads(payload.decode("utf-8"))
        if response.getheader("Content-Type", "").startswith("application/json")
        else payload
    )
    headers_out = dict(response.getheaders())
    status = response.status
    connection.close()
    return status, headers_out, result


def _login(port: int, password: str):
    status, headers, value = _request(
        port,
        "POST",
        "/api/login",
        body={"password": password},
        origin=f"http://127.0.0.1:{port}",
    )
    assert status == 200
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    return cookie, value["csrf"]


def test_repeatable_password_auth_is_bounded_rate_limited_and_unicode_safe() -> None:
    auth = ScopedAuth("admin", "灵溪 密码")

    first = auth.login("灵溪 密码")
    second = auth.login("灵溪 密码")

    assert first.token != second.token
    assert first.csrf != second.csrf
    for _ in range(20):
        auth.login("灵溪 密码")
    assert len(auth._sessions) <= 16

    limited = ScopedAuth("admin", "correct")
    for _ in range(5):
        with pytest.raises(ControlPlaneError, match="password is incorrect"):
            limited.login("wrong")
    with pytest.raises(ControlPlaneError) as raised:
        limited.login("correct")
    assert raised.value.code == "login_rate_limited"
    assert raised.value.http_status == 429


@pytest.mark.parametrize("password", ["", "   ", "x" * 257])
def test_scoped_auth_rejects_invalid_configured_password(password: str) -> None:
    with pytest.raises(ControlPlaneError, match="configured control password"):
        ScopedAuth("admin", password)


def test_control_password_configuration_precedence_and_validation() -> None:
    assert resolve_control_passwords({}) == ("admin", "admin")
    assert resolve_control_passwords({"LINGXI_CONTROL_PASSWORD": "shared"}) == (
        "shared",
        "shared",
    )
    assert resolve_control_passwords(
        {
            "LINGXI_CONTROL_PASSWORD": "shared",
            "LINGXI_CONTROL_ADMIN_PASSWORD": "admin-only",
            "LINGXI_CONTROL_COMPANION_PASSWORD": "伴随屏",
        }
    ) == ("admin-only", "伴随屏")
    with pytest.raises(ControlPlaneError, match="must not be empty"):
        resolve_control_passwords({"LINGXI_CONTROL_PASSWORD": ""})
    with pytest.raises(ControlPlaneError, match="at most 256"):
        resolve_control_passwords({"LINGXI_CONTROL_ADMIN_PASSWORD": "x" * 257})


def test_http_surfaces_enforce_auth_csrf_origin_host_and_fixed_scope(tmp_path: Path) -> None:
    admin_port, companion_port = _free_port(), _free_port()
    while companion_port == admin_port:
        companion_port = _free_port()
    coordinator = FakeControlCoordinator()
    static_root = Path(__file__).parents[3] / "src" / "agent_workflow_hub" / "xiaozhi_hermes_bridge" / "static"
    servers = ControlHttpServers(
        coordinator,  # type: ignore[arg-type]
        admin_port=admin_port,
        companion_port=companion_port,
        static_root=static_root.resolve(),
        admin_password="admin-secret",
        companion_password="companion-secret",
    )
    servers.start()
    try:
        status, headers, _ = _request(admin_port, "GET", "/api/snapshot")
        assert status == 401
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

        status, _, _ = _request(
            admin_port,
            "POST",
            "/api/login",
            body={"password": "admin-secret"},
            origin="http://evil.example",
        )
        assert status == 403

        status, headers, value = _request(
            admin_port,
            "POST",
            "/api/login",
            body={"password": "wrong"},
            origin=f"http://127.0.0.1:{admin_port}",
        )
        assert status == 403
        assert "Set-Cookie" not in headers
        assert value["error"]["code"] == "login_rejected"

        admin_cookie, admin_csrf = _login(admin_port, "admin-secret")
        second_admin_cookie, _ = _login(admin_port, "admin-secret")
        assert second_admin_cookie != admin_cookie

        status, _, value = _request(
            companion_port,
            "GET",
            "/api/snapshot",
            cookie=admin_cookie,
        )
        assert status == 401
        assert value["error"]["code"] == "authentication_required"

        status, _, value = _request(
            admin_port,
            "POST",
            "/api/login",
            body={"password": "admin-secret", "scope": "companion"},
            origin=f"http://127.0.0.1:{admin_port}",
        )
        assert status == 400
        assert value["error"]["code"] == "invalid_request"

        status, _, value = _request(
            companion_port,
            "POST",
            "/api/login",
            body={"password": "admin-secret"},
            origin=f"http://127.0.0.1:{companion_port}",
        )
        assert status == 403
        assert value["error"]["code"] == "login_rejected"

        status, _, value = _request(
            admin_port,
            "POST",
            "/api/bootstrap",
            body={"code": "legacy-code"},
            origin=f"http://127.0.0.1:{admin_port}",
        )
        assert status == 401
        assert value["error"]["code"] == "authentication_required"
        bind_body = {
            "device_id": "esp32-s3",
            "host": "hermes",
            "agent": "default",
            "stored_session_id": "chat-1",
            "expected_binding_revision": 0,
            "expected_generation": "generation-1",
        }
        status, _, _ = _request(
            admin_port,
            "POST",
            "/api/bind",
            body=bind_body,
            cookie=admin_cookie,
            origin=f"http://127.0.0.1:{admin_port}",
        )
        assert status == 403
        status, _, _ = _request(
            admin_port,
            "POST",
            "/api/bind",
            body=bind_body,
            cookie=admin_cookie,
            csrf=admin_csrf,
            origin=f"http://127.0.0.1:{admin_port}",
        )
        assert status == 200
        assert coordinator.bind_calls == [bind_body]

        status, _, _ = _request(
            admin_port,
            "POST",
            "/api/diagnostics",
            body={"device_id": "esp32-s3", "expected_generation": "generation-1"},
            cookie=admin_cookie,
            csrf=admin_csrf,
            origin=f"http://127.0.0.1:{admin_port}",
        )
        assert status == 200
        assert coordinator.diagnostic_calls == [
            {"device_id": "esp32-s3", "expected_generation": "generation-1"}
        ]

        status, _, _ = _request(
            admin_port,
            "GET",
            "/api/snapshot",
            cookie=admin_cookie,
            host="evil.example",
        )
        assert status == 403

        status, _, _ = _request(
            admin_port,
            "GET",
            "/api/sessions?host=hermes&agent",
            cookie=admin_cookie,
        )
        assert status == 400

        status, headers, _ = _request(
            admin_port,
            "OPTIONS",
            "/api/snapshot",
            origin=f"http://127.0.0.1:{admin_port}",
        )
        assert status == 405
        assert headers["X-Frame-Options"] == "DENY"
        assert "Access-Control-Allow-Origin" not in headers

        companion_cookie, companion_csrf = _login(companion_port, "companion-secret")
        status, _, value = _request(
            admin_port,
            "GET",
            "/api/snapshot",
            cookie=companion_cookie,
        )
        assert status == 401
        assert value["error"]["code"] == "authentication_required"
        status, _, _ = _request(
            companion_port, "GET", "/api/agents", cookie=companion_cookie
        )
        assert status == 404
        status, _, _ = _request(
            companion_port,
            "POST",
            "/api/bind",
            body=bind_body,
            cookie=companion_cookie,
            csrf=companion_csrf,
            origin=f"http://127.0.0.1:{companion_port}",
        )
        assert status == 404
        status, _, _ = _request(
            companion_port,
            "POST",
            "/api/diagnostics",
            body={"device_id": "esp32-s3", "expected_generation": "generation-1"},
            cookie=companion_cookie,
            csrf=companion_csrf,
            origin=f"http://127.0.0.1:{companion_port}",
        )
        assert status == 404
        status, _, _ = _request(
            companion_port,
            "POST",
            "/api/stop",
            body={"device_id": "esp32-s3", "expected_generation": "generation-1"},
            cookie=companion_cookie,
            csrf=companion_csrf,
            origin=f"http://127.0.0.1:{companion_port}",
        )
        assert status == 200
        assert coordinator.stop_calls == [
            {"device_id": "esp32-s3", "expected_generation": "generation-1"}
        ]
        status, _, _ = _request(
            admin_port,
            "POST",
            "/api/stop",
            body={"device_id": "esp32-s3"},
            cookie=admin_cookie,
            csrf=admin_csrf,
            origin=f"http://127.0.0.1:{admin_port}",
        )
        assert status == 404
    finally:
        servers.close()


def test_live_http_is_companion_scoped_session_bound_and_no_store() -> None:
    admin_port, companion_port = _free_port(), _free_port()
    while companion_port == admin_port:
        companion_port = _free_port()
    broker = TransientCompanionBroker()
    servers = ControlHttpServers(
        FakeControlCoordinator(),  # type: ignore[arg-type]
        admin_port=admin_port,
        companion_port=companion_port,
        admin_password="admin-secret",
        companion_password="companion-secret",
        live_companion=broker,
    )
    servers.start()
    try:
        admin_cookie, admin_csrf = _login(admin_port, "admin-secret")
        status, _, _ = _request(
            admin_port,
            "POST",
            "/api/live/subscribe",
            body={"device_id": "esp32-s3", "connection_generation": "generation-1"},
            cookie=admin_cookie,
            csrf=admin_csrf,
            origin=f"http://127.0.0.1:{admin_port}",
        )
        assert status == 404

        cookie, csrf = _login(companion_port, "companion-secret")
        status, _, waiting = _request(
            companion_port, "POST", "/api/live/wait", body={"previous": ""},
            cookie=cookie, csrf=csrf, origin=f"http://127.0.0.1:{companion_port}",
        )
        assert status == 200 and waiting["resume_token"]
        identity = {"device_id": "esp32-s3", "connection_generation": "generation-1"}
        broker.register_connection(**identity)
        wake_turn = broker.start_turn(**identity, capture_active=True)
        broker.publish_question(**identity, turn_id=wake_turn, text="你好灵溪")
        status, _, watch = _request(
            companion_port, "POST", "/api/live/subscribe",
            body={**identity, "resume_token": waiting["resume_token"], "after": 0},
            cookie=cookie, csrf=csrf, origin=f"http://127.0.0.1:{companion_port}",
        )
        assert status == 200 and watch["resumed"]
        status, _, wake = _request(
            companion_port, "GET",
            f"/api/live?subscription={watch['subscription']}&after={watch['cursor']}",
            cookie=cookie,
        )
        assert status == 200 and wake["projection"]["question"] == "你好灵溪"
        status, _, value = _request(
            companion_port,
            "POST",
            "/api/live/subscribe",
            body={"device_id": "esp32-s3", "connection_generation": "generation-1"},
            cookie=cookie,
            origin=f"http://127.0.0.1:{companion_port}",
        )
        assert status == 403
        status, headers, value = _request(
            companion_port,
            "POST",
            "/api/live/subscribe",
            body={"device_id": "esp32-s3", "connection_generation": "generation-1"},
            cookie=cookie,
            csrf=csrf,
            origin=f"http://127.0.0.1:{companion_port}",
        )
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        subscription = value["subscription"]
        resume_token = value["resume_token"]
        cursor = value["cursor"]

        turn = broker.start_turn(
            device_id="esp32-s3",
            connection_generation="generation-1",
            capture_active=True,
        )
        assert turn
        broker.publish_question(
            device_id="esp32-s3",
            connection_generation="generation-1",
            turn_id=turn,
            text="visible only to the subscribed companion session",
        )
        status, headers, value = _request(
            companion_port,
            "GET",
            f"/api/live?subscription={subscription}&after={cursor}",
            cookie=cookie,
        )
        assert status == 200
        assert headers["Cache-Control"] == "no-store"
        assert value["projection"]["question"].startswith("visible only")

        other_cookie, _ = _login(companion_port, "companion-secret")
        status, _, value = _request(
            companion_port,
            "GET",
            f"/api/live?subscription={subscription}&after={cursor}",
            cookie=other_cookie,
        )
        assert status == 404
        assert value["error"]["code"] == "subscription_unavailable"

        status, _, resumed = _request(
            companion_port,
            "POST",
            "/api/live/subscribe",
            body={
                "device_id": "esp32-s3",
                "connection_generation": "generation-1",
                "resume_token": resume_token,
                "after": cursor,
            },
            cookie=cookie,
            csrf=csrf,
            origin=f"http://127.0.0.1:{companion_port}",
        )
        assert status == 200
        assert resumed["resumed"] is True
        assert resumed["cursor"] == cursor
    finally:
        servers.close()


def test_frontend_uses_text_nodes_not_html_injection() -> None:
    static_root = Path(__file__).parents[3] / "src" / "agent_workflow_hub" / "xiaozhi_hermes_bridge" / "static"
    javascript = "\n".join(path.read_text(encoding="utf-8") for path in static_root.glob("*.js"))
    html = "\n".join(path.read_text(encoding="utf-8") for path in static_root.glob("*.html"))
    assert "innerHTML" not in javascript
    assert "outerHTML" not in javascript
    assert "insertAdjacentHTML" not in javascript
    assert "bootstrap" not in javascript.lower()
    assert "一次性" not in html
    assert 'type="password"' in html
    assert 'autocomplete="current-password"' in html
    assert "lingxi.login" in javascript
    assert "setUnauthorizedHandler" in javascript


def test_companion_status_tracks_online_state_and_hides_after_inactivity() -> None:
    static_root = Path(__file__).parents[3] / "src" / "agent_workflow_hub" / "xiaozhi_hermes_bridge" / "static"
    javascript = (static_root / "companion.js").read_text(encoding="utf-8")
    html = (static_root / "index-companion.html").read_text(encoding="utf-8")
    css = (static_root / "style.css").read_text(encoding="utf-8")

    assert 'notice(online ? "灵溪设备已上线" : "正在等待灵溪设备上线……")' in javascript
    assert "const UI_IDLE_MS = 4000" in javascript
    assert '"pointermove"' in javascript
    assert '"pointerdown"' in javascript
    assert '"keydown"' in javascript
    assert '"touchstart"' in javascript
    assert 'class="notice companion-ui"' in html
    assert ".companion.ui-hidden .companion-ui" in css
    assert 'id="mic-indicator"' in html
    assert 'id="live-dialogue"' in html
    assert 'id="question-text"' in html
    assert 'id="answer-text"' in html
    assert 'lingxi.request("/api/live/subscribe"' in javascript
    assert "const LIVE_POLL_MS = 500" in javascript
    assert "requestAnimationFrame" in javascript
    assert "Intl.Segmenter" in javascript
    assert "scrollDialogueToLatest" in javascript
    assert "viewGeneration" in javascript
    assert "livePollInFlight" in javascript
    assert "liveSyncPromise" in javascript
    assert "const COMPLETED_DIALOGUE_MS = 60000" in javascript
    assert "function scheduleDialogueClear" in javascript
    assert "const hasDialogue = Boolean(question || answer)" in javascript
    assert "nextTurn !== liveState.turnId && hasDialogue" in javascript
    assert "nextTurn === liveState.turnId && revision >= liveState.revision" in javascript
    assert "resume_token" in javascript
    assert 'id="answer-gap"' in html
    assert 'id="answer-tail"' in html
    assert "async function setPrivacy" in javascript
    assert "await unsubscribeLive()" in javascript
    assert 'data-mood="none"' in html
    assert "scheduleMood(12000)" in javascript
    assert ".mic-wave i" in css
    assert '.face-wrap[data-mood="wink"]' in css
    assert '.face-wrap[data-mood="cheeky"]' in css
    assert 'class="companion-page"' in html
    assert 'class="eye left"></span>' in html
    assert 'class="eye right"></span>' in html
    assert 'class="arm left"' not in html
    assert 'class="orbit orbit-a"' not in html
    assert ".arm, .orbit, .pupil, .brow, .cheek, .nose, .sparkle { display: none; }" in css
    assert "border: 0;" in css
    assert "--robot-eye: #78ffc4" in css
    assert "--robot-screen: #020608" in css
    assert '.face-wrap[data-state="happy"] .eye' in css
    assert '.face-wrap[data-state="error"] .eye' in css
    assert "@keyframes robot-blink" in css
    assert "@keyframes robot-listen" in css
    assert "@keyframes robot-ear-left" not in css
    assert 'style.css?v=8' in html
    assert "@media (orientation: portrait)" in css
    assert "@media (orientation: landscape) and (min-width: 900px)" in css
    assert "env(safe-area-inset-top)" in css
    assert "max-height: min(34svh, 360px)" in css
    assert 'id="butterfly-stage"' in html
    assert "function releaseButterfly" in javascript
    assert 'document.createElement("span")' in javascript
    assert 'window.addEventListener("pointerdown", releaseButterfly' in javascript
    assert ".click-butterfly" in css
    assert "@keyframes butterfly-flight" in css
    assert "prefers-reduced-motion: reduce" in css


def test_companion_keeps_completed_dialogue_until_new_turn_has_text() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the companion browser-state regression")
    static_root = Path(__file__).parents[3] / "src" / "agent_workflow_hub" / "xiaozhi_hermes_bridge" / "static"
    javascript = (static_root / "companion.js").read_text(encoding="utf-8")
    harness = r'''
const fs = require("fs");
let source = fs.readFileSync(0, "utf8");
const marker = "  lingxi.session()";
if (!source.includes(marker)) throw new Error("companion test export marker is missing");
source = source.replace(marker, "  globalThis.__companionTest = { applyProjection, liveState, render, startPolling };\n" + marker);

let timerId = 0;
const timers = new Map();
const intervals = new Map();
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      id,
      textContent: "",
      hidden: false,
      disabled: false,
      value: "70",
      dataset: { mood: "none" },
      style: { setProperty() {} },
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener() {}, focus() {}, append() {}, remove() {},
      scrollHeight: 0, scrollTop: 0, childElementCount: 0, firstElementChild: null,
    });
  }
  return elements.get(id);
}
global.document = {
  getElementById: element,
  createElement: (tag) => element(`created-${tag}-${elements.size}`),
  body: element("body"),
  documentElement: { requestFullscreen() {} },
  fullscreenElement: null,
  exitFullscreen() {},
};
global.window = {
  Intl,
  matchMedia: () => ({ matches: true, addEventListener() {} }),
  addEventListener() {},
  setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
  clearTimeout(id) { timers.delete(id); },
  setInterval(callback, delay) { const id = ++timerId; intervals.set(id, { callback, delay }); return id; },
  clearInterval(id) { intervals.delete(id); },
  requestAnimationFrame: () => ++timerId,
  cancelAnimationFrame() {},
};
let requestImpl = (path) => Promise.resolve(path === "/api/live/subscribe"
  ? { subscription: "subscription-1", resume_token: "resume-1", cursor: 0 }
  : {});
global.lingxi = {
  text(id, value) { element(id).textContent = value; },
  setUnauthorizedHandler() {},
  session() { return new Promise(() => {}); },
  request(path, options) { return requestImpl(path, options); },
  login() { return Promise.resolve({}); },
};
eval(source);
const api = globalThis.__companionTest;
const projection = (turn_id, revision, question, answer, capture_active = false, speech_active = false) => ({
  turn_id, revision, question, answer, capture_active, speech_active,
  question_truncated: false, answer_truncated: false,
  answer_head_length: 0, answer_omitted_characters: 0,
});

(async () => {
  api.applyProjection(projection("turn-1", 2, "第一问", "第一答"));
  if (element("question-text").textContent !== "第一问") throw new Error("first question was not rendered");
  if (element("answer-text").textContent !== "第一答") throw new Error("first answer was not rendered");
  if (timers.size !== 1 || [...timers.values()][0].delay !== 60000) throw new Error("completed dialogue timer is missing");

  api.applyProjection(projection("turn-1", 3, "第一问", "第一答", false, true));
  if (timers.size !== 0) throw new Error("same-turn speech did not cancel the completed dialogue timer");
  api.applyProjection(projection("turn-1", 4, "第一问", "第一答"));
  if (timers.size !== 1) throw new Error("same-turn speech stop did not restore the dialogue timer");

  const retainedTimer = [...timers.keys()][0];
  api.applyProjection(projection("turn-2", 0, "", "", true, true));
  api.applyProjection(projection("turn-3", 0, "", "", true, true));
  if (api.liveState.turnId !== "turn-1") throw new Error("blank listening turn replaced displayed turn");
  if (!timers.has(retainedTimer) || timers.size !== 1) throw new Error("blank turn changed the displayed turn timer");
  if (element("question-text").textContent !== "第一问" || element("answer-text").textContent !== "第一答") {
    throw new Error("blank listening turn cleared completed dialogue");
  }

  api.applyProjection(projection("turn-3", 1, "", "新回答"));
  if (api.liveState.turnId !== "turn-3") throw new Error("answer-first turn was not adopted");
  if (element("question-text").textContent !== "" || element("answer-text").textContent !== "新回答") {
    throw new Error("new answer-first turn mixed dialogue from the previous turn");
  }

  api.applyProjection(projection("", 0, "", ""));
  if (api.liveState.turnId !== "" || !element("live-dialogue").hidden) throw new Error("explicit clear did not hide dialogue");
  api.applyProjection(projection("turn-4", 1, "仅有问题", ""));
  if (timers.size !== 0) throw new Error("active question started the completed dialogue timer too early");
  api.applyProjection(projection("turn-5", 0, "", "", true, true));
  if (api.liveState.turnId !== "turn-4" || timers.size !== 1) throw new Error("superseded question-only turn was not bounded");

  api.applyProjection(projection("", 0, "", ""));
  api.applyProjection(projection("turn-6", 0, "", "", true));
  if (api.liveState.turnId !== "" || !element("live-dialogue").hidden) throw new Error("new page replayed a blank baseline turn");

  let usedWait = false;
  requestImpl = (path, options) => {
    if (path === "/api/live/wait") return Promise.resolve({resume_token: "wait-before-wake"});
    if (path === "/api/live/subscribe") {
      usedWait = options.body.resume_token === "wait-before-wake" && options.body.after === 0;
      return Promise.resolve({subscription: "subscription-1", resume_token: "resume-1", cursor: 1});
    }
    return Promise.resolve({});
  };
  await api.render({ runtime: { devices: [] } });
  await api.render({ runtime: { devices: [{
    device_id: "device-1", connection_generation: "generation-1", online: true,
    capabilities: {}, binding: null, pending_action: null,
  }] } });
  if (!usedWait) throw new Error("already-open offline page missed the first wake");
  api.applyProjection(projection("turn-7", 2, "离线前问题", "离线前回答"));
  let releaseUnsubscribe;
  requestImpl = (path) => path === "/api/live/unsubscribe"
    ? new Promise((resolve) => { releaseUnsubscribe = resolve; })
    : Promise.resolve({});
  const offlineRender = api.render({ runtime: { devices: [] } });
  if (!element("live-dialogue").hidden) throw new Error("device change waited for unsubscribe before clearing dialogue");
  releaseUnsubscribe({});
  await offlineRender;

  api.applyProjection(projection("turn-8", 2, "异常前问题", "异常前回答"));
  requestImpl = () => Promise.reject(new Error("snapshot unavailable"));
  api.startPolling();
  const refreshTimer = [...intervals.values()].find((item) => item.delay === 1000);
  refreshTimer.callback();
  await new Promise((resolve) => setImmediate(resolve));
  if (!element("live-dialogue").hidden) throw new Error("refresh failure left stale dialogue visible");
})().catch((error) => { console.error(error); process.exitCode = 1; });
'''
    completed = subprocess.run(
        [node, "-e", harness],
        input=javascript,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


class FakeBackend:
    async def snapshot(self):
        return {"status": "ok", "devices": []}

    async def list_agents(self):
        return (
            {"host": "hermes", "id": "default", "name": "<script>x</script>", "api_key": "secret"},
        )

    async def list_sessions(self, **_values: object):
        return (
            {"id": "chat-1", "title": "Title", "message_count": 2, "cwd": "C:/secret", "preview": "private"},
        )

    async def bind(self, **_values: object):
        return {"status": "ok", "binding_revision": 1}

    async def unbind(self, **_values: object):
        return {"status": "ok"}

    async def consume_action(self, **_values: object):
        return {"status": "ok"}

    async def stop_speaking(self, **_values: object):
        return {"status": "ok"}

    async def set_volume(self, **_values: object):
        return {"status": "ok"}


def test_control_coordinator_runs_on_owner_loop_and_sanitizes_dtos(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    events = ControlEventStore((tmp_path / "events.json").resolve(), runtime_epoch="epoch-b")
    coordinator = DeviceControlCoordinator(
        FakeBackend(),  # type: ignore[arg-type]
        event_store=events,
        settings=settings,
    )
    ready = threading.Event()
    loop = asyncio.new_event_loop()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()

    thread = threading.Thread(target=run_loop)
    thread.start()
    ready.wait(timeout=2)
    try:
        attached = threading.Event()

        def attach() -> None:
            coordinator.attach_running_loop(loop)
            attached.set()

        loop.call_soon_threadsafe(attach)
        attached.wait(timeout=2)
        agents = coordinator.list_agents()
        sessions = coordinator.list_sessions(host="hermes", agent="default", limit=20)
        assert agents["agents"] == [
            {"host": "hermes", "id": "default", "name": "<script>x</script>", "available": True}
        ]
        assert sessions["sessions"] == [
            {"id": "chat-1", "title": "Title", "message_count": 2}
        ]
        assert "secret" not in json.dumps({"agents": agents, "sessions": sessions})
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
