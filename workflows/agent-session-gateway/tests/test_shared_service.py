from __future__ import annotations

from pathlib import Path
import threading

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.binding_store import (
    BindingStore,
    BindingStoreError,
    SessionBinding,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.contracts import BridgeConfig
from agent_workflow_hub.xiaozhi_hermes_bridge.desktop_gateway import (
    CompressionResult,
    CreatedSessionResult,
    SharedTurnResult,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.service import BridgeRejected
from agent_workflow_hub.xiaozhi_hermes_bridge.shared_service import (
    LifecycleBindingError,
    SharedConversationService,
)


def _config(
    *,
    enabled: bool = True,
    deepseek_home: Path | None = None,
    bound_voice_mode: str = "confirm",
) -> BridgeConfig:
    mapping: dict[str, object] = {
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
                    "name": "research-agent",
                    "api_server_base_url": "http://127.0.0.1:9120/p/research-agent",
                    "api_key_env": "GITHUB_KEY",
                },
            ],
            "dispatch_enabled": enabled,
            "require_confirmation": True,
            "request_timeout_seconds": 5,
            "poll_interval_seconds": 1,
            "max_polls": 2,
            "max_input_chars": 100,
            "bound_voice_dispatch_mode": bound_voice_mode,
        }
    if deepseek_home is not None:
        mapping["deepseek_harness"] = {
            "base_url": "http://127.0.0.1:8765",
            "launch_token_env": "XIAOZHI_DSH_LAUNCH_TOKEN",
            "dsh_home": str(deepseek_home.resolve()),
            "expected_version": "0.2.0-rc.2",
            "allowed_presets": ["standard", "assistant-agent"],
        }
    return BridgeConfig.from_mapping(mapping)


class FakeGateway:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.created: list[tuple[str, str]] = []
        self.compressed: list[tuple[str, str]] = []

    def list_sessions(self, profile: str, *, limit: int = 20):
        assert limit <= 100
        return (
            {
                "id": "stored-1",
                "title": "RAG projects",
                "preview": "projects",
                "message_count": 10,
                "source": "desktop",
            },
        )
    def list_agents(self):
        return ()

    def send_to_session(self, *, profile: str, stored_session_id: str, text: str):
        self.sent.append((profile, stored_session_id, text))
        return SharedTurnResult(stored_session_id, "runtime-1", "complete", "answer", 9)

    def create_session(self, *, profile: str, idempotency_key: str):
        self.created.append((profile, idempotency_key))
        return CreatedSessionResult("stored-new", "runtime-new")

    def compress_session(self, *, profile: str, stored_session_id: str):
        self.compressed.append((profile, stored_session_id))
        return CompressionResult(
            stored_session_id="stored-compressed",
            runtime_session_id="runtime-1",
            status="compressed",
            removed_message_count=4,
            before_messages=10,
            after_messages=6,
            message="",
        )


def _expected(binding: SessionBinding | None = None) -> dict[str, object]:
    current = binding or SessionBinding(
        "esp32-s3", "research-agent", "stored-1"
    )
    return {
        "expected_host": current.host,
        "expected_profile": current.profile,
        "expected_stored_session_id": current.stored_session_id,
        "expected_revision": current.revision,
    }


def test_browser_binding_mutations_require_the_expected_revision(tmp_path: Path) -> None:
    service = SharedConversationService(
        _config(),
        BindingStore((tmp_path / "bindings.json").resolve()),
        FakeGateway(),
    )

    first = service.bind(
        device_id="esp32-s3",
        profile="research-agent",
        stored_session_id="stored-1",
        expected_revision=0,
    )
    assert first.revision == 1

    with pytest.raises(BridgeRejected, match="changed before bind"):
        service.bind(
            device_id="esp32-s3",
            profile="research-agent",
            stored_session_id="stored-1",
            expected_revision=0,
        )

    second = service.bind(
        device_id="esp32-s3",
        profile="research-agent",
        stored_session_id="stored-1",
        expected_revision=1,
    )
    assert second.revision == 2
    with pytest.raises(BridgeRejected, match="changed before unbind"):
        service.unbind("esp32-s3", expected_revision=1)
    assert service.unbind("esp32-s3", expected_revision=2) is True


