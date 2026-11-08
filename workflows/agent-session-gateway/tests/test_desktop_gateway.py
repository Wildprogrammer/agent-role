from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import BridgeAttachment
from agent_workflow_hub.xiaozhi_hermes_bridge.desktop_gateway import (
    DesktopGatewayError,
    DiscoveredBackend,
    HermesDesktopGateway,
    NativeGatewayBackend,
    discover_desktop_backend,
    discover_desktop_gateway_backend,
    parse_spawn_ledger,
)


def _write_backend_files(
    tmp_path: Path, *, token: str = "private-token", pid: int = 4242, port: int = 8123
) -> tuple[Path, Path]:
    machine = tmp_path / "hermes"
    locks = tmp_path / "locks"
    machine.mkdir()
    locks.mkdir()
    (machine / "spawn-ledger.json").write_text(
        json.dumps(
            [
                {
                    "pid": pid,
                    "purpose": "serve",
                    "host": "127.0.0.1",
                    "port": port,
                    "registered_at": 10,
                    "isolated": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    (locks / "host-desktop-serve.json").write_text(
        json.dumps(
            {
                "role": "desktop-serve",
                "protocolVersion": 1,
                "pid": pid,
                "port": port,
                "tokenFingerprint": hashlib.sha256(token.encode()).hexdigest()[:16],
            }
        ),
        encoding="utf-8",
    )
    (locks / "host-desktop-serve.token").write_text(token + "\n", encoding="utf-8")
    return machine.resolve(), locks.resolve()


def test_parse_spawn_ledger_keeps_only_attachable_loopback_records() -> None:
    records = parse_spawn_ledger(
        json.dumps(
            [
                {"pid": 1, "purpose": "serve", "host": "127.0.0.1", "port": 1234},
                {"pid": 2, "purpose": "serve", "host": "192.0.2.1", "port": 1234},
                {"pid": 3, "purpose": "mcp-helper", "host": "", "port": 1234},
                {"pid": 4, "purpose": "dashboard", "host": "::", "port": 2345, "isolated": True},
            ]
        )
    )

    assert [(record.pid, record.port) for record in records] == [(1, 1234)]


def test_discovery_matches_publication_without_exposing_token(tmp_path: Path) -> None:
    machine, locks = _write_backend_files(tmp_path)

    backend = discover_desktop_backend(
        machine_root=machine, lock_dir=locks, process_alive=lambda pid: pid == 4242
    )

    assert backend.record.port == 8123
    assert "private-token" not in repr(backend)
    assert "token=private-token" in backend.websocket_url


def test_discovery_rejects_a_mismatched_token(tmp_path: Path) -> None:
    machine, locks = _write_backend_files(tmp_path)
    (locks / "host-desktop-serve.token").write_text("other-token", encoding="utf-8")

    with pytest.raises(DesktopGatewayError, match="matching published token"):
        discover_desktop_backend(
            machine_root=machine, lock_dir=locks, process_alive=lambda pid: True
        )


def test_combined_discovery_falls_back_to_legacy_serve_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine, locks = _write_backend_files(tmp_path, pid=os.getpid())
    assert machine == (tmp_path / "hermes").resolve()
    monkeypatch.setattr(
        "agent_workflow_hub.xiaozhi_hermes_bridge.desktop_gateway._native_runtime_paths",
        lambda environment: (_ for _ in ()).throw(
            DesktopGatewayError("native unavailable")
        ),
    )
    backend = discover_desktop_gateway_backend(
        {
            "LOCALAPPDATA": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "HERMES_GATEWAY_LOCK_DIR": str(locks),
        }
    )

    assert isinstance(backend, DiscoveredBackend)
    assert backend.describe().transport == "legacy-serve-token"


class FakeTicketBootstrap:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def request(self, operation: str, profile: str) -> dict[str, object]:
        self.requests.append((operation, profile))
        base: dict[str, object] = {
            "api_origin": "http://127.0.0.1:9123",
            "profile_id": f"C:/profiles/{profile}",
            "instance_id": "owner-1",
            "runtime_protocol": 1,
        }
        if operation == "describe":
            return base
        return {
            **base,
            "url": "ws://127.0.0.1:9123/api/ws",
            "subprotocols": [
                "hermes-gateway-v1",
                "hermes-gateway-ticket.one-use-secret",
            ],
        }


def test_native_backend_describes_ticket_transport_without_exposing_ticket() -> None:
    bootstrap = FakeTicketBootstrap()
    backend = NativeGatewayBackend(bootstrap)

    description = backend.describe("default")
    target = backend.connection_target("assistant-agent")

    assert description.transport == "native-gateway-ticket"
    assert description.credential_mode == "one-use-ticket"
    assert description.port == 9123
    assert "one-use-secret" not in repr(target)
    assert bootstrap.requests == [("describe", "default"), ("target", "assistant-agent")]


class FakeSocket:
    def __init__(self, *, prompt_status: str = "streaming") -> None:
        self.frames: list[str] = [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {"type": "gateway.ready", "session_id": "", "payload": {}},
                }
            )
        ]
        self.sent: list[dict[str, object]] = []
        self.closed = False
        self.recv_timeouts: list[float | None] = []
        self.prompt_status = prompt_status

    def send(self, message: str) -> None:
        request = cast(dict[str, object], json.loads(message))
        self.sent.append(request)
        method = request.get("method")
        request_id = request.get("id")
        if method == "client.capabilities":
            result: object = {"server_requests": [], "declines_not_shown": True}
        elif method == "session.list":
            result = {
                "sessions": [
                    {
                        "id": "stored-1",
                        "title": "Existing chat",
                        "preview": "hello",
                        "started_at": 1,
                        "message_count": 2,
                        "source": "desktop",
                    }
                ]
            }
        elif method == "session.resume":
            result = {
                "session_id": "runtime-1",
                "stored_session_id": "stored-1",
                "message_count": 2,
                "messages": [],
                "info": {},
                "status": "idle",
                "running": False,
            }
        elif method == "session.create":
            result = {
                "session_id": "runtime-new",
                "stored_session_id": "stored-new",
                "message_count": 0,
                "messages": [],
                "info": {"stored_session_id": "stored-new"},
            }
        elif method == "session.compress":
            result = {
                "status": "compressed",
                "removed": 4,
                "before_messages": 10,
                "after_messages": 6,
                "info": {"stored_session_id": "stored-compressed"},
                "messages": [],
            }
        elif method == "prompt.submit":
            result = {"status": self.prompt_status}
            if self.prompt_status == "streaming":
                result["user_row_id"] = 41
                admission_id = None
            else:
                admission_id = "admission-current"
                result["admission_id"] = admission_id
            for delta in ("shared ", "answer"):
                self.frames.append(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "event",
                            "params": {
                                "type": "message.delta",
                                "session_id": "runtime-1",
                                **(
                                    {"admission_id": admission_id}
                                    if admission_id is not None
                                    else {}
                                ),
                                "payload": {"text": delta},
                            },
                        }
                    )
                )
            self.frames.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "message.complete",
                            "session_id": "runtime-1",
                            **(
                                {"admission_id": admission_id}
                                if admission_id is not None
                                else {}
                            ),
                            "payload": {
                                "text": "shared answer",
                                "status": "complete",
                                **(
                                    {"admission_id": admission_id}
                                    if admission_id is not None
                                    else {}
                                ),
                                "persisted_turn": {
                                    "row_ids": [41, 42],
                                    "complete": True,
                                    "user_row_id": 41,
                                    "final_assistant_row_id": 42,
                                },
                            },
                        },
                    }
                )
            )
        else:
            raise AssertionError(f"unexpected method {method}")
        self.frames.append(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})
        )

    def recv(self, timeout: float | None = None) -> str:
        self.recv_timeouts.append(timeout)
        if not self.frames:
            raise TimeoutError("no fake frame")
        return self.frames.pop(0)

    def close(self) -> None:
        self.closed = True


