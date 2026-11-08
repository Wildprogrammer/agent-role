from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import BridgeAttachment
from agent_workflow_hub.xiaozhi_hermes_bridge.dsh_web_gateway import (
    DeepSeekHarnessWebGateway,
    DshGatewayError,
    validate_dsh_origin,
)


class FakeResponse:
    def __init__(self, status_code: int, value: object, *, headers=None) -> None:
        self.status_code = status_code
        self._value = value
        self.headers = headers or {}

    def json(self) -> object:
        return self._value


class FakeClient:
    def __init__(self) -> None:
        self.cookies = {"dsh": "session"}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.uploads: list[tuple[str, bytes]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        assert "secret-launch-token" in url
        return FakeResponse(302, None, headers={"location": "/"})

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        if "/api/session/uploadFileBinary?" in url:
            content = kwargs["content"]
            payload = b"".join(content)  # type: ignore[arg-type]
            self.uploads.append((url, payload))
            return FakeResponse(
                200,
                {
                    "ok": True,
                    "value": {
                        "receiptId": "receipt-1",
                        "file": {
                            "attachmentId": "sha256:abc",
                            "name": "note.txt",
                            "bytes": len(payload),
                        },
                    },
                },
            )
        body = kwargs["json"]
        assert isinstance(body, dict)
        method = str(body["method"])
        self.calls.append((method, body))
        values: dict[str, object] = {
            "agentPresets/list": {
                "presets": [
                    {"id": "assistant-agent", "name": "Role Agent", "isDefault": True},
                    {"id": "broken-agent", "broken": {"reason": "bad"}},
                ]
            },
            "session/list": {
                "items": [
                    {
                        "sessionId": "dsh-session-1",
                        "updatedAt": 10,
                        "running": False,
                        "blank": False,
                        "cwd": "C:/work",
                        "projections": {
                            "asOfSeq": 14,
                            "values": {
                                "agentPreset": "assistant-agent",
                                "title": "Shared chat",
                            },
                        },
                    }
                ]
            },
            "session/projections": {"asOfSeq": 14, "values": {}},
            "session/prompt": {"accepted": True},
            "session/cancel": {"accepted": True},
            "session/updateQueue": {"accepted": True},
        }
        if method == "session/page":
            value: object = {
                "records": [
                    {
                        "type": "event",
                        "event": {
                            "type": "user/message",
                            "seq": 11,
                            "data": {
                                "source": {
                                    "kind": "user",
                                    "rpcId": self._request_id(),
                                }
                            },
                        },
                    },
                    {
                        "type": "event",
                        "event": {
                            "type": "assistant/message",
                            "seq": 13,
                            "data": {
                                "message": {
                                    "content": [
                                        {"type": "reasoning", "text": "private"},
                                        {"type": "text", "text": "shared answer"},
                                    ]
                                }
                            },
                        },
                    },
                    {
                        "type": "event",
                        "event": {
                            "type": "turn/end",
                            "seq": 14,
                            "data": {"reason": {"kind": "completed"}},
                        },
                    },
                ]
            }
        else:
            value = values[method]
        return FakeResponse(
            200,
            {
                "type": "server-response",
                "rpcId": body["rpcId"],
                "result": {"ok": True, "value": value},
            },
        )

    def _request_id(self) -> str:
        prompt = next(body for method, body in self.calls if method == "session/prompt")
        return prompt["payload"]["args"]["request"]["requestId"]  # type: ignore[index]


def dsh_home(tmp_path: Path, version: str = "0.2.0-rc.2") -> Path:
    home = (tmp_path / ".dsh").resolve()
    package = home / "profiles/node_modules/@deepseek-ai/dsh/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": version}), encoding="utf-8")
    return home


def gateway(tmp_path: Path, client: FakeClient) -> DeepSeekHarnessWebGateway:
    return DeepSeekHarnessWebGateway(
        base_url="http://127.0.0.1:5000",
        launch_token="secret-launch-token",
        dsh_home=dsh_home(tmp_path),
        allowed_presets=("assistant-agent",),
        client=client,
        sleep=lambda _seconds: None,
    )


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:5000",
        "http://127.0.0.1:5000/path",
        "https://127.0.0.1:5000",
        "http://user@127.0.0.1:5000",
    ],
)
def test_origin_is_numeric_loopback_only(value: str) -> None:
    with pytest.raises(DshGatewayError):
        validate_dsh_origin(value)


