from __future__ import annotations

from pathlib import Path

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import (
    BridgeConfig,
    BridgeContractError,
    BridgeRequest,
)


def config_mapping(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1.0",
        "trusted_caller_id": "xiaozhi-backend",
        "default_profile": "default",
        "profiles": [
            {
                "name": "default",
                "api_server_base_url": "http://127.0.0.1:9119/p/default",
                "api_key_env": "HERMES_DEFAULT_KEY",
            },
            {
                "name": "specialist",
                "api_server_base_url": "http://[::1]:9119/p/specialist",
                "api_key_env": "HERMES_SPECIALIST_KEY",
            },
        ],
        "dispatch_enabled": False,
        "require_confirmation": True,
        "request_timeout_seconds": 30,
        "poll_interval_seconds": 1,
        "max_polls": 3,
        "max_input_chars": 100,
    }
    value.update(changes)
    return value


def test_config_accepts_only_allowlisted_loopback_profile_routes() -> None:
    config = BridgeConfig.from_mapping(config_mapping())

    assert config.default_profile == "default"
    assert [item.name for item in config.profiles] == ["default", "specialist"]
    assert config.profile("unknown") is None
    assert config.bound_voice_dispatch_mode == "confirm"


def test_bound_voice_direct_policy_is_explicit_and_closed() -> None:
    direct = BridgeConfig.from_mapping(
        config_mapping(bound_voice_dispatch_mode="direct")
    )
    assert direct.bound_voice_dispatch_mode == "direct"
    assert direct.to_mapping()["bound_voice_dispatch_mode"] == "direct"

    with pytest.raises(BridgeContractError, match="confirm or direct"):
        BridgeConfig.from_mapping(config_mapping(bound_voice_dispatch_mode="enabled"))


@pytest.mark.parametrize(
    "url",
    (
        "https://127.0.0.1:9119/p/default",
        "http://example.com/p/default",
        "http://localhost:9119/p/default",
        "http://user:secret@127.0.0.1:9119/p/default",
        "http://127.0.0.1:9119/p/other",
        "http://127.0.0.1:9119/p/default?key=secret",
    ),
)
def test_config_rejects_unsafe_or_mismatched_urls(url: str) -> None:
    mapping = config_mapping()
    profiles = list(mapping["profiles"])  # type: ignore[arg-type]
    profiles[0] = {**profiles[0], "api_server_base_url": url}  # type: ignore[arg-type]
    mapping["profiles"] = profiles

    with pytest.raises(BridgeContractError, match="loopback HTTP"):
        BridgeConfig.from_mapping(mapping)


def test_config_rejects_secret_value_and_duplicate_profiles() -> None:
    mapping = config_mapping(api_key="plaintext-secret")
    with pytest.raises(BridgeContractError, match="unknown fields"):
        BridgeConfig.from_mapping(mapping)


def test_config_accepts_closed_deepseek_web_contract(tmp_path: Path) -> None:
    mapping = config_mapping(
        deepseek_harness={
            "base_url": "http://127.0.0.1:8765",
            "launch_token_env": "XIAOZHI_DSH_LAUNCH_TOKEN",
            "dsh_home": str(tmp_path.resolve()),
            "expected_version": "0.2.0-rc.2",
            "allowed_presets": ["standard", "assistant-agent"],
        }
    )

    config = BridgeConfig.from_mapping(mapping)

    assert config.deepseek_harness is not None
    assert config.deepseek_harness.base_url == "http://127.0.0.1:8765"
    assert config.deepseek_harness.allowed_presets == ("standard", "assistant-agent")
    assert config.to_mapping()["deepseek_harness"] == mapping["deepseek_harness"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("base_url", "http://localhost:8765", "numeric-loopback"),
        ("base_url", "https://127.0.0.1:8765", "numeric-loopback"),
        ("expected_version", "0.2.0", "0.2.0-rc.2"),
        ("allowed_presets", ["standard", "standard"], "unique"),
    ),
)
def test_config_rejects_unsafe_deepseek_web_contract(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    deepseek: dict[str, object] = {
        "base_url": "http://127.0.0.1:8765",
        "launch_token_env": "XIAOZHI_DSH_LAUNCH_TOKEN",
        "dsh_home": str(tmp_path.resolve()),
        "expected_version": "0.2.0-rc.2",
        "allowed_presets": ["standard"],
    }
    deepseek[field] = value

    with pytest.raises(BridgeContractError, match=message):
        BridgeConfig.from_mapping(config_mapping(deepseek_harness=deepseek))

    mapping = config_mapping()
    profiles = list(mapping["profiles"])  # type: ignore[arg-type]
    profiles.append(dict(profiles[0]))  # type: ignore[arg-type]
    mapping["profiles"] = profiles
    with pytest.raises(BridgeContractError, match="unique"):
        BridgeConfig.from_mapping(mapping)


def test_requests_are_closed_per_operation() -> None:
    switch = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "voice:42",
            "operation": "switch_profile",
            "profile": "specialist",
        }
    )
    assert switch.profile == "specialist"

    with pytest.raises(BridgeContractError, match="requires input"):
        BridgeRequest.from_mapping(
            {
                "schema_version": "1.0",
                "caller_id": "xiaozhi-backend",
                "session_id": "voice:42",
                "operation": "dispatch",
            }
        )


