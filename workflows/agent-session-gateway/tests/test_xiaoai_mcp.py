from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.xiaoai_mcp import (
    XiaoAiMcpConfigurationError,
    XiaoAiMcpGateway,
    XiaoAiMcpSecurityMiddleware,
    XiaoAiMcpSettings,
    create_xiaoai_mcp_server,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.attachments import AttachmentBinding, AttachmentStore
from agent_workflow_hub.xiaozhi_hermes_bridge import xiaoai_mcp


class FakeAdapter:
    def __init__(self) -> None:
        self.binding: dict[str, object] = {
            "schema_version": "1.0",
            "status": "ok",
            "operation": "show_binding",
            "bound": False,
        }
        self.bound: list[tuple[str, str, str, int | None]] = []
        self.sent: list[str] = []
        self.sent_attachments: list[tuple[Mapping[str, object], ...]] = []
        self.closed = False

    async def list_agents(self, session_id: str) -> Mapping[str, object]:
        assert session_id == "xiaoai-mcp"
        return {
            "status": "ok",
            "agents": [
                {
                    "host": "hermes",
                    "id": "default",
                    "name": "default",
                    "available": True,
                },
                {
                    "host": "deepseek-harness",
                    "id": "assistant-agent",
                    "name": "Role Agent",
                    "available": False,
                },
                {"host": "invalid", "id": "hidden", "name": "hidden"},
            ],
        }

    async def list_sessions(
        self,
        session_id: str,
        *,
        profile: str,
        limit: int = 10,
        host: str = "hermes",
    ) -> Mapping[str, object]:
        assert session_id == "xiaoai-mcp"
        assert limit <= 20
        return {
            "status": "ok",
            "sessions": [
                {
                    "id": "stored-1",
                    "title": "Shared chat",
                    "preview": "private history",
                    "cwd": "C:/private",
                    "message_count": 3,
                    "updated_at": 10,
                }
            ],
        }

    async def show_binding(
        self, session_id: str, *, device_id: str
    ) -> Mapping[str, object]:
        assert session_id == "xiaoai-mcp"
        assert device_id == "xiaoai-phone"
        return dict(self.binding)

    async def bind_session(
        self,
        session_id: str,
        *,
        device_id: str,
        host: str = "hermes",
        profile: str,
        stored_session_id: str,
        expected_binding_revision: int | None = None,
    ) -> Mapping[str, object]:
        assert session_id == "xiaoai-mcp"
        assert device_id == "xiaoai-phone"
        revision = expected_binding_revision or 0
        self.bound.append((host, profile, stored_session_id, expected_binding_revision))
        self.binding = {
            "status": "ok",
            "bound": True,
            "device_id": device_id,
            "host": host,
            "profile": profile,
            "stored_session_id": stored_session_id,
            "title": "Bound chat",
            "binding_revision": revision + 1,
        }
        return dict(self.binding)

    async def dispatch_bound(
        self,
        session_id: str,
        *,
        device_id: str,
        prompt: str,
        attachments: tuple[Mapping[str, object], ...] = (),
        expected_binding: AttachmentBinding | None = None,
    ) -> Mapping[str, object]:
        assert session_id == "xiaoai-mcp"
        assert device_id == "xiaoai-phone"
        if attachments:
            assert expected_binding == AttachmentBinding(
                device_id, self.binding["host"], self.binding["profile"],
                self.binding["stored_session_id"], self.binding["binding_revision"],
            )
        self.sent.append(prompt)
        self.sent_attachments.append(attachments)
        return {
            "status": "completed",
            "host": self.binding.get("host", "hermes"),
            "stored_session_id": self.binding.get("stored_session_id", "stored-1"),
            "output": f"answer:{prompt}",
        }

    async def close(self) -> None:
        self.closed = True


def settings() -> XiaoAiMcpSettings:
    return XiaoAiMcpSettings(
        host="127.0.0.1",
        port=8770,
        token="t" * 48,
        device_id="xiaoai-phone",
        session_id="xiaoai-mcp",
        max_body_bytes=4096,
        requests_per_minute=60,
        max_concurrent_requests=8,
        body_read_timeout_seconds=10,
        max_input_chars=8000,
        max_output_chars=4000,
        send_cache_seconds=600,
    )


def test_settings_require_loopback_strong_token_and_server_owned_device() -> None:
    base = {
        "XIAOAI_MCP_TOKEN": "x" * 48,
        "XIAOAI_MCP_DEVICE_ID": "xiaoai-phone",
    }
    parsed = XiaoAiMcpSettings.from_environment(base)
    assert parsed.host == "127.0.0.1"
    assert parsed.port == 8770
    assert parsed.max_input_chars == 8000
    assert parsed.max_attachment_bytes == 52_428_800

    with pytest.raises(XiaoAiMcpConfigurationError, match="between 200 and 8000"):
        XiaoAiMcpSettings.from_environment(
            {**base, "XIAOAI_MCP_MAX_INPUT_CHARS": "8001"}
        )

    with pytest.raises(XiaoAiMcpConfigurationError, match="127.0.0.1"):
        XiaoAiMcpSettings.from_environment({**base, "XIAOAI_MCP_HOST": "0.0.0.0"})
    with pytest.raises(XiaoAiMcpConfigurationError, match="32..512"):
        XiaoAiMcpSettings.from_environment({**base, "XIAOAI_MCP_TOKEN": "short"})
    with pytest.raises(XiaoAiMcpConfigurationError, match="DEVICE_ID"):
        XiaoAiMcpSettings.from_environment({"XIAOAI_MCP_TOKEN": "x" * 48})
    with pytest.raises(XiaoAiMcpConfigurationError, match="between 1048576 and 52428800"):
        XiaoAiMcpSettings.from_environment(
            {**base, "XIAOAI_MCP_MAX_ATTACHMENT_BYTES": "52428801"}
        )
    with pytest.raises(XiaoAiMcpConfigurationError, match="HTTPS origin"):
        XiaoAiMcpSettings.from_environment(
            {**base, "XIAOAI_MCP_PUBLIC_BASE_URL": "http://example.test"}
        )


@pytest.mark.asyncio
async def test_inventory_is_bounded_and_hides_history_and_paths() -> None:
    gateway = XiaoAiMcpGateway(FakeAdapter(), device_id="xiaoai-phone")
    agents = await gateway.list_agents()
    assert [row["agent"] for row in agents["agents"]] == ["default", "assistant-agent"]

    sessions = await gateway.list_sessions(host="hermes", agent="default")
    row = sessions["sessions"][0]
    assert row["session_id"] == "stored-1"
    assert "preview" not in row
    assert "cwd" not in row


@pytest.mark.asyncio
async def test_first_bind_is_direct_but_rebind_requires_exact_confirmation() -> None:
    adapter = FakeAdapter()
    gateway = XiaoAiMcpGateway(adapter, device_id="xiaoai-phone")

    first = await gateway.bind_session(
        host="hermes", agent="default", session_id="stored-1"
    )
    assert first["status"] == "ok"
    assert len(adapter.bound) == 1

    prepared = await gateway.bind_session(
        host="deepseek-harness", agent="assistant-agent", session_id="dsh-1"
    )
    assert prepared["status"] == "confirmation_required"
    assert len(adapter.bound) == 1

    rejected = await gateway.bind_session(
        host="deepseek-harness",
        agent="assistant-agent",
        session_id="dsh-1",
        confirmation_id="wrong",
    )
    assert rejected["status"] == "rejected"
    assert len(adapter.bound) == 1

    confirmed = await gateway.bind_session(
        host="deepseek-harness",
        agent="assistant-agent",
        session_id="dsh-1",
        confirmation_id=prepared["confirmation_id"],
    )
    assert confirmed["status"] == "ok"
    assert confirmed["host"] == "deepseek-harness"
    assert len(adapter.bound) == 2


@pytest.mark.asyncio
async def test_send_request_id_deduplicates_and_cannot_change_text() -> None:
    adapter = FakeAdapter()
    adapter.binding = {
        "status": "ok",
        "bound": True,
        "host": "hermes",
        "profile": "default",
        "stored_session_id": "stored-1",
        "title": "Shared chat",
        "binding_revision": 1,
    }
    gateway = XiaoAiMcpGateway(adapter, device_id="xiaoai-phone")

    first = await gateway.send_message(text="hello", request_id="req-1")
    replay = await gateway.send_message(text="hello", request_id="req-1")
    conflict = await gateway.send_message(text="different", request_id="req-1")

    assert first["status"] == "completed"
    assert replay["replayed"] is True
    assert conflict["status"] == "rejected"
    assert adapter.sent == ["hello"]


@pytest.mark.asyncio
async def test_prepare_upload_and_send_attachment_is_bound_and_idempotent(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter()
    adapter.binding = {
        "status": "ok",
        "bound": True,
        "host": "deepseek-harness",
        "profile": "assistant-agent",
        "stored_session_id": "dsh-1",
        "binding_revision": 3,
    }
    store = AttachmentStore(tmp_path, max_attachment_bytes=1024)
    gateway = XiaoAiMcpGateway(
        adapter,
        device_id="xiaoai-phone",
        attachment_store=store,
        public_base_url="https://example.test",
    )

    prepared = await gateway.prepare_attachment_upload(request_id="upload-1")
    replayed_prepare = await gateway.prepare_attachment_upload(request_id="upload-1")
    assert prepared == replayed_prepare
    token = parse_qs(urlsplit(prepared["upload_url"]).query)["token"][0]
    incoming = [
        {"type": "http.request", "body": b"hello attachment", "more_body": False}
    ]

    async def receive() -> Mapping[str, object]:
        return incoming.pop(0)

    staged = await store.upload(
        token=token,
        name="note.txt",
        declared_media_type="text/plain",
        receive=receive,
        content_length=16,
    )
    result = await gateway.send_message(
        text="read this",
        request_id="send-with-file",
        attachment_ids=[staged.attachment_id],
    )
    replay = await gateway.send_message(
        text="read this",
        request_id="send-with-file",
        attachment_ids=[staged.attachment_id],
    )

    assert result["status"] == "completed"
    assert replay["replayed"] is True
    assert adapter.sent == ["read this"]
    assert adapter.sent_attachments[0][0]["attachment_id"] == staged.attachment_id
    assert not staged.path.exists()


@pytest.mark.asyncio
async def test_send_cache_ttl_starts_after_a_slow_host_call(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]

    class SlowAdapter(FakeAdapter):
        async def dispatch_bound(
            self, session_id: str, *, device_id: str, prompt: str
        ) -> Mapping[str, object]:
            clock[0] += 700.0
            return await super().dispatch_bound(
                session_id, device_id=device_id, prompt=prompt
            )

    monkeypatch.setattr(xiaoai_mcp.time, "monotonic", lambda: clock[0])
    adapter = SlowAdapter()
    adapter.binding = {
        "status": "ok",
        "bound": True,
        "host": "hermes",
        "profile": "default",
        "stored_session_id": "stored-1",
        "binding_revision": 1,
    }
    gateway = XiaoAiMcpGateway(
        adapter, device_id="xiaoai-phone", send_cache_seconds=600
    )

    await gateway.send_message(text="slow", request_id="req-slow")
    replay = await gateway.send_message(text="slow", request_id="req-slow")

    assert replay["replayed"] is True
    assert adapter.sent == ["slow"]


@pytest.mark.asyncio
async def test_send_cache_is_bounded_and_input_is_limited() -> None:
    adapter = FakeAdapter()
    adapter.binding = {
        "status": "ok",
        "bound": True,
        "host": "hermes",
        "profile": "default",
        "stored_session_id": "stored-1",
        "binding_revision": 1,
    }
    gateway = XiaoAiMcpGateway(
        adapter,
        device_id="xiaoai-phone",
        max_input_chars=10,
        send_cache_seconds=3600,
    )

    rejected = await gateway.send_message(text="x" * 11, request_id="too-long")
    assert rejected["status"] == "rejected"
    for index in range(300):
        result = await gateway.send_message(text="ok", request_id=f"req-{index}")
        assert result["status"] == "completed"

    assert len(gateway._send_cache) == 256
    assert len(adapter.sent) == 300


@pytest.mark.asyncio
async def test_cancelled_host_call_is_cached_as_outcome_unknown() -> None:
    started = asyncio.Event()

    class BlockingAdapter(FakeAdapter):
        async def dispatch_bound(
            self, session_id: str, *, device_id: str, prompt: str
        ) -> Mapping[str, object]:
            self.sent.append(prompt)
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    adapter = BlockingAdapter()
    adapter.binding = {
        "status": "ok",
        "bound": True,
        "host": "hermes",
        "profile": "default",
        "stored_session_id": "stored-1",
        "binding_revision": 1,
    }
    gateway = XiaoAiMcpGateway(adapter, device_id="xiaoai-phone")
    task = asyncio.create_task(
        gateway.send_message(text="once", request_id="req-cancel")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    replay = await gateway.send_message(text="once", request_id="req-cancel")
    assert replay["status"] == "outcome_unknown"
    assert replay["replayed"] is True
    assert adapter.sent == ["once"]


@pytest.mark.asyncio
async def test_server_exposes_six_tools_without_device_identity() -> None:
    gateway = XiaoAiMcpGateway(FakeAdapter(), device_id="xiaoai-phone")
    server = create_xiaoai_mcp_server(gateway, settings())
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == {
        "list_agents",
        "list_sessions",
        "show_binding",
        "bind_session",
        "prepare_attachment_upload",
        "send_message",
    }
    assert "device_id" not in json.dumps(
        {name: tool.inputSchema for name, tool in tools.items()}, ensure_ascii=False
    )
    assert "request_id" in tools["send_message"].inputSchema["required"]
    assert "request_id" in tools["prepare_attachment_upload"].inputSchema["required"]
    assert "attachment_ids" not in tools["send_message"].inputSchema["required"]
    assert server.settings.stateless_http is True


async def _asgi_request(
    app: object,
    *,
    authorization: str | list[str] | None,
    body: bytes = b"{}",
) -> tuple[int, bytes, bool]:
    headers = [(b"host", b"127.0.0.1:8770"), (b"content-type", b"application/json")]
    if isinstance(authorization, str):
        headers.append((b"authorization", authorization.encode("ascii")))
    elif authorization is not None:
        headers.extend(
            (b"authorization", value.encode("ascii")) for value in authorization
        )
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8770),
    }
    incoming = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, object]] = []
    called = False

    async def receive() -> Mapping[str, object]:
        return incoming.pop(0)

    async def send(message: Mapping[str, object]) -> None:
        sent.append(dict(message))

    async def downstream(
        _scope: Mapping[str, object], _receive: object, downstream_send: object
    ) -> None:
        nonlocal called
        called = True
        await downstream_send(
            {"type": "http.response.start", "status": 204, "headers": []}
        )
        await downstream_send({"type": "http.response.body", "body": b""})

    selected = app if app is not None else downstream
    await selected(scope, receive, send)
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    response_body = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    return status, response_body, called