def test_version_gate_runs_before_authentication(tmp_path: Path) -> None:
    with pytest.raises(DshGatewayError, match="unverified"):
        DeepSeekHarnessWebGateway(
            base_url="http://127.0.0.1:5000",
            launch_token="secret-launch-token",
            dsh_home=dsh_home(tmp_path, "0.2.1"),
            allowed_presets=("assistant-agent",),
            client=FakeClient(),
        )


def test_unsupported_expected_version_is_rejected_before_authentication(
    tmp_path: Path,
) -> None:
    with pytest.raises(DshGatewayError, match="not supported"):
        DeepSeekHarnessWebGateway(
            base_url="http://127.0.0.1:5000",
            launch_token="secret-launch-token",
            dsh_home=dsh_home(tmp_path, "0.2.2"),
            expected_version="0.2.2",
            allowed_presets=("assistant-agent",),
            client=FakeClient(),
        )


@pytest.mark.parametrize("version", ("0.2.0-rc.2", "0.2.1-rc.5"))
def test_each_supported_version_passes_the_exact_version_gate(
    tmp_path: Path, version: str
) -> None:
    adapter = DeepSeekHarnessWebGateway(
        base_url="http://127.0.0.1:5000",
        launch_token="secret-launch-token",
        dsh_home=dsh_home(tmp_path, version),
        expected_version=version,
        allowed_presets=("assistant-agent",),
        client=FakeClient(),
    )

    assert adapter.version == version


def test_lists_allowlisted_presets_and_their_sessions(tmp_path: Path) -> None:
    client = FakeClient()
    instance = gateway(tmp_path, client)

    assert instance.list_agents() == (
        {
            "host": "deepseek-harness",
            "id": "assistant-agent",
            "name": "Role Agent",
            "default": True,
            "available": True,
        },
    )
    assert instance.list_sessions("assistant-agent") == (
        {
            "id": "dsh-session-1",
            "title": "Shared chat",
            "updated_at": 10,
            "message_count": None,
            "running": False,
            "blank": False,
            "cwd": "C:/work",
        },
    )


def test_prompt_waits_for_the_correlated_durable_turn(tmp_path: Path) -> None:
    client = FakeClient()
    instance = gateway(tmp_path, client)
    progress: list[str] = []

    result = instance.send_to_session(
        profile="assistant-agent",
        stored_session_id="dsh-session-1",
        text="hello",
        on_progress=progress.append,
    )

    assert result.status == "complete"
    assert result.stored_session_id == "dsh-session-1"
    assert result.runtime_session_id == "dsh-session-1"
    assert result.user_row_id == 11
    assert result.text == "shared answer"
    assert progress == ["shared answer"]
    prompt = next(body for method, body in client.calls if method == "session/prompt")
    request = prompt["payload"]["args"]["request"]  # type: ignore[index]
    assert request["mode"] == "queue"
    assert request["clientTimeZone"] == "Asia/Shanghai"


def test_cancel_session_uses_native_dsh_contract(tmp_path: Path) -> None:
    client = FakeClient()
    instance = gateway(tmp_path, client)

    status = instance.cancel_session(
        profile="assistant-agent", stored_session_id="dsh-session-1"
    )

    assert status == "requested"
    method, body = client.calls[-1]
    assert method == "session/cancel"
    assert body["payload"]["args"]["request"] == {"sessionId": "dsh-session-1"}


def test_prompt_streams_file_then_uses_receipt(tmp_path: Path) -> None:
    client = FakeClient()
    instance = gateway(tmp_path, client)
    path = tmp_path / "note.txt"
    path.write_bytes(b"hello")
    attachment = BridgeAttachment(
        attachment_id="att_" + "a" * 32,
        path=path,
        name="note.txt",
        media_type="text/plain",
        kind="text",
        bytes=5,
        sha256=hashlib.sha256(b"hello").hexdigest(),
    )

    result = instance.send_to_session(
        profile="assistant-agent",
        stored_session_id="dsh-session-1",
        text="summarize",
        attachments=(attachment,),
    )

    assert result.status == "complete"
    assert client.uploads[0][1] == b"hello"
    assert "sessionId=dsh-session-1" in client.uploads[0][0]
    prompt = next(body for method, body in client.calls if method == "session/prompt")
    assert prompt["payload"]["args"]["request"]["content"] == [  # type: ignore[index]
        {"type": "text", "text": "summarize"},
        {"type": "file", "receiptId": "receipt-1"},
    ]


