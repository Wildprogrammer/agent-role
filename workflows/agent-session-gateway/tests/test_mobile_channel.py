from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from agent_workflow_hub.xiaozhi_hermes_bridge.mobile_channel import MobileChannel, Phone, binding_snapshot, MAX_PCM
from agent_workflow_hub.xiaozhi_hermes_bridge.mobile_runtime import load_mobile_settings


class Adapter:
    def __init__(self):
        self.binding = dict(status="ok", bound=True, host="hermes", profile="test", stored_session_id="stored-A", binding_revision=1)
        self.sent = []
        self.cancelled = []
        self.gate = None

    async def show_binding(self, session, **kwargs):
        return dict(self.binding)

    async def list_agents(self, session):
        return dict(status="ok", agents=[dict(host="hermes", id="test", name="Test", available=True)])

    async def list_sessions(self, session, **kwargs):
        return dict(status="ok", sessions=[dict(id="stored-A", title="会话A", cwd="secret", preview="history")])

    async def bind_session(self, session, **kwargs):
        assert kwargs["expected_binding_revision"] == self.binding["binding_revision"]
        self.binding.update(stored_session_id=kwargs["stored_session_id"], binding_revision=self.binding["binding_revision"] + 1)
        return dict(status="ok")

    async def dispatch_bound(self, session, **kwargs):
        if kwargs["expected_binding"] != binding_snapshot(kwargs["device_id"], self.binding):
            return dict(status="rejected")
        self.sent.append(kwargs)
        await kwargs["on_progress"]("正在回答")
        if self.gate:
            await self.gate.wait()
        return dict(status="completed", output="这是手机上的完整回答。")

    async def cancel_active_for_device(self, session, **kwargs):
        self.cancelled.append(kwargs["device_id"])
        if self.gate:
            self.gate.set()
        return dict(status="cancelled")


class Socket:
    closed = False
    def __init__(self):
        self.events, self.frames = [], []
        self.phone = None

    async def send_json(self, value):
        self.events.append(value)

    async def send_bytes(self, value):
        self.frames.append(value)
        self.phone.ack.set()


def setup(root, adapter=None, transcribe=None, synthesize=None):
    adapter = adapter or Adapter()
    async def asr(pcm, session):
        return "手机识别的文字"
    async def tts(text):
        return b"mp3-audio"
    app = MobileChannel(adapter, transcribe=transcribe or asr, synthesize=synthesize or tts,
        attachment_root=root, password="test-password", origins=["http://127.0.0.1"], secure_cookie=False)
    phone = Phone("mobile-" + "a" * 32, socket=Socket())
    phone.socket.phone = phone
    return app, phone, adapter


@pytest.mark.parametrize("disconnect", [False, True])
def test_photo_route_slow_network_limits_and_disconnect_cleanup(tmp_path, disconnect):
    from types import SimpleNamespace
    from agent_workflow_hub.xiaozhi_hermes_bridge.attachments import AttachmentError

    async def run():
        app, phone, adapter = setup(tmp_path)
        app.phones[phone.device] = phone
        body = b"\xff\xd8\xff" + b"camera"
        reads = 0
        async def read(size):
            nonlocal reads
            reads += 1
            if reads == 1:
                return body
            if disconnect:
                raise ConnectionResetError("client closed")
            return b""
        class Request(dict):
            headers = {"X-File-Type": "image/jpeg", "X-File-Name": "camera.jpg"}
            content_length = len(body)
            content = SimpleNamespace(read=read)
        original_upload = app.store.upload
        async def upload(**kwargs):
            assert kwargs["timeout_seconds"] == 600
            assert kwargs["idle_timeout_seconds"] == 60
            return await original_upload(**kwargs)
        app.store.upload = upload
        request = Request(login=(phone.device, "csrf", 0))
        if disconnect:
            with pytest.raises(AttachmentError) as error:
                await app.photo(request)
            assert error.value.status_code == 499
            assert list(tmp_path.iterdir()) == []
        else:
            response = await app.photo(request)
            assert response.status == 200
            assert json.loads(response.text)["attachment_id"]
        assert not adapter.sent and not adapter.cancelled
    asyncio.run(run())


def test_phone_text_audio_and_duplicate_without_esp32(tmp_path):
    async def run():
        app, phone, adapter = setup(tmp_path)
        await app.command(phone, dict(type="submit", turn="1"*32, text="问题"))
        await phone.task
        assert len(adapter.sent) == 1
        assert phone.socket.frames == [b"1"*32 + b"mp3-audio"]
        assert any(e.get("text") == "这是手机上的完整回答。" and e.get("final") for e in phone.socket.events)
        with pytest.raises(ValueError, match="不能重复发送"):
            await app.command(phone, dict(type="submit", turn="1"*32, text="问题"))
    asyncio.run(run())