def _gateway(socket: FakeSocket) -> HermesDesktopGateway:
    machine, locks = Path("C:/fake"), Path("C:/fake-locks")
    del machine, locks
    backend = DiscoveredBackend(
        record=parse_spawn_ledger(
            '[{"pid":1,"purpose":"serve","host":"127.0.0.1","port":8123}]'
        )[0],
        token="secret",
    )
    return HermesDesktopGateway(backend, connect_websocket=lambda *args, **kwargs: socket)


def _native_gateway(socket: FakeSocket) -> HermesDesktopGateway:
    socket.subprotocol = "hermes-gateway-v1"  # type: ignore[attr-defined]
    return HermesDesktopGateway(
        NativeGatewayBackend(FakeTicketBootstrap()),
        connect_websocket=lambda *args, **kwargs: socket,
    )


def test_native_gateway_dials_with_one_use_ticket_subprotocol_for_exact_profile() -> None:
    socket = FakeSocket()
    socket.subprotocol = "hermes-gateway-v1"  # type: ignore[attr-defined]
    bootstrap = FakeTicketBootstrap()
    captured: dict[str, object] = {}

    def connect(uri: str, **kwargs: object) -> FakeSocket:
        captured["uri"] = uri
        captured["kwargs"] = kwargs
        return socket

    gateway = HermesDesktopGateway(
        NativeGatewayBackend(bootstrap), connect_websocket=connect
    )

    rows = gateway.list_sessions("assistant-agent")

    assert rows[0]["id"] == "stored-1"
    assert captured["uri"] == "ws://127.0.0.1:9123/api/ws"
    options = cast(dict[str, object], captured["kwargs"])
    assert options["subprotocols"] == [
        "hermes-gateway-v1",
        "hermes-gateway-ticket.one-use-secret",
    ]
    assert bootstrap.requests == [("target", "assistant-agent")]