@pytest.mark.asyncio
async def test_security_middleware_requires_token_and_bounds_body() -> None:
    called = False

    async def downstream(scope: Mapping[str, object], receive: object, send: object) -> None:
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    middleware = XiaoAiMcpSecurityMiddleware(
        downstream,
        token="t" * 48,
        max_body_bytes=8,
        requests_per_minute=5,
    )
    status, _, _ = await _asgi_request(middleware, authorization=None)
    assert status == 401
    assert called is False

    status, _, _ = await _asgi_request(
        middleware, authorization="t" * 48, body=b"{}"
    )
    assert status == 401
    assert called is False

    status, _, _ = await _asgi_request(
        middleware,
        authorization=["Bearer " + "t" * 48, "Bearer " + "t" * 48],
        body=b"{}",
    )
    assert status == 401
    assert called is False

    status, _, _ = await _asgi_request(
        middleware, authorization="Bearer " + "t" * 48, body=b"0123456789"
    )
    assert status == 413
    assert called is False

    status, _, _ = await _asgi_request(
        middleware, authorization="Bearer " + "t" * 48, body=b"{}"
    )
    assert status == 204
    assert called is True


@pytest.mark.asyncio
async def test_security_middleware_stops_on_client_disconnect() -> None:
    called = False
    receive_count = 0

    async def downstream(scope: Mapping[str, object], receive: object, send: object) -> None:
        nonlocal called
        called = True

    middleware = XiaoAiMcpSecurityMiddleware(
        downstream,
        token="t" * 48,
        max_body_bytes=4096,
        requests_per_minute=5,
    )
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer " + b"t" * 48)],
    }

    async def receive() -> Mapping[str, object]:
        nonlocal receive_count
        receive_count += 1
        return {"type": "http.disconnect"}

    async def send(message: Mapping[str, object]) -> None:
        raise AssertionError(f"disconnected request must not send: {message}")

    await middleware(scope, receive, send)
    assert receive_count == 1
    assert called is False