def test_muted_phone_keeps_text_skips_tts_and_can_enable_next_turn(tmp_path):
    async def run():
        calls = []
        async def tts(text):
            calls.append(text)
            return b"mp3-audio"
        app, phone, adapter = setup(tmp_path, synthesize=tts)
        with pytest.raises(ValueError, match="布尔值"):
            await app.command(phone, dict(type="audio_preference", enabled="false"))
        await app.command(phone, dict(type="audio_preference", enabled=False))
        await app.command(phone, dict(type="submit", turn="a"*32, text="只显示文字"))
        await asyncio.wait_for(phone.task, 1)
        assert len(adapter.sent) == 1 and not adapter.cancelled
        assert not calls and not phone.socket.frames
        assert any(e["type"] == "answer" and e.get("final") for e in phone.socket.events)
        assert any(e["type"] == "done" for e in phone.socket.events)
        await app.command(phone, dict(type="audio_preference", enabled=True))
        await app.command(phone, dict(type="submit", turn="b"*32, text="下一轮语音"))
        await asyncio.wait_for(phone.task, 1)
        assert len(calls) == 1 and len(phone.socket.frames) == 1
    asyncio.run(run())


def test_mute_during_agent_answer_does_not_cancel_dispatch(tmp_path):
    async def run():
        async def tts(text):
            pytest.fail("muted answer must not request TTS")
        app, phone, adapter = setup(tmp_path, synthesize=tts)
        adapter.gate = asyncio.Event()
        await app.command(phone, dict(type="submit", turn="a"*32, text="继续回答"))
        for _ in range(10):
            if adapter.sent:
                break
            await asyncio.sleep(0)
        assert adapter.sent
        await app.command(phone, dict(type="audio_preference", enabled=False))
        assert phone.busy and not adapter.cancelled
        adapter.gate.set()
        await asyncio.wait_for(phone.task, 1)
        assert any(e.get("final") for e in phone.socket.events)
    asyncio.run(run())


def test_mute_cancels_inflight_tts_without_error_or_replay(tmp_path):
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def tts(text):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        app, phone, adapter = setup(tmp_path, synthesize=tts)
        await app.command(phone, dict(type="submit", turn="a"*32, text="静音"))
        await asyncio.wait_for(entered.wait(), 1)
        await app.command(phone, dict(type="audio_preference", enabled=False))
        await app.command(phone, dict(type="audio_preference", enabled=True))
        await asyncio.wait_for(phone.task, 1)
        assert cancelled.is_set() and not phone.socket.frames and not adapter.cancelled
        assert not any(e["type"] == "error" for e in phone.socket.events)
        assert any(e["type"] == "done" for e in phone.socket.events)
    asyncio.run(run())


def test_mute_releases_playback_wait_and_skips_remaining_segments(tmp_path):
    async def run():
        calls, played = [], asyncio.Event()
        class LongAnswer(Adapter):
            async def dispatch_bound(self, session, **kwargs):
                return dict(status="completed", output="很长的回答。"*100)
        async def tts(text):
            calls.append(text)
            return b"mp3-audio"
        app, phone, adapter = setup(tmp_path, adapter=LongAnswer(), synthesize=tts)
        async def send_without_ack(value):
            phone.socket.frames.append(value)
            played.set()
        phone.socket.send_bytes = send_without_ack
        await app.command(phone, dict(type="submit", turn="a"*32, text="停止声音"))
        await asyncio.wait_for(played.wait(), 1)
        await app.command(phone, dict(type="audio_preference", enabled=False))
        await asyncio.wait_for(phone.task, 1)
        assert len(calls) == len(phone.socket.frames) == 1
        assert not adapter.cancelled
        assert any(e["type"] == "done" for e in phone.socket.events)
    asyncio.run(run())


def test_capture_and_binding_changed_during_asr(tmp_path):
    async def run():
        adapter = Adapter()
        async def asr(pcm, session):
            assert pcm == b"\0" * 3200
            adapter.binding.update(stored_session_id="stored-B", binding_revision=2)
            return "语音问题"
        app, phone, _ = setup(tmp_path, adapter, asr)
        await app.command(phone, dict(type="capture", turn="2"*32))
        phone.capture.extend(b"\0" * 3200)
        await app.command(phone, dict(type="capture_end", turn="2"*32))
        await phone.task
        assert not adapter.sent and not phone.socket.frames
        assert any(e["type"] == "error" for e in phone.socket.events)
    asyncio.run(run())