def test_bound_voice_attachment_contract_is_closed(tmp_path: Path) -> None:
    attachment = {
        "attachment_id": "att_" + "a" * 32,
        "path": str((tmp_path / "note.txt").resolve()),
        "name": "note.txt",
        "media_type": "text/plain",
        "kind": "text",
        "bytes": 12,
        "sha256": "b" * 64,
    }
    request = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:100",
            "operation": "dispatch_bound_voice",
            "device_id": "esp32-s3",
            "input": "read it",
            "host": "hermes",
            "profile": "specialist",
            "stored_session_id": "stored-session-1",
            "binding_revision": 1,
            "attachments": [attachment],
        }
    )

    assert request.attachments[0].name == "note.txt"
    with pytest.raises(BridgeContractError, match="forbids attachments"):
        BridgeRequest.from_mapping(
            {
                "schema_version": "1.0",
                "caller_id": "xiaozhi-backend",
                "session_id": "socket:100",
                "operation": "dispatch_shared",
                "device_id": "esp32-s3",
                "input": "read it",
                "confirmed": True,
                "host": "hermes",
                "profile": "specialist",
                "stored_session_id": "stored-session-1",
                "binding_revision": 1,
                "attachments": [attachment],
            }
        )


def test_shared_requests_separate_socket_session_from_stable_device() -> None:
    binding = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:42",
            "operation": "bind_session",
            "device_id": "esp32-s3",
            "profile": "specialist",
            "stored_session_id": "stored-session-1",
        }
    )
    dispatch = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:99",
            "operation": "dispatch_shared",
            "device_id": "esp32-s3",
            "input": "continue the existing conversation",
            "confirmed": True,
            "host": "hermes",
            "profile": "specialist",
            "stored_session_id": "stored-session-1",
            "binding_revision": 1,
        }
    )

    assert binding.device_id == dispatch.device_id == "esp32-s3"
    assert binding.session_id != dispatch.session_id
    assert binding.host == "hermes"

    guarded_binding = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:42",
            "operation": "bind_session",
            "device_id": "esp32-s3",
            "profile": "specialist",
            "stored_session_id": "stored-session-1",
            "binding_revision": 0,
        }
    )
    guarded_unbind = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:42",
            "operation": "unbind_session",
            "device_id": "esp32-s3",
            "binding_revision": 1,
        }
    )
    assert guarded_binding.binding_revision == 0
    assert guarded_unbind.binding_revision == 1

    with pytest.raises(BridgeContractError, match="integer from 0"):
        BridgeRequest.from_mapping(
            {
                "schema_version": "1.0",
                "caller_id": "xiaozhi-backend",
                "session_id": "socket:42",
                "operation": "bind_session",
                "device_id": "esp32-s3",
                "profile": "specialist",
                "stored_session_id": "stored-session-1",
                "binding_revision": -1,
            }
        )

    voice = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:100",
            "operation": "dispatch_bound_voice",
            "device_id": "esp32-s3",
            "input": "ordinary voice",
            "host": "hermes",
            "profile": "specialist",
            "stored_session_id": "stored-session-1",
            "binding_revision": 1,
        }
    )
    assert voice.confirmed is False
    with pytest.raises(BridgeContractError, match="expected binding"):
        BridgeRequest.from_mapping(
            {
                "schema_version": "1.0",
                "caller_id": "xiaozhi-backend",
                "session_id": "socket:100",
                "operation": "dispatch_bound_voice",
                "device_id": "esp32-s3",
                "input": "ordinary voice",
                "confirmed": True,
                "host": "hermes",
                "profile": "specialist",
                "stored_session_id": "stored-session-1",
                "binding_revision": 1,
            }
        )

    agents = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:42",
            "operation": "list_agents",
        }
    )
    deepseek_sessions = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:42",
            "operation": "list_sessions",
            "host": "deepseek-harness",
            "profile": "assistant-agent",
        }
    )
    assert agents.operation == "list_agents"
    assert deepseek_sessions.host == "deepseek-harness"

    with pytest.raises(BridgeContractError, match="requires only device_id"):
        BridgeRequest.from_mapping(
            {
                "schema_version": "1.0",
                "caller_id": "xiaozhi-backend",
                "session_id": "socket:42",
                "operation": "show_binding",
            }
        )

    with pytest.raises(BridgeContractError, match="forbids"):
        BridgeRequest.from_mapping(
            {
                "schema_version": "1.0",
                "caller_id": "xiaozhi-backend",
                "session_id": "voice:42",
                "operation": "list_profiles",
                "confirmed": True,
            }
        )


def test_contract_module_does_not_accept_relative_paths(tmp_path: Path) -> None:
    from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import read_config

    with pytest.raises(BridgeContractError, match="absolute"):
        read_config(Path("bridge.json"))


def test_lifecycle_requests_require_confirmation_and_create_idempotency() -> None:
    created = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:42",
            "operation": "create_session",
            "device_id": "esp32-s3",
            "confirmed": True,
            "idempotency_key": "xiaozhi:new:request-1",
            "host": "hermes",
            "profile": "specialist",
            "stored_session_id": "stored-session-1",
            "binding_revision": 1,
        }
    )
    compressed = BridgeRequest.from_mapping(
        {
            "schema_version": "1.0",
            "caller_id": "xiaozhi-backend",
            "session_id": "socket:42",
            "operation": "compress_session",
            "device_id": "esp32-s3",
            "confirmed": True,
            "host": "hermes",
            "profile": "specialist",
            "stored_session_id": "stored-session-1",
            "binding_revision": 1,
        }
    )

    assert created.idempotency_key == "xiaozhi:new:request-1"
    assert compressed.confirmed is True

    with pytest.raises(BridgeContractError, match="confirmed"):
        BridgeRequest.from_mapping(
            {
                "schema_version": "1.0",
                "caller_id": "xiaozhi-backend",
                "session_id": "socket:42",
                "operation": "compress_session",
                "device_id": "esp32-s3",
                "host": "hermes",
                "profile": "specialist",
                "stored_session_id": "stored-session-1",
                "binding_revision": 1,
            }
        )
