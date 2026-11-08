from __future__ import annotations

import io
import json
import hashlib
from pathlib import Path

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.cli import (
    _validate_attachment_files,
    main,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import (
    BridgeAttachment,
    BridgeRequest,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.service import BridgeRejected


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def config_value(*, enabled: bool) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "trusted_caller_id": "xiaozhi-backend",
        "default_profile": "default",
        "profiles": [
            {
                "name": "default",
                "api_server_base_url": "http://127.0.0.1:9119/p/default",
                "api_key_env": "DEFAULT_KEY",
            }
        ],
        "dispatch_enabled": enabled,
        "require_confirmation": True,
        "request_timeout_seconds": 5,
        "poll_interval_seconds": 1,
        "max_polls": 2,
        "max_input_chars": 100,
    }


def attachment_request(path: Path, *, digest: str) -> BridgeRequest:
    attachment = BridgeAttachment(
        attachment_id="att_0123456789abcdef",
        path=path,
        name=path.name,
        media_type="text/plain",
        kind="text",
        bytes=path.stat().st_size,
        sha256=digest,
    )
    return BridgeRequest(
        schema_version="1.0",
        caller_id="xiaozhi-backend",
        session_id="test-session",
        operation="dispatch_bound_voice",
        profile="default",
        input="请查看附件",
        device_id="xiaoi-mcp",
        stored_session_id="stored-session",
        host="hermes",
        binding_revision=1,
        attachments=(attachment,),
    )


def test_attachment_validation_rejects_path_outside_staging_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    request = attachment_request(
        outside,
        digest=hashlib.sha256(outside.read_bytes()).hexdigest(),
    )

    with pytest.raises(BridgeRejected, match="outside the staging root"):
        _validate_attachment_files(
            request,
            {"XIAOAI_MCP_ATTACHMENT_ROOT": str(root)},
        )


def test_attachment_validation_rejects_changed_content(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    root.mkdir()
    path = root / "note.txt"
    path.write_text("original", encoding="utf-8")
    request = attachment_request(path, digest="0" * 64)

    with pytest.raises(BridgeRejected, match="content changed"):
        _validate_attachment_files(
            request,
            {"XIAOAI_MCP_ATTACHMENT_ROOT": str(root)},
        )


def test_doctor_reports_presence_without_printing_secret(tmp_path: Path) -> None:
    config = write_json(tmp_path / "config.json", config_value(enabled=True))
    stdout = io.StringIO()

    exit_code = main(
        ["doctor", "--config", str(config.resolve())],
        environment={"DEFAULT_KEY": "do-not-print-this"},
        stdout=stdout,
    )

    result = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert result["status"] == "configuration_ready_live_not_checked"
    assert result["live_readiness_checked"] is False
    assert result["config_sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert result["bound_voice_dispatch_mode"] == "confirm"
    assert result["profiles"][0]["api_key_present"] is True
    assert "do-not-print-this" not in stdout.getvalue()


def test_desktop_doctor_reports_dsh_initialization_failure_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge import cli
    from agent_workflow_hub.xiaozhi_hermes_bridge.desktop_gateway import (
        GatewayBackendDescription,
    )
    from agent_workflow_hub.xiaozhi_hermes_bridge.dsh_web_gateway import (
        DshGatewayError,
    )

    mapping = config_value(enabled=True)
    mapping["deepseek_harness"] = {
        "base_url": "http://127.0.0.1:3080",
        "launch_token_env": "DSH_TOKEN",
        "dsh_home": str(tmp_path.resolve()),
        "expected_version": "0.2.1-rc.5",
        "allowed_presets": ["assistant-agent"],
    }
    config = write_json(tmp_path / "config.json", mapping)

    class Backend:
        def describe(self, profile: str = "default") -> GatewayBackendDescription:
            assert profile == "default"
            return GatewayBackendDescription(
                transport="native-gateway-ticket",
                host="127.0.0.1",
                port=9123,
                pid=None,
                credential_mode="one-use-ticket",
            )

    class Gateway:
        backend = Backend()

    monkeypatch.setattr(cli, "_desktop_gateway", lambda _environment: Gateway())
    monkeypatch.setattr(
        cli,
        "_deepseek_gateway",
        lambda *_args: (_ for _ in ()).throw(DshGatewayError("offline")),
    )
    stdout = io.StringIO()

    exit_code = main(
        ["desktop-doctor", "--config", str(config.resolve())],
        environment={"DSH_TOKEN": "not-printed"},
        stdout=stdout,
    )

    result = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert result["status"] == "ready"
    assert result["backend_transport"] == "native-gateway-ticket"
    assert result["deepseek_harness"] == {
        "configured": True,
        "status": "unavailable",
        "version": "0.2.1-rc.5",
    }
    assert "not-printed" not in stdout.getvalue()


def test_simulation_never_calls_hermes(tmp_path: Path) -> None:
    config = write_json(tmp_path / "config.json", config_value(enabled=True))
    request = write_json(
        tmp_path / "request.json",
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "test-session",
            "operation": "dispatch",
            "input": "hello",
            "confirmed": True,
        },
    )
    stdout = io.StringIO()

    exit_code = main(
        [
            "simulate",
            "--config",
            str(config.resolve()),
            "--request",
            str(request.resolve()),
        ],
        environment={},
        stdout=stdout,
    )

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["status"] == "simulated"


def test_desktop_cancel_session_uses_managed_dsh_gateway(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge import cli

    mapping = config_value(enabled=True)
    mapping["deepseek_harness"] = {
        "base_url": "http://127.0.0.1:3080",
        "launch_token_env": "DSH_TOKEN",
        "dsh_home": str(tmp_path.resolve()),
        "expected_version": "0.2.0-rc.2",
        "allowed_presets": ["conversation-agent"],
    }
    config = write_json(tmp_path / "config.json", mapping)
    cancelled: list[str] = []

    class Gateway:
        def cancel_session(self, session_id: str) -> None:
            cancelled.append(session_id)

    monkeypatch.setattr(cli, "_deepseek_gateway", lambda *_args: Gateway())
    stdout = io.StringIO()

    exit_code = main(
        [
            "desktop-cancel-session",
            "--config",
            str(config.resolve()),
            "--session-id",
            "session-1",
        ],
        environment={"DSH_TOKEN": "not-printed"},
        stdout=stdout,
    )

    assert exit_code == 0
    assert cancelled == ["session-1"]
    assert json.loads(stdout.getvalue()) == {
        "cancelled": True,
        "schema_version": "1.0",
        "session_id": "session-1",
        "status": "ok",
    }
    assert "not-printed" not in stdout.getvalue()


def test_desktop_remove_queued_item_uses_managed_dsh_gateway(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge import cli

    mapping = config_value(enabled=True)
    mapping["deepseek_harness"] = {
        "base_url": "http://127.0.0.1:3080",
        "launch_token_env": "DSH_TOKEN",
        "dsh_home": str(tmp_path.resolve()),
        "expected_version": "0.2.0-rc.2",
        "allowed_presets": ["conversation-agent"],
    }
    config = write_json(tmp_path / "config.json", mapping)
    removed: list[tuple[str, str]] = []

    class Gateway:
        def remove_queued_item(self, *, session_id: str, item_id: str) -> None:
            removed.append((session_id, item_id))

    monkeypatch.setattr(cli, "_deepseek_gateway", lambda *_args: Gateway())
    stdout = io.StringIO()

    exit_code = main(
        [
            "desktop-remove-queued-item",
            "--config",
            str(config.resolve()),
            "--session-id",
            "session-1",
            "--item-id",
            "queued-1",
        ],
        environment={"DSH_TOKEN": "not-printed"},
        stdout=stdout,
    )

    assert exit_code == 0
    assert removed == [("session-1", "queued-1")]
    assert json.loads(stdout.getvalue()) == {
        "item_id": "queued-1",
        "removed": True,
        "schema_version": "1.0",
        "session_id": "session-1",
        "status": "ok",
    }
    assert "not-printed" not in stdout.getvalue()


def test_serve_requires_the_exact_confirmed_config_digest(tmp_path: Path) -> None:
    config = write_json(tmp_path / "config.json", config_value(enabled=False))
    stdout = io.StringIO()

    exit_code = main(
        [
            "serve-stdio",
            "--config",
            str(config.resolve()),
            "--confirmed-config-sha256",
            "0" * 64,
        ],
        stdin=io.StringIO(""),
        stdout=stdout,
    )

    assert exit_code == 2
    assert json.loads(stdout.getvalue())["status"] == "rejected"


def test_stdio_rejects_bad_line_and_continues(tmp_path: Path) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge.cli import _serve_stdio
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import read_config

    config_path = write_json(tmp_path / "config.json", config_value(enabled=False))
    stdin = io.StringIO(
        "not-json\n"
        + json.dumps(
            {
                "schema_version": "1.0",
                "caller_id": "xiaozhi-backend",
                "session_id": "s-1",
                "operation": "list_profiles",
            }
        )
        + "\n"
    )
    stdout = io.StringIO()

    assert (
        _serve_stdio(
            read_config(config_path.resolve()),
            environment={},
            stdin=stdin,
            stdout=stdout,
        )
        == 0
    )
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [item["status"] for item in responses] == ["rejected", "ok"]


def test_stdio_rejects_oversized_line_and_continues(tmp_path: Path) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge.cli import (
        _MAX_REQUEST_LINE_CHARS,
        _serve_stdio,
    )
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import read_config

    config_path = write_json(tmp_path / "config.json", config_value(enabled=False))
    valid = json.dumps(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "s-1",
            "operation": "list_profiles",
        }
    )
    stdout = io.StringIO()
    _serve_stdio(
        read_config(config_path.resolve()),
        environment={},
        stdin=io.StringIO("x" * (_MAX_REQUEST_LINE_CHARS + 10) + "\n" + valid + "\n"),
        stdout=stdout,
    )

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [item["status"] for item in responses] == ["rejected", "ok"]


def test_stdio_contains_non_finite_transport_output_and_continues(tmp_path: Path) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge.cli import _serve_stdio
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import read_config
    from agent_workflow_hub.xiaozhi_hermes_bridge.service import HermesRunResult

    class NonFiniteTransport:
        def run(self, route: object, *, input: str, session_id: str) -> HermesRunResult:
            return HermesRunResult(status="completed", run_id="run-1", output=float("nan"))

    config_path = write_json(tmp_path / "config.json", config_value(enabled=True))
    dispatch = json.dumps(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "s-1",
            "operation": "dispatch",
            "input": "hello",
            "confirmed": True,
        }
    )
    listing = json.dumps(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "s-1",
            "operation": "list_profiles",
        }
    )
    stdout = io.StringIO()
    _serve_stdio(
        read_config(config_path.resolve()),
        environment={},
        stdin=io.StringIO(dispatch + "\n" + listing + "\n"),
        stdout=stdout,
        transport=NonFiniteTransport(),
    )

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [item["status"] for item in responses] == ["failed", "ok"]
    assert responses[0]["run_id"] == "run-1"


def test_outcome_unknown_response_keeps_run_id() -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge.cli import _error_response
    from agent_workflow_hub.xiaozhi_hermes_bridge.service import HermesTransportError

    response = _error_response(
        HermesTransportError(
            "poll result unknown", outcome_unknown=True, run_id="run-9"
        )
    )

    assert response["status"] == "outcome_unknown"
    assert response["run_id"] == "run-9"


def test_stdio_shared_binding_uses_device_id_across_socket_sessions(tmp_path: Path) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge.binding_store import BindingStore
    from agent_workflow_hub.xiaozhi_hermes_bridge.cli import _serve_stdio
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import read_config
    from agent_workflow_hub.xiaozhi_hermes_bridge.shared_service import (
        SharedConversationService,
    )

    class Gateway:
        def list_sessions(self, profile: str, *, limit: int = 20):
            return ({"id": "stored-1", "title": "Existing chat"},)

        def send_to_session(self, *, profile: str, stored_session_id: str, text: str):
            raise AssertionError("send must not run")

    config_path = write_json(tmp_path / "config.json", config_value(enabled=False))
    config = read_config(config_path.resolve())
    shared = SharedConversationService(
        config,
        BindingStore((tmp_path / "bindings.json").resolve()),
        Gateway(),
    )
    requests = [
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket-1",
            "operation": "bind_session",
            "device_id": "esp32-s3",
            "profile": "default",
            "stored_session_id": "stored-1",
        },
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket-2",
            "operation": "show_binding",
            "device_id": "esp32-s3",
        },
    ]
    stdout = io.StringIO()

    _serve_stdio(
        config,
        environment={},
        stdin=io.StringIO("".join(json.dumps(item) + "\n" for item in requests)),
        stdout=stdout,
        shared=shared,
    )

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[0]["bound"] is True
    assert responses[1]["stored_session_id"] == "stored-1"
    assert responses[1]["session_id"] == "socket-2"