def test_stop_cancels_only_phone_and_suppresses_late_asr(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def asr(pcm, session):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                return "旧文字"
        app, phone, adapter = setup(tmp_path, transcribe=asr)
        await app.command(phone, dict(type="capture", turn="3"*32))
        phone.capture.extend(b"\0"*3200)
        await app.command(phone, dict(type="capture_end", turn="3"*32))
        await entered.wait()
        await app.stop(phone)
        assert phone.turn == "" and not phone.busy and not adapter.sent
        assert adapter.cancelled == []
        assert not phone.socket.frames
    asyncio.run(run())


def test_stop_during_host_and_bind_while_busy(tmp_path):
    async def run():
        app, phone, adapter = setup(tmp_path)
        adapter.gate = asyncio.Event()
        await app.command(phone, dict(type="submit", turn="4"*32, text="问题"))
        for _ in range(10):
            if adapter.sent:
                break
            await asyncio.sleep(0)
        with pytest.raises(ValueError, match="停止"):
            await app.command(phone, dict(type="bind"))
        await app.stop(phone)
        if phone.stop_task:
            await phone.stop_task
        assert adapter.cancelled == [phone.device]
        assert not phone.socket.frames and not phone.busy
        count = len(phone.socket.events)
        await app.emit(phone, "answer", turn="4"*32, text="迟到")
        assert len(phone.socket.events) == count
    asyncio.run(run())


def test_photo_binding_and_single_submission(tmp_path):
    async def run():
        app, phone, adapter = setup(tmp_path)
        snapshot = binding_snapshot(phone.device, adapter.binding)
        grant = await app.store.prepare(request_id="photo", binding=snapshot)
        async def receive():
            return {"type":"http.request", "body":b"\xff\xd8\xff" + b"photo", "more_body":False}
        record = await app.store.upload(token=grant.token, name="photo.jpg", declared_media_type="image/jpeg", receive=receive, content_length=8)
        adapter.binding.update(binding_revision=2)
        with pytest.raises(AttachmentError):
            await app.command(phone, dict(type="submit", turn="5"*32, text="看图", attachments=[record.attachment_id]))
        assert not adapter.sent
        adapter.binding.update(binding_revision=1)
        await app.command(phone, dict(type="submit", turn="6"*32, text="看图", attachments=[record.attachment_id]))
        await phone.task
        assert len(adapter.sent) == 1 and len(adapter.sent[0]["attachments"]) == 1
        assert not record.path.exists()
    from agent_workflow_hub.xiaozhi_hermes_bridge.attachments import AttachmentError
    asyncio.run(run())


def test_tts_failure_preserves_answer(tmp_path):
    async def run():
        async def fail(text):
            raise RuntimeError("private provider error")
        app, phone, _ = setup(tmp_path, synthesize=fail)
        await app.command(phone, dict(type="submit", turn="7"*32, text="问题"))
        await phone.task
        assert any(e.get("final") for e in phone.socket.events)
        assert any("文字回答已保留" in e.get("message", "") for e in phone.socket.events)
        assert "private provider" not in json.dumps(phone.socket.events)
    asyncio.run(run())


def test_real_http_auth_origin_and_websocket_pcm(tmp_path):
    async def run():
        app, _, adapter = setup(tmp_path)
        server = TestServer(app.app)
        await server.start_server()
        origin = str(server.make_url("")).rstrip("/")
        app.origins = {origin}
        async with TestClient(server, cookie_jar=CookieJar(unsafe=True)) as client:
            assert (await client.get("/")).status == 200
            assert (await client.get("/api/session")).status == 401
            assert (await client.post("/api/login",json={})).status == 403
            response = await client.post("/api/login",headers={"Origin":origin},json={"password":"test-password","terminal":"a"*32})
            assert response.status == 200
            csrf = (await response.json())["csrf"]
            assert (await client.post("/api/logout",headers={"Origin":origin})).status == 403
            socket = await client.ws_connect("/api/socket",headers={"Origin":origin})
            assert (await socket.receive_json())["type"] == "ready"
            await socket.send_json(dict(type="sessions",host="hermes",agent="test"))
            rows = await socket.receive_json()
            assert rows["sessions"] == [{"id":"stored-A","title":"会话A"}]
            await socket.send_json(dict(type="capture",turn="8"*32))
            assert (await socket.receive_json())["type"] == "capture_ready"
            await socket.send_bytes(b"\0"*3200)
            await socket.send_json(dict(type="capture_end",turn="8"*32))
            for _ in range(16):
                message = await socket.receive(timeout=3)
                if message.type.name == "BINARY":
                    assert message.data.startswith(b"8"*32)
                    await socket.send_json(dict(type="played",turn="8"*32,sequence=0))
                elif message.type.name == "TEXT" and json.loads(message.data)["type"] == "idle":
                    break
            assert adapter.sent[0]["prompt"] == "手机识别的文字"
            await socket.close()
            assert (await client.post("/api/logout",headers={"Origin":origin,"X-CSRF":csrf})).status == 200
    asyncio.run(run())


def test_settings_requires_tls_for_lan_and_https_origins(tmp_path):
    settings = dict(host="0.0.0.0", port=8767, origins=["https://192.0.2.10:8767"], attachment_root=str(tmp_path))
    path = tmp_path / "phone.json"
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="TLS"):
        load_mobile_settings(path)
    settings.update(certificate=str(tmp_path/"server.pem"),private_key=str(tmp_path/"server.key"))
    path.write_text(json.dumps(settings))
    assert load_mobile_settings(path)["port"] == 8767
    settings["origins"] = ["https://host/path"]
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="origin"):
        load_mobile_settings(path)