@pytest.mark.asyncio
async def test_upload_page_uses_one_time_token_without_mcp_bearer(
    tmp_path: Path,
) -> None:
    store = AttachmentStore(tmp_path, max_attachment_bytes=1024)
    grant = await store.prepare(
        request_id="upload-page",
        binding=xiaoai_mcp.AttachmentBinding(
            device_id="xiaoai-phone",
            host="deepseek-harness",
            profile="assistant-agent",
            stored_session_id="dsh-1",
            binding_revision=1,
        ),
    )

    async def downstream(*_args: object) -> None:
        raise AssertionError("upload routes must not reach the MCP app")

    middleware = XiaoAiMcpSecurityMiddleware(
        downstream,
        token="t" * 48,
        attachment_store=store,
        max_body_bytes=4096,
        requests_per_minute=5,
    )

    async def invoke(
        *, method: str, path: str, query: bytes = b"", headers=(), body: bytes = b""
    ) -> tuple[int, bytes]:
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": query,
            "headers": list(headers),
        }
        incoming = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict[str, object]] = []

        async def receive() -> Mapping[str, object]:
            return incoming.pop(0)

        async def send(message: Mapping[str, object]) -> None:
            sent.append(dict(message))

        await middleware(scope, receive, send)
        status = next(
            item["status"] for item in sent if item["type"] == "http.response.start"
        )
        payload = b"".join(
            item.get("body", b"")
            for item in sent
            if item["type"] == "http.response.body"
        )
        return status, payload

    status, page = await invoke(
        method="GET",
        path="/upload",
        query=f"token={grant.token}".encode("ascii"),
    )
    assert status == 200
    assert grant.token.encode() not in page
    assert grant.attachment_id.encode() in page

    status, payload = await invoke(
        method="POST",
        path="/attachments",
        headers=(
            (b"content-type", b"application/octet-stream"),
            (b"content-length", b"5"),
            (b"x-upload-token", grant.token.encode("ascii")),
            (b"x-file-name", b"note.txt"),
            (b"x-file-type", b"text/plain"),
        ),
        body=b"hello",
    )
    assert status == 201
    assert json.loads(payload)["attachment_id"] == grant.attachment_id
