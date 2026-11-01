from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import BridgeAttachment
from agent_workflow_hub.xiaozhi_hermes_bridge.desktop_gateway import (
    DesktopGatewayError,
    DiscoveredBackend,
    HermesDesktopGateway,
    discover_desktop_backend,
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


class FakeSocket:
    def __init__(self) -> None:
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
            result = {"status": "streaming", "user_row_id": 41}
            self.frames.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {
                            "type": "message.complete",
                            "session_id": "runtime-1",
                            "payload": {
                                "text": "shared answer",
                                "status": "complete",
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


def test_list_sessions_uses_profile_scoped_native_contract() -> None:
    socket = FakeSocket()

    rows = _gateway(socket).list_sessions("research-agent")

    assert rows[0]["id"] == "stored-1"
    listing = next(item for item in socket.sent if item.get("method") == "session.list")
    assert cast(dict[str, object], listing["params"])["profile"] == "research-agent"
    assert socket.closed is True


def test_send_resumes_existing_session_and_waits_for_matching_completion() -> None:
    socket = FakeSocket()

    result = _gateway(socket).send_to_session(
        profile="research-agent",
        stored_session_id="stored-1",
        text="现在我们 RAG 数据库有哪些项目了？",
    )

    assert result.text == "shared answer"
    assert result.user_row_id == 41
    methods = [item.get("method") for item in socket.sent]
    assert methods == [
        "client.capabilities",
        "session.resume",
        "prompt.submit",
    ]
    submit = socket.sent[-1]
    assert cast(dict[str, object], submit["params"])["surface"] == "voice-live"


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


def test_create_session_uses_native_idempotent_contract() -> None:
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
        "close_on_disconnect": False,
        "idempotency_key": "xiaozhi:new:request-1",
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
    assert cast(dict[str, object], resume["params"])["omit_messages"] is True
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