def test_native_gateway_rejects_missing_protocol_echo_before_rpc() -> None:
    socket = FakeSocket()
    socket.subprotocol = None  # type: ignore[attr-defined]
    gateway = HermesDesktopGateway(
        NativeGatewayBackend(FakeTicketBootstrap()),
        connect_websocket=lambda *args, **kwargs: socket,
    )

    with pytest.raises(DesktopGatewayError, match="protocol mismatch"):
        gateway.list_sessions("default")

    assert socket.closed is True
    assert socket.sent == []


def test_gateway_closes_socket_when_ready_frame_is_malformed() -> None:
    socket = FakeSocket()
    socket.frames[0] = "not-json"

    with pytest.raises(DesktopGatewayError, match="invalid JSON"):
        _gateway(socket).list_sessions("default")

    assert socket.closed is True


def test_gateway_closes_socket_when_capability_handshake_is_rejected() -> None:
    class CapabilityRejectedSocket(FakeSocket):
        def send(self, message: str) -> None:
            request = cast(dict[str, object], json.loads(message))
            if request.get("method") == "client.capabilities":
                self.sent.append(request)
                self.frames.append(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "error": {"code": -32602, "message": "unsupported"},
                        }
                    )
                )
                return
            super().send(message)

    socket = CapabilityRejectedSocket()

    with pytest.raises(DesktopGatewayError, match="rejected"):
        _gateway(socket).list_sessions("default")

    assert socket.closed is True


def test_list_sessions_uses_profile_scoped_native_contract() -> None:
    socket = FakeSocket()

    rows = _gateway(socket).list_sessions("research-agent")

    assert rows[0]["id"] == "stored-1"
    listing = next(item for item in socket.sent if item.get("method") == "session.list")
    assert cast(dict[str, object], listing["params"]) == {
        "profile": "research-agent",
        "limit": 20,
        "scope": "stored",
    }
    assert socket.closed is True


def test_send_resumes_existing_session_and_waits_for_matching_completion() -> None:
    socket = FakeSocket()
    progress: list[str] = []

    result = _gateway(socket).send_to_session(
        profile="research-agent",
        stored_session_id="stored-1",
        text="现在我们 RAG 数据库有哪些项目了？",
        on_progress=progress.append,
    )

    assert result.text == "shared answer"
    assert progress == ["shared ", "shared answer"]
    assert result.user_row_id == 41
    methods = [item.get("method") for item in socket.sent]
    assert methods == [
        "client.capabilities",
        "session.resume",
        "prompt.submit",
    ]
    resume = next(item for item in socket.sent if item.get("method") == "session.resume")
    assert cast(dict[str, object], resume["params"]) == {
        "session_id": "stored-1",
        "profile": "research-agent",
        "omit_messages": True,
    }
    submit = socket.sent[-1]
    submit_params = cast(dict[str, object], submit["params"])
    assert submit_params["surface"] == "voice-live"
    submission_id = submit_params["submission_id"]
    assert isinstance(submission_id, str)
    assert len(submission_id) == 32
    assert set(submission_id) <= set("0123456789abcdef")


@pytest.mark.parametrize("prompt_status", ("queued", "started"))
def test_send_waits_for_completion_after_native_admission_receipt(
    prompt_status: str,
) -> None:
    socket = FakeSocket(prompt_status=prompt_status)

    result = _native_gateway(socket).send_to_session(
        profile="research-agent",
        stored_session_id="stored-1",
        text="hello",
    )

    assert result.status == "complete"
    assert result.text == "shared answer"
    assert result.user_row_id is None
    assert socket.closed is True