def test_deepseek_binding_routes_only_to_deepseek_gateway(tmp_path: Path) -> None:
    hermes = FakeGateway()
    deepseek = FakeGateway()
    service = SharedConversationService(
        _config(deepseek_home=tmp_path),
        BindingStore((tmp_path / "bindings.json").resolve()),
        hermes,
        gateways={"deepseek-harness": deepseek},
    )

    binding = service.bind(
        device_id="esp32-s3",
        host="deepseek-harness",
        profile="assistant-agent",
        stored_session_id="stored-1",
    )
    result = service.send(
        device_id="esp32-s3", text="继续", confirmed=True, **_expected(binding)
    )

    assert binding.host == "deepseek-harness"
    assert result.text == "answer"
    assert deepseek.sent == [("assistant-agent", "stored-1", "继续")]
    assert hermes.sent == []
    with pytest.raises(BridgeRejected, match="not supported"):
        service.compress_session(
            device_id="esp32-s3", confirmed=True, **_expected(binding)
        )


def test_unified_agent_catalog_keeps_host_identity(tmp_path: Path) -> None:
    class DeepSeekGateway(FakeGateway):
        def list_agents(self):
            return (
                {
                    "host": "deepseek-harness",
                    "id": "assistant-agent",
                    "name": "assistant-agent",
                    "available": True,
                },
            )

    service = SharedConversationService(
        _config(deepseek_home=tmp_path),
        BindingStore((tmp_path / "bindings.json").resolve()),
        FakeGateway(),
        gateways={"deepseek-harness": DeepSeekGateway()},
    )

    assert service.list_agents() == (
        {"host": "hermes", "id": "default", "name": "default", "available": True},
        {"host": "hermes", "id": "research-agent", "name": "research-agent", "available": True},
        {"host": "deepseek-harness", "id": "standard", "name": "standard", "available": False},
        {"host": "deepseek-harness", "id": "assistant-agent", "name": "assistant-agent", "available": True},
    )


def test_catalog_marks_configured_deepseek_unavailable_without_gateway(
    tmp_path: Path,
) -> None:
    service = SharedConversationService(
        _config(deepseek_home=tmp_path),
        BindingStore((tmp_path / "bindings.json").resolve()),
        FakeGateway(),
    )

    agents = service.list_agents()

    assert {
        (item["host"], item["id"], item["available"]) for item in agents
    } >= {
        ("deepseek-harness", "standard", False),
        ("deepseek-harness", "assistant-agent", False),
    }


def test_catalog_isolates_host_discovery_failures(tmp_path: Path) -> None:
    class PartiallyFailingHermes(FakeGateway):
        def list_sessions(self, profile: str, *, limit: int = 20):
            if profile == "default":
                raise RuntimeError("default profile offline")
            return super().list_sessions(profile, limit=limit)

    class FailingDeepSeek(FakeGateway):
        def list_agents(self):
            raise RuntimeError("DeepSeek Harness offline")

    service = SharedConversationService(
        _config(deepseek_home=tmp_path),
        BindingStore((tmp_path / "bindings.json").resolve()),
        PartiallyFailingHermes(),
        gateways={"deepseek-harness": FailingDeepSeek()},
    )

    availability = {
        (item["host"], item["id"]): item["available"]
        for item in service.list_agents()
    }

    assert availability[("hermes", "default")] is False
    assert availability[("hermes", "research-agent")] is True
    assert availability[("deepseek-harness", "standard")] is False
    assert availability[("deepseek-harness", "assistant-agent")] is False


def test_binding_persists_across_service_recreation(tmp_path: Path) -> None:
    state = (tmp_path / "bindings.json").resolve()
    first = SharedConversationService(_config(), BindingStore(state), FakeGateway())
    first.bind(
        device_id="esp32-s3", profile="research-agent", stored_session_id="stored-1"
    )

    second = SharedConversationService(_config(), BindingStore(state), FakeGateway())

    assert second.binding("esp32-s3").stored_session_id == "stored-1"  # type: ignore[union-attr]


def test_send_uses_bound_profile_and_stored_session(tmp_path: Path) -> None:
    gateway = FakeGateway()
    service = SharedConversationService(
        _config(bound_voice_mode="direct"),
        BindingStore((tmp_path / "bindings.json").resolve()),
        gateway,
    )
    binding = service.bind(
        device_id="esp32-s3", profile="research-agent", stored_session_id="stored-1"
    )

    result = service.send(
        device_id="esp32-s3",
        text="有哪些项目？",
        confirmed=True,
        **_expected(binding),
    )

    assert result.text == "answer"
    assert gateway.sent == [("research-agent", "stored-1", "有哪些项目？")]