def test_stdio_progress_requires_explicit_wire_capability(tmp_path: Path) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge.binding_store import BindingStore
    from agent_workflow_hub.xiaozhi_hermes_bridge.cli import _serve_stdio
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import read_config
    from agent_workflow_hub.xiaozhi_hermes_bridge.desktop_gateway import (
        SharedTurnResult,
    )
    from agent_workflow_hub.xiaozhi_hermes_bridge.shared_service import (
        SharedConversationService,
    )

    class Gateway:
        def list_sessions(self, profile, *, limit=20):
            return ({"id": "stored-1", "title": "Existing"},)

        def send_to_session(
            self,
            *,
            profile,
            stored_session_id,
            text,
            on_progress=None,
        ):
            if on_progress is not None:
                on_progress("累计正文。")
            return SharedTurnResult(
                status="complete",
                stored_session_id=stored_session_id,
                runtime_session_id=stored_session_id,
                text="累计正文。最终句。",
                user_row_id=1,
            )

    mapping = config_value(enabled=True)
    mapping["bound_voice_dispatch_mode"] = "direct"
    config_path = write_json(tmp_path / "config.json", mapping)
    config = read_config(config_path.resolve())
    shared = SharedConversationService(
        config,
        BindingStore((tmp_path / "bindings.json").resolve()),
        Gateway(),
    )
    binding = shared.bind(
        device_id="esp32-s3",
        profile="default",
        stored_session_id="stored-1",
    )

    def request(session_id: str, *, stream_progress: bool | None = None):
        value = {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": session_id,
            "operation": "dispatch_bound_voice",
            "device_id": "esp32-s3",
            "input": "问题",
            "host": "hermes",
            "profile": "default",
            "stored_session_id": "stored-1",
            "binding_revision": binding.revision,
        }
        if stream_progress is not None:
            value["stream_progress"] = stream_progress
        return json.dumps(value)

    stdout = io.StringIO()
    _serve_stdio(
        config,
        environment={},
        stdin=io.StringIO(
            request("legacy-session")
            + "\n"
            + request("stream-session", stream_progress=True)
            + "\n"
        ),
        stdout=stdout,
        shared=shared,
    )

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [item["status"] for item in responses] == [
        "completed",
        "progress",
        "completed",
    ], responses
    assert responses[0]["session_id"] == "legacy-session"
    assert responses[1]["session_id"] == "stream-session"
    assert responses[2]["session_id"] == "stream-session"
    assert responses[1]["output"] == "累计正文。"
    assert responses[2]["output"] == "累计正文。最终句。"