def test_native_send_ignores_other_admission_events_before_its_completion() -> None:
    class InterleavedSocket(FakeSocket):
        def send(self, message: str) -> None:
            request = cast(dict[str, object], json.loads(message))
            super().send(message)
            if request.get("method") != "prompt.submit":
                return
            foreign = [
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "message.delta",
                            "session_id": "runtime-1",
                            "admission_id": "admission-other",
                            "payload": {"text": "wrong answer"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "message.complete",
                            "session_id": "runtime-1",
                            "admission_id": "admission-other",
                            "payload": {
                                "text": "wrong answer",
                                "status": "complete",
                                "admission_id": "admission-other",
                            },
                        },
                    }
                ),
            ]
            self.frames[:0] = foreign

    socket = InterleavedSocket(prompt_status="queued")
    progress: list[str] = []

    result = _native_gateway(socket).send_to_session(
        profile="research-agent",
        stored_session_id="stored-1",
        text="hello",
        on_progress=progress.append,
    )

    assert result.text == "shared answer"
    assert progress == ["shared ", "shared answer"]


def test_cancel_session_resumes_then_interrupts_exact_runtime() -> None:
    class InterruptSocket(FakeSocket):
        def send(self, message: str) -> None:
            request = cast(dict[str, object], json.loads(message))
            if request.get("method") == "session.interrupt":
                self.sent.append(request)
                self.frames.append(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": {"status": "interrupted", "interrupted": True},
                        }
                    )
                )
                return
            super().send(message)

    socket = InterruptSocket()

    _gateway(socket).cancel_session(
        profile="research-agent", stored_session_id="stored-1"
    )

    assert [item.get("method") for item in socket.sent] == [
        "client.capabilities",
        "session.resume",
        "session.interrupt",
    ]
    resume = next(item for item in socket.sent if item.get("method") == "session.resume")
    assert cast(dict[str, object], resume["params"]) == {
        "session_id": "stored-1",
        "profile": "research-agent",
        "omit_messages": True,
    }
    assert cast(dict[str, object], socket.sent[-1]["params"]) == {
        "session_id": "runtime-1",
        "profile": "research-agent",
    }


def test_send_stages_file_attachment_before_prompt(tmp_path: Path) -> None:
    class AttachmentSocket(FakeSocket):
        def send(self, message: str) -> None:
            request = cast(dict[str, object], json.loads(message))
            if request.get("method") == "file.attach":
                self.sent.append(request)
                self.frames.append(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": {
                                "attached": True,
                                "name": "note.txt",
                                "path": "C:/staged/note.txt",
                                "ref_path": ".hermes/attachments/note.txt",
                                "ref_text": "@file:.hermes/attachments/note.txt",
                                "uploaded": True,
                            },
                        }
                    )
                )
                return
            super().send(message)

    path = tmp_path / "note.txt"
    path.write_text("hello", encoding="utf-8")
    attachment = BridgeAttachment(
        attachment_id="att_" + "a" * 32,
        path=path,
        name="note.txt",
        media_type="text/plain",
        kind="text",
        bytes=5,
        sha256=hashlib.sha256(b"hello").hexdigest(),
    )
    socket = AttachmentSocket()

    result = _gateway(socket).send_to_session(
        profile="default",
        stored_session_id="stored-1",
        text="summarize",
        attachments=(attachment,),
    )

    assert result.status == "complete"
    assert [item.get("method") for item in socket.sent] == [
        "client.capabilities",
        "session.resume",
        "file.attach",
        "prompt.submit",
    ]
    submit = cast(dict[str, object], socket.sent[-1]["params"])
    assert submit["text"] == "summarize\n\n@file:.hermes/attachments/note.txt"


class BusySocket(FakeSocket):
    def send(self, message: str) -> None:
        request = cast(dict[str, object], json.loads(message))
        if request.get("method") == "session.resume":
            self.sent.append(request)
            self.frames.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "result": {
                            "session_id": "runtime-1",
                            "stored_session_id": "stored-1",
                            "message_count": 2,
                            "messages": [],
                            "info": {},
                            "status": "working",
                            "running": True,
                        },
                    }
                )
            )
            return
        super().send(message)