@pytest.mark.parametrize("operation", ("send", "create", "compress"))
def test_expected_binding_rejects_rebind_before_any_host_write(
    tmp_path: Path, operation: str
) -> None:
    gateway = FakeGateway()
    service = SharedConversationService(
        _config(bound_voice_mode="direct"),
        BindingStore((tmp_path / "bindings.json").resolve()),
        gateway,
    )
    prepared_binding = service.bind(
        device_id="esp32-s3",
        profile="research-agent",
        stored_session_id="stored-1",
    )
    service.bind(
        device_id="esp32-s3",
        profile="research-agent",
        stored_session_id="stored-1",
    )

    with pytest.raises(BridgeRejected, match="changed after preparation"):
        if operation == "send":
            service.send(
                device_id="esp32-s3",
                text="must stay on the prepared binding",
                confirmed=True,
                **_expected(prepared_binding),
            )
        elif operation == "create":
            service.create_session(
                device_id="esp32-s3",
                confirmed=True,
                idempotency_key="request-1",
                **_expected(prepared_binding),
            )
        else:
            service.compress_session(
                device_id="esp32-s3",
                confirmed=True,
                **_expected(prepared_binding),
            )

    assert gateway.sent == []
    assert gateway.created == []
    assert gateway.compressed == []


def test_shared_send_uses_stable_binding_without_per_message_confirmation(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    service = SharedConversationService(
        _config(bound_voice_mode="direct"),
        BindingStore((tmp_path / "bindings.json").resolve()),
        gateway,
    )

    binding = service.bind(
        device_id="esp32-s3",
        profile="research-agent",
        stored_session_id="stored-1",
    )
    result = service.send(
        device_id="esp32-s3",
        text="hello",
        confirmed=False,
        voice_direct=True,
        **_expected(binding),
    )

    assert result.text == "answer"
    assert gateway.sent == [("research-agent", "stored-1", "hello")]


def test_shared_send_still_requires_a_stable_binding(tmp_path: Path) -> None:
    service = SharedConversationService(
        _config(bound_voice_mode="direct"),
        BindingStore((tmp_path / "bindings.json").resolve()),
        FakeGateway(),
    )

    with pytest.raises(BridgeRejected, match="not bound"):
        service.send(
            device_id="esp32-s3",
            text="hello",
            confirmed=False,
            voice_direct=True,
            **_expected(),
        )


def test_shared_send_without_voice_scope_still_requires_confirmation(
    tmp_path: Path,
) -> None:
    gateway = FakeGateway()
    service = SharedConversationService(
        _config(bound_voice_mode="direct"),
        BindingStore((tmp_path / "bindings.json").resolve()),
        gateway,
    )
    binding = service.bind(
        device_id="esp32-s3",
        profile="research-agent",
        stored_session_id="stored-1",
    )

    with pytest.raises(BridgeRejected, match="confirmation"):
        service.send(
            device_id="esp32-s3",
            text="desktop path must confirm",
            confirmed=False,
            **_expected(binding),
        )
    assert gateway.sent == []


def test_bound_voice_direct_mode_is_explicit_and_fail_closed(tmp_path: Path) -> None:
    gateway = FakeGateway()
    service = SharedConversationService(
        _config(), BindingStore((tmp_path / "bindings.json").resolve()), gateway
    )
    binding = service.bind(
        device_id="esp32-s3",
        profile="research-agent",
        stored_session_id="stored-1",
    )

    with pytest.raises(BridgeRejected, match="voice direct dispatch is disabled"):
        service.send(
            device_id="esp32-s3",
            text="must not bypass policy",
            confirmed=False,
            voice_direct=True,
            **_expected(binding),
        )
    assert gateway.sent == []


def test_cross_process_device_lock_blocks_concurrent_send_and_rebind(
    tmp_path: Path,
) -> None:
    entered_host = threading.Event()
    release_host = threading.Event()

    class BlockingGateway(FakeGateway):
        def send_to_session(self, *, profile: str, stored_session_id: str, text: str):
            self.sent.append((profile, stored_session_id, text))
            entered_host.set()
            assert release_host.wait(timeout=3)
            return SharedTurnResult(
                stored_session_id, "runtime-1", "complete", "answer", 9
            )

    state = (tmp_path / "bindings.json").resolve()
    first_gateway = BlockingGateway()
    second_gateway = FakeGateway()
    first = SharedConversationService(
        _config(bound_voice_mode="direct"), BindingStore(state), first_gateway
    )
    second = SharedConversationService(
        _config(bound_voice_mode="direct"), BindingStore(state), second_gateway
    )
    binding = first.bind(
        device_id="esp32-s3",
        profile="research-agent",
        stored_session_id="stored-1",
    )
    thread_errors: list[BaseException] = []

    def send_first() -> None:
        try:
            first.send(
                device_id="esp32-s3",
                text="first",
                confirmed=False,
                voice_direct=True,
                **_expected(binding),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            thread_errors.append(exc)

    worker = threading.Thread(target=send_first)
    worker.start()
    assert entered_host.wait(timeout=2)
    try:
        with pytest.raises(BindingStoreError, match="already in progress"):
            second.send(
                device_id="esp32-s3",
                text="second",
                confirmed=False,
                voice_direct=True,
                **_expected(binding),
            )
        with pytest.raises(BindingStoreError, match="already in progress"):
            second.bind(
                device_id="esp32-s3",
                profile="default",
                stored_session_id="stored-1",
            )
    finally:
        release_host.set()
        worker.join(timeout=3)

    assert not worker.is_alive()
    assert thread_errors == []
    assert first_gateway.sent == [("research-agent", "stored-1", "first")]
    assert second_gateway.sent == []
    assert second.binding("esp32-s3") == binding


def test_disabled_dispatch_never_calls_gateway(tmp_path: Path) -> None:
    gateway = FakeGateway()
    service = SharedConversationService(
        _config(enabled=False),
        BindingStore((tmp_path / "bindings.json").resolve()),
        gateway,
    )

    with pytest.raises(BridgeRejected, match="disabled"):
        service.send(
            device_id="esp32-s3", text="hello", confirmed=True, **_expected()
        )

    assert gateway.sent == []


def test_create_session_rebinds_only_after_confirmed_native_success(tmp_path: Path) -> None:
    state = (tmp_path / "bindings.json").resolve()
    gateway = FakeGateway()
    service = SharedConversationService(_config(), BindingStore(state), gateway)
    binding = service.bind(
        device_id="esp32-s3", profile="research-agent", stored_session_id="stored-1"
    )

    created = service.create_session(
        device_id="esp32-s3",
        confirmed=True,
        idempotency_key="xiaozhi:new:request-1",
        **_expected(binding),
    )

    assert created.binding.stored_session_id == "stored-new"
    assert service.binding("esp32-s3").stored_session_id == "stored-new"  # type: ignore[union-attr]
    assert gateway.created == [("research-agent", "xiaozhi:new:request-1")]


def test_create_session_failure_preserves_previous_binding(tmp_path: Path) -> None:
    class FailingGateway(FakeGateway):
        def create_session(self, *, profile: str, idempotency_key: str):
            raise RuntimeError("create failed")

    state = (tmp_path / "bindings.json").resolve()
    service = SharedConversationService(_config(), BindingStore(state), FailingGateway())
    binding = service.bind(
        device_id="esp32-s3", profile="research-agent", stored_session_id="stored-1"
    )

    with pytest.raises(RuntimeError, match="create failed"):
        service.create_session(
            device_id="esp32-s3",
            confirmed=True,
            idempotency_key="request-1",
            **_expected(binding),
        )

    assert service.binding("esp32-s3").stored_session_id == "stored-1"  # type: ignore[union-attr]


def test_compress_updates_rotated_session_binding_after_success(tmp_path: Path) -> None:
    state = (tmp_path / "bindings.json").resolve()
    gateway = FakeGateway()
    service = SharedConversationService(_config(), BindingStore(state), gateway)
    binding = service.bind(
        device_id="esp32-s3", profile="research-agent", stored_session_id="stored-1"
    )

    compressed = service.compress_session(
        device_id="esp32-s3", confirmed=True, **_expected(binding)
    )

    assert compressed.result.status == "compressed"
    assert compressed.binding.stored_session_id == "stored-compressed"
    assert service.binding("esp32-s3").stored_session_id == "stored-compressed"  # type: ignore[union-attr]
    assert gateway.compressed == [("research-agent", "stored-1")]


def test_lifecycle_writes_require_confirmation(tmp_path: Path) -> None:
    service = SharedConversationService(
        _config(), BindingStore((tmp_path / "bindings.json").resolve()), FakeGateway()
    )

    with pytest.raises(BridgeRejected, match="confirmation"):
        service.create_session(
            device_id="esp32-s3",
            confirmed=False,
            idempotency_key="request-1",
            **_expected(),
        )
    with pytest.raises(BridgeRejected, match="confirmation"):
        service.compress_session(
            device_id="esp32-s3", confirmed=False, **_expected()
        )


def test_lifecycle_confirmation_is_required_even_when_message_confirmation_is_disabled(
    tmp_path: Path,
) -> None:
    mapping = _config().to_mapping()
    mapping["require_confirmation"] = False
    service = SharedConversationService(
        BridgeConfig.from_mapping(mapping),
        BindingStore((tmp_path / "bindings.json").resolve()),
        FakeGateway(),
    )
    binding = service.bind(
        device_id="esp32-s3", profile="research-agent", stored_session_id="stored-1"
    )

    with pytest.raises(BridgeRejected, match="confirmation"):
        service.create_session(
            device_id="esp32-s3",
            confirmed=False,
            idempotency_key="request-1",
            **_expected(binding),
        )
    with pytest.raises(BridgeRejected, match="confirmation"):
        service.compress_session(
            device_id="esp32-s3", confirmed=False, **_expected(binding)
        )


def test_create_session_binding_conflict_preserves_new_session_as_recovery_evidence(
    tmp_path: Path,
) -> None:
    state = (tmp_path / "bindings.json").resolve()
    store = BindingStore(state)

    class RacingGateway(FakeGateway):
        def create_session(self, *, profile: str, idempotency_key: str):
            store.bind(SessionBinding("esp32-s3", "default", "other-session", "Other"))
            return super().create_session(
                profile=profile, idempotency_key=idempotency_key
            )

    service = SharedConversationService(_config(), store, RacingGateway())
    binding = service.bind(
        device_id="esp32-s3", profile="research-agent", stored_session_id="stored-1"
    )

    with pytest.raises(LifecycleBindingError) as raised:
        service.create_session(
            device_id="esp32-s3",
            confirmed=True,
            idempotency_key="request-1",
            **_expected(binding),
        )

    assert raised.value.status == "conflict"
    assert raised.value.stored_session_id == "stored-new"
    assert raised.value.old_stored_session_id == "stored-1"
    assert raised.value.old_binding_revision == 1
    assert store.get("esp32-s3").stored_session_id == "other-session"  # type: ignore[union-attr]


def test_compress_binding_conflict_preserves_newer_user_binding(tmp_path: Path) -> None:
    state = (tmp_path / "bindings.json").resolve()
    store = BindingStore(state)

    class RacingGateway(FakeGateway):
        def compress_session(self, *, profile: str, stored_session_id: str):
            store.bind(SessionBinding("esp32-s3", "default", "other-session", "Other"))
            return super().compress_session(
                profile=profile, stored_session_id=stored_session_id
            )

    service = SharedConversationService(_config(), store, RacingGateway())
    binding = service.bind(
        device_id="esp32-s3", profile="research-agent", stored_session_id="stored-1"
    )

    with pytest.raises(LifecycleBindingError) as raised:
        service.compress_session(
            device_id="esp32-s3", confirmed=True, **_expected(binding)
        )

    assert raised.value.status == "conflict"
    assert raised.value.stored_session_id == "stored-compressed"
    assert raised.value.old_stored_session_id == "stored-1"
    assert store.get("esp32-s3").stored_session_id == "other-session"  # type: ignore[union-attr]


@pytest.mark.parametrize("operation", ("create", "compress"))
def test_lifecycle_binding_write_failure_returns_recovery_evidence(
    tmp_path: Path, operation: str
) -> None:
    class FailingCasStore(BindingStore):
        def compare_and_swap(self, expected, replacement):
            raise BindingStoreError("synthetic write failure")

    store = FailingCasStore((tmp_path / "bindings.json").resolve())
    service = SharedConversationService(_config(), store, FakeGateway())
    binding = service.bind(
        device_id="esp32-s3", profile="research-agent", stored_session_id="stored-1"
    )

    with pytest.raises(LifecycleBindingError) as raised:
        if operation == "create":
            service.create_session(
                device_id="esp32-s3",
                confirmed=True,
                idempotency_key="request-1",
                **_expected(binding),
            )
        else:
            service.compress_session(
                device_id="esp32-s3", confirmed=True, **_expected(binding)
            )

    assert raised.value.status == "partial_failure"
    assert raised.value.old_stored_session_id == "stored-1"
    assert raised.value.old_binding_revision == 1
    assert raised.value.stored_session_id in {"stored-new", "stored-compressed"}