def test_shared_dispatch_failure_exposes_dsh_reason_without_losing_status() -> None:
    from types import SimpleNamespace

    from agent_workflow_hub.xiaozhi_hermes_bridge.cli import _handle_shared_request
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import BridgeRequest
    from agent_workflow_hub.xiaozhi_hermes_bridge.desktop_gateway import SharedTurnResult

    class Service:
        def send(
            self,
            *,
            device_id: str,
            text: str,
            confirmed: bool,
            voice_direct: bool,
            expected_host: str,
            expected_profile: str,
            expected_stored_session_id: str,
            expected_revision: int,
        ):
            assert (device_id, text, confirmed) == ("esp32-s3", "hello", True)
            assert voice_direct is False
            assert (
                expected_host,
                expected_profile,
                expected_stored_session_id,
                expected_revision,
            ) == ("deepseek-harness", "assistant-agent", "dsh-session-1", 2)
            return SharedTurnResult(
                stored_session_id="dsh-session-1",
                runtime_session_id="dsh-session-1",
                status="failed",
                text="",
                user_row_id=17,
                error_code="PROVIDER_ERROR",
                error_message="provider request failed",
            )

        def binding(self, device_id: str):
            assert device_id == "esp32-s3"
            return SimpleNamespace(host="deepseek-harness")

    request = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket-1",
            "operation": "dispatch_shared",
            "device_id": "esp32-s3",
            "input": "hello",
            "confirmed": True,
            "host": "deepseek-harness",
            "profile": "assistant-agent",
            "stored_session_id": "dsh-session-1",
            "binding_revision": 2,
        }
    )

    response = _handle_shared_request(Service(), request)  # type: ignore[arg-type]

    assert response["status"] == "failed"
    assert response["host"] == "deepseek-harness"
    assert response["error_code"] == "PROVIDER_ERROR"
    assert response["error"] == "provider request failed"