def test_send_refuses_busy_conversation_before_prompt_submit() -> None:
    socket = BusySocket()

    with pytest.raises(DesktopGatewayError, match="conversation is busy"):
        _gateway(socket).send_to_session(
            profile="default", stored_session_id="stored-1", text="hello"
        )

    assert "prompt.submit" not in [item.get("method") for item in socket.sent]


def test_create_session_keeps_legacy_idempotent_contract() -> None:
    socket = FakeSocket()

    result = _gateway(socket).create_session(
        profile="research-agent",
        idempotency_key="xiaozhi:new:request-1",
    )

    assert result.stored_session_id == "stored-new"
    assert result.runtime_session_id == "runtime-new"
    request = next(item for item in socket.sent if item.get("method") == "session.create")
    assert cast(dict[str, object], request["params"]) == {
        "profile": "research-agent",
        "source": "voice-live",
        "idempotency_key": "xiaozhi:new:request-1",
    }


def test_create_session_uses_native_request_id_contract() -> None:
    socket = FakeSocket()

    result = _native_gateway(socket).create_session(
        profile="research-agent",
        idempotency_key="xiaozhi:new:request-1",
    )

    assert result.stored_session_id == "stored-new"
    assert result.runtime_session_id == "runtime-new"
    request = next(item for item in socket.sent if item.get("method") == "session.create")
    assert cast(dict[str, object], request["params"]) == {
        "profile": "research-agent",
        "source": "gui",
        "request_id": "xiaozhi:new:request-1",
    }


def test_compress_session_resumes_then_uses_native_contract() -> None:
    socket = FakeSocket()

    result = _gateway(socket).compress_session(
        profile="research-agent",
        stored_session_id="stored-1",
    )

    assert result.status == "compressed"
    assert result.stored_session_id == "stored-compressed"
    assert result.removed_message_count == 4
    assert [item.get("method") for item in socket.sent] == [
        "client.capabilities",
        "session.resume",
        "session.compress",
    ]
    compress = socket.sent[-1]
    compress_params = cast(dict[str, object], compress["params"])
    assert compress_params["session_id"] == "runtime-1"
    resume = socket.sent[-2]
    assert cast(dict[str, object], resume["params"]) == {
        "session_id": "stored-1",
        "profile": "research-agent",
        "omit_messages": True,
    }
    assert any(timeout is not None and timeout > 660 for timeout in socket.recv_timeouts)


def test_compress_rejects_ambiguous_success_shape() -> None:
    class AmbiguousCompressSocket(FakeSocket):
        def send(self, message: str) -> None:
            request = cast(dict[str, object], json.loads(message))
            if request.get("method") == "session.compress":
                self.sent.append(request)
                self.frames.append(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": request["id"], "result": {}}
                    )
                )
                return
            super().send(message)

    with pytest.raises(DesktopGatewayError, match="ambiguous",) as raised:
        _gateway(AmbiguousCompressSocket()).compress_session(
            profile="default", stored_session_id="stored-1"
        )

    assert raised.value.outcome_unknown is True


@pytest.mark.parametrize("method", ("session.create", "session.compress"))
@pytest.mark.parametrize(
    "malformed",
    ("non_utf8", "invalid_json", "invalid_envelope", "missing_result"),
)
def test_mutating_lifecycle_rpc_marks_malformed_response_unknown(
    method: str, malformed: str
) -> None:
    class MalformedSocket(FakeSocket):
        def send(self, message: str) -> None:
            request = cast(dict[str, object], json.loads(message))
            if request.get("method") == method:
                self.sent.append(request)
                request_id = request["id"]
                frames: dict[str, str | bytes] = {
                    "non_utf8": b"\xff",
                    "invalid_json": "not-json",
                    "invalid_envelope": json.dumps(
                        {"jsonrpc": "1.0", "id": request_id, "result": {}}
                    ),
                    "missing_result": json.dumps(
                        {"jsonrpc": "2.0", "id": request_id}
                    ),
                }
                self.frames.append(frames[malformed])  # type: ignore[arg-type]
                return
            super().send(message)

    socket = MalformedSocket()
    gateway = _gateway(socket)
    with pytest.raises(DesktopGatewayError) as raised:
        if method == "session.create":
            gateway.create_session(profile="default", idempotency_key="request-1")
        else:
            gateway.compress_session(profile="default", stored_session_id="stored-1")

    assert raised.value.outcome_unknown is True
    assert [item.get("method") for item in socket.sent].count(method) == 1