def test_stop_preserves_real_adapter_cancellation_state_and_blocks_next_turn(tmp_path):
    from agent_workflow_hub.xiaozhi_hermes_bridge.xiaozhi_server_adapter import XiaozhiHermesAdapter
    async def run():
        entered, complete = asyncio.Event(), asyncio.Event()
        binding = Adapter().binding
        calls = []
        class Client:
            async def request(self, operation, **kwargs):
                calls.append(operation)
                if operation == "show_binding":
                    return dict(binding)
                if operation == "dispatch_bound_voice":
                    entered.set()
                    await complete.wait()
                    return {"status":"completed", "output":"旧回复"}
                if operation == "cancel_bound_voice":
                    return {"status":"requested"}
                raise AssertionError(operation)
        adapter = XiaozhiHermesAdapter(Client(), control_client=Client())
        app, phone, _ = setup(tmp_path, adapter)
        await app.command(phone, dict(type="submit", turn="a"*32, text="第一轮"))
        await entered.wait()
        await app.stop(phone)
        assert "cancel_bound_voice" in calls
        assert phone.busy
        with pytest.raises(ValueError, match="尚未结束"):
            await app.command(phone, dict(type="submit", turn="b"*32, text="不能提前发送"))
        complete.set()
        await phone.stop_task
        assert not phone.busy and not phone.socket.frames
        await app.command(phone, dict(type="submit", turn="b"*32, text="第二轮"))
        await phone.task
        assert calls.count("dispatch_bound_voice") == 2
        assert phone.socket.frames == [b"b"*32 + b"mp3-audio"]
    asyncio.run(run())


def test_exact_management_phrase_is_not_sent_as_prompt(tmp_path):
    async def run():
        app, phone, adapter = setup(tmp_path)
        await app.command(phone, dict(type="submit", turn="c"*32, text="开启新会话。"))
        await phone.task
        assert not adapter.sent
        await app.command(phone, dict(type="submit", turn="d"*32, text="为什么要说开启新会话？"))
        await phone.task
        assert len(adapter.sent) == 1
    asyncio.run(run())


def test_trusted_https_login_and_websocket(tmp_path):
    import importlib.util
    import ssl
    from aiohttp import TCPConnector
    spec = importlib.util.spec_from_file_location("mobile_cert", Path(__file__).parents[1]/"scripts"/"create_mobile_certificate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tls = tmp_path / "tls"
    module.create(tls, ["127.0.0.1"])
    async def run():
        channel, _, _ = setup(tmp_path / "attachments")
        channel.secure_cookie = True
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(tls/"server.crt", tls/"server.key")
        server = TestServer(channel.app, scheme="https")
        await server.start_server(ssl=server_tls)
        origin = str(server.make_url("")).rstrip("/")
        channel.origins = {origin}
        trusted = ssl.create_default_context(cafile=str(tls/"lingxi-ca.crt"))
        async with TestClient(server, connector=TCPConnector(ssl=trusted), cookie_jar=CookieJar(unsafe=True)) as client:
            response = await client.post("/api/login", headers={"Origin":origin}, json={"password":"test-password","terminal":"f"*32})
            assert response.status == 200
            assert response.cookies["lingxi_mobile"]["secure"]
            async with client.ws_connect("/api/socket", headers={"Origin":origin}) as socket:
                assert (await socket.receive_json())["type"] == "ready"
    asyncio.run(run())
