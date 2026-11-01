from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import (
    BridgeConfig,
    BridgeRequest,
    ProfileRoute,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.service import (
    BridgeRejected,
    BridgeService,
    HermesRunResult,
    HermesRunsTransport,
    HermesTransportError,
)


def make_config(
    *, dispatch_enabled: bool = True, require_confirmation: bool = True
) -> BridgeConfig:
    return BridgeConfig.from_mapping(
        {
            "schema_version": "1.0",
            "trusted_caller_id": "xiaozhi-backend",
            "default_profile": "default",
            "profiles": [
                {
                    "name": "default",
                    "api_server_base_url": "http://127.0.0.1:9119/p/default",
                    "api_key_env": "DEFAULT_KEY",
                },
                {
                    "name": "specialist",
                    "api_server_base_url": "http://127.0.0.1:9119/p/specialist",
                    "api_key_env": "SPECIALIST_KEY",
                },
            ],
            "dispatch_enabled": dispatch_enabled,
            "require_confirmation": require_confirmation,
            "request_timeout_seconds": 5,
            "poll_interval_seconds": 1,
            "max_polls": 3,
            "max_input_chars": 80,
        }
    )


def request(operation: str, **fields: object) -> BridgeRequest:
    return BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": fields.pop("caller_id", "xiaozhi-backend"),
            "session_id": fields.pop("session_id", "session-a"),
            "operation": operation,
            **fields,
        }
    )


@dataclass
class RecordingTransport:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def run(self, route: ProfileRoute, *, input: str, session_id: str) -> HermesRunResult:
        self.calls.append((route.name, input, session_id))
        return HermesRunResult(status="completed", run_id="run-1", output="done")


def test_profile_state_is_allowlisted_and_isolated_by_session() -> None:
    transport = RecordingTransport()
    service = BridgeService(make_config(), transport)

    switched = service.handle(request("switch_profile", profile="specialist"))
    other = service.handle(request("list_profiles", session_id="session-b"))

    assert switched["active_profile"] == "specialist"
    assert other["active_profile"] == "default"
    with pytest.raises(BridgeRejected, match="allowlisted"):
        service.handle(request("switch_profile", profile="unknown"))
    assert transport.calls == []


def test_untrusted_caller_is_rejected_before_state_or_transport() -> None:
    transport = RecordingTransport()
    service = BridgeService(make_config(), transport)

    with pytest.raises(BridgeRejected, match="trusted stdio parent"):
        service.handle(
            request("dispatch", caller_id="untrusted", input="hello", confirmed=True)
        )

    assert transport.calls == []


def test_session_state_has_a_fixed_capacity() -> None:
    service = BridgeService(make_config(), RecordingTransport(), max_sessions=1)
    service.handle(request("switch_profile", session_id="one", profile="specialist"))

    with pytest.raises(BridgeRejected, match="session limit"):
        service.handle(request("switch_profile", session_id="two", profile="specialist"))


def test_dispatch_fails_closed_before_transport() -> None:
    transport = RecordingTransport()
    service = BridgeService(make_config(dispatch_enabled=False), transport)
    with pytest.raises(BridgeRejected, match="disabled"):
        service.handle(request("dispatch", input="hello", confirmed=True))

    service = BridgeService(make_config(), transport)
    with pytest.raises(BridgeRejected, match="confirmed=true"):
        service.handle(request("dispatch", input="hello", confirmed=False))
    with pytest.raises(BridgeRejected, match="max_input_chars"):
        service.handle(request("dispatch", input="x" * 81, confirmed=True))
    assert transport.calls == []


def test_dispatch_routes_to_current_profile_without_auto_approval() -> None:
    transport = RecordingTransport()
    service = BridgeService(make_config(), transport)
    service.handle(request("switch_profile", profile="specialist"))

    result = service.handle(request("dispatch", input="summarize", confirmed=True))

    assert result == {
        "schema_version": "1.0",
        "operation": "dispatch",
        "session_id": "session-a",
        "status": "completed",
        "active_profile": "specialist",
        "run_id": "run-1",
        "output": "done",
    }
    assert transport.calls == [("specialist", "summarize", "session-a")]


class FakeResponse:
    def __init__(self, data: object, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://127.0.0.1")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("bad response", request=request, response=response)

    def json(self) -> object:
        return self._data


class FakeClient:
    def __init__(self, polls: list[object]) -> None:
        self.polls = polls
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return FakeResponse({"run_id": "r-7"})

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return FakeResponse(self.polls.pop(0))


class FailingPollClient(FakeClient):
    def get(self, url: str, **kwargs: object) -> FakeResponse:
        request = httpx.Request("GET", url)
        raise httpx.ConnectError("synthetic poll failure", request=request)


def test_http_transport_uses_bearer_key_and_stops_at_approval() -> None:
    config = make_config()
    client = FakeClient(
        [
            {"status": "running"},
            {"status": "waiting_for_approval", "output": {"reason": "confirm"}},
        ]
    )
    sleeps: list[float] = []
    transport = HermesRunsTransport(
        config,
        environment={"DEFAULT_KEY": "synthetic-key"},
        client=client,
        sleeper=sleeps.append,
    )

    result = transport.run(
        config.profiles[0], input="do work", session_id="session-a"
    )

    assert result.status == "waiting_for_approval"
    assert len(client.calls) == 3
    assert client.calls[0][1].endswith("/p/default/v1/runs")
    assert client.calls[0][2]["json"] == {
        "input": "do work",
        "session_id": "session-a",
    }
    assert client.calls[0][2]["headers"] == {
        "Authorization": "Bearer synthetic-key"
    }
    assert sleeps == [1.0]


def test_http_transport_never_invents_or_echoes_a_missing_key() -> None:
    config = make_config()
    with pytest.raises(HermesTransportError) as raised:
        HermesRunsTransport(
            config,
            environment={},
            client=FakeClient([]),
        ).run(config.profiles[0], input="hello", session_id="session-a")

    assert "DEFAULT_KEY" in str(raised.value)
    assert "Bearer" not in str(raised.value)


def test_http_transport_disables_environment_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def factory(**kwargs: object) -> FakeClient:
        captured.update(kwargs)
        return FakeClient([])

    monkeypatch.setattr(httpx, "Client", factory)
    HermesRunsTransport(make_config(), environment={"DEFAULT_KEY": "synthetic"})

    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


def test_poll_failure_preserves_run_id_as_outcome_unknown() -> None:
    config = make_config()
    transport = HermesRunsTransport(
        config,
        environment={"DEFAULT_KEY": "synthetic"},
        client=FailingPollClient([]),
    )

    with pytest.raises(HermesTransportError) as raised:
        transport.run(config.profiles[0], input="hello", session_id="session-a")

    assert raised.value.outcome_unknown is True
    assert raised.value.run_id == "r-7"


def test_unknown_status_is_outcome_unknown_and_run_id_is_path_encoded() -> None:
    config = make_config()
    client = FakeClient([{"status": "mystery"}])
    transport = HermesRunsTransport(
        config,
        environment={"DEFAULT_KEY": "synthetic"},
        client=client,
    )
    client.post = lambda url, **kwargs: FakeResponse({"run_id": "folder/run"})  # type: ignore[method-assign]

    with pytest.raises(HermesTransportError) as raised:
        transport.run(config.profiles[0], input="hello", session_id="session-a")

    assert raised.value.outcome_unknown is True
    assert raised.value.run_id == "folder/run"
    assert client.calls[-1][1].endswith("/folder%2Frun")