def test_stdio_rejects_untrusted_caller_before_shared_operation(tmp_path: Path) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge.binding_store import BindingStore
    from agent_workflow_hub.xiaozhi_hermes_bridge.cli import _serve_stdio
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import read_config
    from agent_workflow_hub.xiaozhi_hermes_bridge.shared_service import SharedConversationService

    class Gateway:
        def list_sessions(self, profile: str, *, limit: int = 20):
            raise AssertionError("untrusted caller must not reach a gateway")

    config_path = write_json(tmp_path / "config.json", config_value(enabled=True))
    config = read_config(config_path.resolve())
    shared = SharedConversationService(
        config,
        BindingStore((tmp_path / "bindings.json").resolve()),
        Gateway(),
    )
    stdout = io.StringIO()

    _serve_stdio(
        config,
        environment={},
        stdin=io.StringIO(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "caller_id": "untrusted-parent",
                    "session_id": "socket-1",
                    "operation": "show_binding",
                    "device_id": "esp32-s3",
                }
            )
            + "\n"
        ),
        stdout=stdout,
        shared=shared,
    )

    assert json.loads(stdout.getvalue())["status"] == "rejected"


def test_deepseek_initialization_failure_does_not_block_hermes(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge import cli
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import BridgeConfig
    from agent_workflow_hub.xiaozhi_hermes_bridge.dsh_web_gateway import DshGatewayError

    mapping = config_value(enabled=True)
    mapping["deepseek_harness"] = {
        "base_url": "http://127.0.0.1:8765",
        "launch_token_env": "DSH_TOKEN",
        "dsh_home": str(tmp_path.resolve()),
        "expected_version": "0.2.0-rc.2",
        "allowed_presets": ["assistant-agent"],
    }
    config = BridgeConfig.from_mapping(mapping)

    class HermesGateway:
        def list_sessions(self, profile: str, *, limit: int = 20):
            return ()

    monkeypatch.setattr(cli, "_desktop_gateway", lambda _environment: HermesGateway())
    monkeypatch.setattr(
        cli,
        "_deepseek_gateway",
        lambda _config, _environment: (_ for _ in ()).throw(
            DshGatewayError("stale launch token")
        ),
    )

    service = cli._shared_service(
        config,
        {"DSH_TOKEN": "stale"},
        (tmp_path / "bindings.json").resolve(),
    )
    agents = service.list_agents()

    assert (agents[0]["host"], agents[0]["available"]) == ("hermes", True)
    assert (agents[-1]["host"], agents[-1]["available"]) == (
        "deepseek-harness",
        False,
    )


def test_hermes_initialization_failure_does_not_block_deepseek(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge import cli
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import BridgeConfig
    from agent_workflow_hub.xiaozhi_hermes_bridge.desktop_gateway import (
        DesktopGatewayError,
    )

    mapping = config_value(enabled=True)
    mapping["deepseek_harness"] = {
        "base_url": "http://127.0.0.1:8765",
        "launch_token_env": "DSH_TOKEN",
        "dsh_home": str(tmp_path.resolve()),
        "expected_version": "0.2.0-rc.2",
        "allowed_presets": ["assistant-agent"],
    }
    config = BridgeConfig.from_mapping(mapping)

    class DeepSeekGateway:
        def list_agents(self):
            return (
                {
                    "host": "deepseek-harness",
                    "id": "assistant-agent",
                    "name": "assistant-agent",
                    "default": False,
                    "available": True,
                },
            )

    monkeypatch.setattr(
        cli,
        "_desktop_gateway",
        lambda _environment: (_ for _ in ()).throw(
            DesktopGatewayError("desktop serve is offline")
        ),
    )
    monkeypatch.setattr(
        cli,
        "_deepseek_gateway",
        lambda _config, _environment: DeepSeekGateway(),
    )

    service = cli._shared_service(
        config,
        {"DSH_TOKEN": "live"},
        (tmp_path / "bindings.json").resolve(),
    )
    agents = service.list_agents()

    assert (agents[0]["host"], agents[0]["available"]) == ("hermes", False)
    assert (agents[-1]["host"], agents[-1]["available"]) == (
        "deepseek-harness",
        True,
    )
