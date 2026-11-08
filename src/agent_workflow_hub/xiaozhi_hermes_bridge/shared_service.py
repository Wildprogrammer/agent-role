"""Application service for binding Xiaozhi devices to existing host chats."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from .binding_store import (
    BindingConflictError,
    BindingStore,
    BindingStoreError,
    SessionBinding,
)
from .contracts import BridgeAttachment, BridgeConfig
from .desktop_gateway import CompressionResult, CreatedSessionResult, SharedTurnResult
from .service import BridgeRejected


class ConversationGateway(Protocol):
    def list_sessions(
        self, profile: str, *, limit: int = 20
    ) -> tuple[dict[str, object], ...]: ...

    def send_to_session(
        self,
        *,
        profile: str,
        stored_session_id: str,
        text: str,
        attachments: tuple[BridgeAttachment, ...] = (),
        on_progress: Callable[[str], None] | None = None,
    ) -> SharedTurnResult: ...

    def cancel_session(self, *, profile: str, stored_session_id: str) -> str: ...

    def create_session(
        self, *, profile: str, idempotency_key: str
    ) -> CreatedSessionResult: ...

    def compress_session(
        self, *, profile: str, stored_session_id: str
    ) -> CompressionResult: ...

    def list_agents(self) -> tuple[dict[str, object], ...]: ...


@dataclass(frozen=True, slots=True)
class CreatedConversationResult:
    binding: SessionBinding
    runtime_session_id: str


@dataclass(frozen=True, slots=True)
class CompressedConversationResult:
    binding: SessionBinding
    result: CompressionResult


class LifecycleBindingError(RuntimeError):
    """Hermes completed, but the durable device binding needs reconciliation."""

    def __init__(
        self,
        message: str,
        *,
        status: str,
        host: str,
        profile: str,
        stored_session_id: str,
        runtime_session_id: str,
        old_stored_session_id: str,
        old_binding_revision: int,
    ) -> str:
        super().__init__(message)
        self.status = status
        self.host = host
        self.profile = profile
        self.stored_session_id = stored_session_id
        self.runtime_session_id = runtime_session_id
        self.old_stored_session_id = old_stored_session_id
        self.old_binding_revision = old_binding_revision


class SharedConversationService:
    """Maintain durable device bindings and submit into the bound host history."""

    def __init__(
        self,
        config: BridgeConfig,
        store: BindingStore,
        gateway: ConversationGateway | None,
        *,
        gateways: Mapping[str, ConversationGateway] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._gateways: dict[str, ConversationGateway] = {}
        if gateway is not None:
            self._gateways["hermes"] = gateway
        if gateways is not None:
            self._gateways.update(gateways)

    def _gateway(self, host: str) -> ConversationGateway:
        gateway = self._gateways.get(host)
        if gateway is None:
            configured = host == "hermes" or (
                host == "deepseek-harness"
                and self._config.deepseek_harness is not None
            )
            state = "unavailable" if configured else "not configured"
            raise BridgeRejected(f"{host} is {state}")
        return gateway

    def _profile_allowed(self, host: str, profile: str) -> bool:
        if host == "hermes":
            return self._config.profile(profile) is not None
        dsh = self._config.deepseek_harness
        return (
            host == "deepseek-harness"
            and dsh is not None
            and profile in dsh.allowed_presets
        )

    def list_agents(self) -> tuple[dict[str, object], ...]:
        hermes = self._gateways.get("hermes")
        result = []
        for route in self._config.profiles:
            available = False
            if hermes is not None:
                try:
                    # Listing zero sessions is still a healthy Profile; only a
                    # gateway failure marks this configured entry unavailable.
                    hermes.list_sessions(route.name, limit=1)
                    available = True
                except Exception:
                    available = False
            result.append(
                {
                    "host": "hermes",
                    "id": route.name,
                    "name": route.name,
                    "available": available,
                }
            )
        dsh_config = self._config.deepseek_harness
        dsh = self._gateways.get("deepseek-harness")
        if dsh_config is not None:
            live = {}
            if dsh is not None:
                try:
                    live = {
                        item.get("id"): {
                            **item,
                            "host": "deepseek-harness",
                            "id": item.get("id"),
                            "name": item.get("name") or item.get("id"),
                            "available": item.get("available") is not False,
                        }
                        for item in dsh.list_agents()
                        if isinstance(item, dict)
                        and item.get("host") == "deepseek-harness"
                        and isinstance(item.get("id"), str)
                        and item.get("id") in dsh_config.allowed_presets
                    }
                except Exception:
                    live = {}
            result.extend(
                live.get(
                    preset,
                    {
                        "host": "deepseek-harness",
                        "id": preset,
                        "name": preset,
                        "available": False,
                    },
                )
                for preset in dsh_config.allowed_presets
            )
        return tuple(result)

    def list_sessions(
        self, *, host: str = "hermes", profile: str, limit: int = 20
    ) -> tuple[dict[str, object], ...]:
        if not self._profile_allowed(host, profile):
            raise BridgeRejected("agent is not allowlisted for the selected host")
        return self._gateway(host).list_sessions(profile, limit=limit)

    def bind(
        self,
        *,
        device_id: str,
        host: str = "hermes",
        profile: str,
        stored_session_id: str,
        expected_revision: int | None = None,
    ) -> SessionBinding:
        sessions = self.list_sessions(host=host, profile=profile, limit=100)
        matching = [row for row in sessions if row.get("id") == stored_session_id]
        if len(matching) != 1:
            raise BridgeRejected(
                "target conversation is unavailable in the recent session list"
            )
        title = matching[0].get("title")
        binding = SessionBinding(
            device_id=device_id,
            profile=profile,
            stored_session_id=stored_session_id,
            host=host,
            title=title if type(title) is str else "",
        )
        with self._store.device_operation(device_id):
            current = self._store.get(device_id)
            current_revision = current.revision if current is not None else 0
            if expected_revision is not None and current_revision != expected_revision:
                raise BridgeRejected("device binding changed before bind")
            return self._store.bind(binding)

    def binding(self, device_id: str) -> SessionBinding | None:
        return self._store.get(device_id)

    def unbind(self, device_id: str, *, expected_revision: int | None = None) -> bool:
        with self._store.device_operation(device_id):
            current = self._store.get(device_id)
            current_revision = current.revision if current is not None else 0
            if expected_revision is not None and current_revision != expected_revision:
                raise BridgeRejected("device binding changed before unbind")
            return self._store.unbind(device_id)

    def _confirmed_binding(
        self,
        *,
        device_id: str,
        confirmed: bool,
        expected_host: str,
        expected_profile: str,
        expected_stored_session_id: str,
        expected_revision: int,
        lifecycle: bool = False,
        voice_direct: bool = False,
    ) -> SessionBinding:
        if not self._config.dispatch_enabled:
            raise BridgeRejected("dispatch is disabled by configuration")
        if lifecycle and not confirmed:
            raise BridgeRejected("lifecycle operation requires an explicit confirmation")
        if not lifecycle and voice_direct:
            if self._config.bound_voice_dispatch_mode != "direct":
                raise BridgeRejected(
                    "bound voice direct dispatch is disabled by configuration"
                )
        elif not lifecycle and not confirmed:
            raise BridgeRejected("shared dispatch requires an explicit confirmation")
        binding = self._store.get(device_id)
        if binding is None:
            raise BridgeRejected("device is not bound to a conversation")
        if (
            binding.host != expected_host
            or binding.profile != expected_profile
            or binding.stored_session_id != expected_stored_session_id
            or binding.revision != expected_revision
        ):
            raise BridgeRejected("device binding changed after preparation")
        if not self._profile_allowed(binding.host, binding.profile):
            raise BridgeRejected("bound agent is no longer allowlisted")
        self._gateway(binding.host)
        return binding

    def create_session(
        self,
        *,
        device_id: str,
        confirmed: bool,
        idempotency_key: str,
        expected_host: str,
        expected_profile: str,
        expected_stored_session_id: str,
        expected_revision: int,
    ) -> CreatedConversationResult:
        with self._store.device_operation(device_id):
            return self._create_session_locked(
                device_id=device_id,
                confirmed=confirmed,
                idempotency_key=idempotency_key,
                expected_host=expected_host,
                expected_profile=expected_profile,
                expected_stored_session_id=expected_stored_session_id,
                expected_revision=expected_revision,
            )

    def _create_session_locked(
        self,
        *,
        device_id: str,
        confirmed: bool,
        idempotency_key: str,
        expected_host: str,
        expected_profile: str,
        expected_stored_session_id: str,
        expected_revision: int,
    ) -> CreatedConversationResult:
        binding = self._confirmed_binding(
            device_id=device_id,
            confirmed=confirmed,
            expected_host=expected_host,
            expected_profile=expected_profile,
            expected_stored_session_id=expected_stored_session_id,
            expected_revision=expected_revision,
            lifecycle=True,
        )
        created = self._gateway(binding.host).create_session(
            profile=binding.profile,
            idempotency_key=idempotency_key,
        )
        replacement = SessionBinding(
            device_id=device_id,
            profile=binding.profile,
            stored_session_id=created.stored_session_id,
            host=binding.host,
            title="",
        )
        try:
            persisted = self._store.compare_and_swap(binding, replacement)
        except BindingConflictError:
            raise LifecycleBindingError(
                "the host created the new conversation, but the device binding changed",
                status="conflict",
                host=binding.host,
                profile=binding.profile,
                stored_session_id=created.stored_session_id,
                runtime_session_id=created.runtime_session_id,
                old_stored_session_id=binding.stored_session_id,
                old_binding_revision=binding.revision,
            ) from None
        except BindingStoreError:
            raise LifecycleBindingError(
                "the host created the new conversation, but the binding could not be saved",
                status="partial_failure",
                host=binding.host,
                profile=binding.profile,
                stored_session_id=created.stored_session_id,
                runtime_session_id=created.runtime_session_id,
                old_stored_session_id=binding.stored_session_id,
                old_binding_revision=binding.revision,
            ) from None
        return CreatedConversationResult(persisted, created.runtime_session_id)

    def compress_session(
        self,
        *,
        device_id: str,
        confirmed: bool,
        expected_host: str,
        expected_profile: str,
        expected_stored_session_id: str,
        expected_revision: int,
    ) -> CompressedConversationResult:
        with self._store.device_operation(device_id):
            return self._compress_session_locked(
                device_id=device_id,
                confirmed=confirmed,
                expected_host=expected_host,
                expected_profile=expected_profile,
                expected_stored_session_id=expected_stored_session_id,
                expected_revision=expected_revision,
            )

    def _compress_session_locked(
        self,
        *,
        device_id: str,
        confirmed: bool,
        expected_host: str,
        expected_profile: str,
        expected_stored_session_id: str,
        expected_revision: int,
    ) -> CompressedConversationResult:
        binding = self._confirmed_binding(
            device_id=device_id,
            confirmed=confirmed,
            expected_host=expected_host,
            expected_profile=expected_profile,
            expected_stored_session_id=expected_stored_session_id,
            expected_revision=expected_revision,
            lifecycle=True,
        )
        if binding.host != "hermes":
            raise BridgeRejected(
                "context compression is not supported for this host yet"
            )
        result = self._gateway(binding.host).compress_session(
            profile=binding.profile,
            stored_session_id=binding.stored_session_id,
        )
        replacement = binding
        if (
            result.status == "compressed"
            and result.stored_session_id != binding.stored_session_id
        ):
            candidate = SessionBinding(
                device_id=device_id,
                profile=binding.profile,
                stored_session_id=result.stored_session_id,
                host=binding.host,
                title=binding.title,
            )
            try:
                replacement = self._store.compare_and_swap(binding, candidate)
            except BindingConflictError:
                raise LifecycleBindingError(
                    "the host compressed the conversation, but the device binding changed",
                    status="conflict",
                    host=binding.host,
                    profile=binding.profile,
                    stored_session_id=result.stored_session_id,
                    runtime_session_id=result.runtime_session_id,
                    old_stored_session_id=binding.stored_session_id,
                    old_binding_revision=binding.revision,
                ) from None
            except BindingStoreError:
                raise LifecycleBindingError(
                    "the host compressed the conversation, but the binding could not be saved",
                    status="partial_failure",
                    host=binding.host,
                    profile=binding.profile,
                    stored_session_id=result.stored_session_id,
                    runtime_session_id=result.runtime_session_id,
                    old_stored_session_id=binding.stored_session_id,
                    old_binding_revision=binding.revision,
                ) from None
        return CompressedConversationResult(replacement, result)

    def send(
        self,
        *,
        device_id: str,
        text: str,
        confirmed: bool,
        expected_host: str,
        expected_profile: str,
        expected_stored_session_id: str,
        expected_revision: int,
        voice_direct: bool = False,
        attachments: tuple[BridgeAttachment, ...] = (),
        on_progress: Callable[[str], None] | None = None,
    ) -> SharedTurnResult:
        with self._store.device_operation(device_id):
            return self._send_locked(
                device_id=device_id,
                text=text,
                confirmed=confirmed,
                expected_host=expected_host,
                expected_profile=expected_profile,
                expected_stored_session_id=expected_stored_session_id,
                expected_revision=expected_revision,
                voice_direct=voice_direct,
                attachments=attachments,
                on_progress=on_progress,
            )

    def _send_locked(
        self,
        *,
        device_id: str,
        text: str,
        confirmed: bool,
        expected_host: str,
        expected_profile: str,
        expected_stored_session_id: str,
        expected_revision: int,
        voice_direct: bool = False,
        attachments: tuple[BridgeAttachment, ...] = (),
        on_progress: Callable[[str], None] | None = None,
    ) -> SharedTurnResult:
        binding = self._confirmed_binding(
            device_id=device_id,
            confirmed=confirmed,
            expected_host=expected_host,
            expected_profile=expected_profile,
            expected_stored_session_id=expected_stored_session_id,
            expected_revision=expected_revision,
            voice_direct=voice_direct,
        )
        clean_text = text.strip()
        if not clean_text:
            raise BridgeRejected("input must not be blank")
        if len(clean_text) > self._config.max_input_chars:
            raise BridgeRejected("input exceeds max_input_chars")
        gateway = self._gateway(binding.host)
        progress_args = (
            {"on_progress": on_progress} if on_progress is not None else {}
        )
        if attachments:
            return gateway.send_to_session(
                profile=binding.profile,
                stored_session_id=binding.stored_session_id,
                text=clean_text,
                attachments=attachments,
                **progress_args,
            )
        return gateway.send_to_session(
            profile=binding.profile,
            stored_session_id=binding.stored_session_id,
            text=clean_text,
            **progress_args,
        )

    def cancel_bound_voice(
        self,
        *,
        device_id: str,
        expected_host: str,
        expected_profile: str,
        expected_stored_session_id: str,
        expected_revision: int,
    ) -> None:
        """Cancel only the exact in-flight bound turn represented by the snapshot."""

        binding = self._store.get(device_id)
        if binding is None:
            raise BridgeRejected("device is not bound to an agent conversation")
        if (
            binding.host != expected_host
            or binding.profile != expected_profile
            or binding.stored_session_id != expected_stored_session_id
            or binding.revision != expected_revision
        ):
            raise BridgeRejected("device binding changed before cancellation")
        return self._gateway(binding.host).cancel_session(
            profile=binding.profile,
            stored_session_id=binding.stored_session_id,
        )

    @staticmethod
    def binding_response(binding: SessionBinding | None) -> Mapping[str, object]:
        if binding is None:
            return {"bound": False}
        return {
            "bound": True,
            "device_id": binding.device_id,
            "host": binding.host,
            "profile": binding.profile,
            "stored_session_id": binding.stored_session_id,
            "title": binding.title,
            "binding_revision": binding.revision,
        }


__all__ = [
    "CompressedConversationResult",
    "CreatedConversationResult",
    "ConversationGateway",
    "LifecycleBindingError",
    "SharedConversationService",
]