def test_remove_queued_item_uses_exact_session_and_item_ids(tmp_path: Path) -> None:
    client = FakeClient()
    instance = gateway(tmp_path, client)

    instance.remove_queued_item(session_id="dsh-session-1", item_id="queued-1")

    update = next(body for method, body in client.calls if method == "session/updateQueue")
    assert update["payload"]["args"]["request"] == {  # type: ignore[index]
        "sessionId": "dsh-session-1",
        "itemId": "queued-1",
        "action": {"kind": "remove"},
    }


def test_turn_correlation_stops_at_the_first_end_after_our_user_event() -> None:
    def record(event_type: str, seq: int, data: object) -> dict[str, object]:
        return {"event": {"type": event_type, "seq": seq, "data": data}}

    records = (
        record(
            "user/message",
            1,
            {"source": {"kind": "user", "rpcId": "ours"}},
        ),
        record(
            "assistant/message",
            2,
            {"message": {"content": [{"type": "text", "text": "our answer"}]}},
        ),
        record("turn/end", 3, {"reason": {"kind": "completed"}}),
        record(
            "assistant/message",
            4,
            {"message": {"content": [{"type": "text", "text": "later answer"}]}},
        ),
        record("turn/end", 5, {"reason": {"kind": "cancelled"}}),
    )

    assert DeepSeekHarnessWebGateway._turn_result(
        records, request_id="ours"
    ) == (1, "completed", "our answer", None, None)


def test_failed_turn_preserves_bounded_safe_reason_detail(tmp_path: Path) -> None:
    class FailedClient(FakeClient):
        def post(self, url: str, **kwargs: object) -> FakeResponse:
            response = super().post(url, **kwargs)
            body = kwargs["json"]
            if isinstance(body, dict) and body.get("method") == "session/page":
                reason = response._value["result"]["value"]["records"][-1]["event"][
                    "data"
                ]["reason"]
                reason["kind"] = "failed"
                reason["error"] = {
                    "code": "PROVIDER_ERROR",
                    "message": "provider\nrequest failed",
                }
            return response

    result = gateway(tmp_path, FailedClient()).send_to_session(
        profile="assistant-agent", stored_session_id="dsh-session-1", text="hello"
    )

    assert result.status == "failed"
    assert result.error_code == "PROVIDER_ERROR"
    assert result.error_message == "provider request failed"


def test_create_transport_failure_is_outcome_unknown(tmp_path: Path) -> None:
    class CreateFailureClient(FakeClient):
        def post(self, url: str, **kwargs: object) -> FakeResponse:
            body = kwargs["json"]
            if isinstance(body, dict) and body.get("method") == "session/create":
                raise TimeoutError("synthetic timeout")
            return super().post(url, **kwargs)

    instance = gateway(tmp_path, CreateFailureClient())

    with pytest.raises(DshGatewayError) as raised:
        instance.create_session(profile="assistant-agent", idempotency_key="request-1")

    assert raised.value.outcome_unknown is True


@pytest.mark.parametrize(
    ("reason", "expected"),
    (
        ("completed", "complete"),
        ("aborted", "cancelled"),
        ("blocked", "failed"),
        ("max-tokens", "failed"),
        ("forked", "interrupted"),
        ("future-unknown", "failed"),
    ),
)
def test_dsh_turn_reasons_map_to_public_statuses(
    tmp_path: Path, reason: str, expected: str
) -> None:
    class ReasonClient(FakeClient):
        def post(self, url: str, **kwargs: object) -> FakeResponse:
            response = super().post(url, **kwargs)
            body = kwargs["json"]
            if isinstance(body, dict) and body.get("method") == "session/page":
                value = response._value
                value["result"]["value"]["records"][-1]["event"]["data"]["reason"][
                    "kind"
                ] = reason
            return response

    result = gateway(tmp_path, ReasonClient()).send_to_session(
        profile="assistant-agent", stored_session_id="dsh-session-1", text="hello"
    )

    assert result.status == expected


def test_malformed_successful_create_is_outcome_unknown(tmp_path: Path) -> None:
    class MalformedCreateClient(FakeClient):
        def post(self, url: str, **kwargs: object) -> FakeResponse:
            body = kwargs["json"]
            if isinstance(body, dict) and body.get("method") == "session/create":
                return FakeResponse(
                    200,
                    {
                        "type": "server-response",
                        "rpcId": body["rpcId"],
                        "result": {"ok": True, "value": None},
                    },
                )
            return super().post(url, **kwargs)

    instance = gateway(tmp_path, MalformedCreateClient())

    with pytest.raises(DshGatewayError) as raised:
        instance.create_session(profile="assistant-agent", idempotency_key="request-1")

    assert raised.value.outcome_unknown is True
    assert "xiaozhi-" in str(raised.value)
