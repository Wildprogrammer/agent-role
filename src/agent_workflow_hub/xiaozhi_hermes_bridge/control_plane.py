"""Single-authority control facade shared by Lingxi voice and browser surfaces."""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import threading
import uuid
from collections.abc import Mapping
from typing import Protocol, cast

from .control_events import ControlEventStore
from .runtime_settings import RuntimeSettingsManager


_DEVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_HOSTS = frozenset(("hermes", "deepseek-harness"))
_AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ACTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ControlPlaneError(RuntimeError):
    """A projected control-plane error safe to return to a browser."""

    def __init__(self, code: str, message: str, *, http_status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class DeviceControlBackend(Protocol):
    """Implemented in the Xiaozhi process that owns adapter and live connections."""

    async def snapshot(self) -> Mapping[str, object]: ...

    async def list_agents(self) -> tuple[Mapping[str, object], ...]: ...

    async def list_sessions(
        self, *, host: str, agent: str, limit: int
    ) -> tuple[Mapping[str, object], ...]: ...

    async def bind(
        self,
        *,
        device_id: str,
        host: str,
        agent: str,
        stored_session_id: str,
        expected_binding_revision: int,
        expected_generation: str,
    ) -> Mapping[str, object]: ...

    async def unbind(
        self,
        *,
        device_id: str,
        expected_binding_revision: int,
        expected_generation: str,
    ) -> Mapping[str, object]: ...

    async def consume_action(
        self,
        *,
        device_id: str,
        action_id: str,
        decision: str,
        expected_generation: str,
    ) -> Mapping[str, object]: ...

    async def stop_speaking(
        self, *, device_id: str, expected_generation: str
    ) -> Mapping[str, object]: ...

    async def set_volume(
        self, *, device_id: str, volume: int, expected_generation: str
    ) -> Mapping[str, object]: ...

    async def audio_diagnostics(
        self, *, device_id: str, expected_generation: str
    ) -> Mapping[str, object]: ...


class DeviceControlCoordinator:
    """Schedule all browser actions onto the one Xiaozhi runtime event loop."""

    def __init__(
        self,
        backend: DeviceControlBackend,
        *,
        event_store: ControlEventStore,
        settings: RuntimeSettingsManager,
        runtime_epoch: str | None = None,
        call_timeout_seconds: float = 8.0,
    ) -> None:
        self.backend = backend
        self.event_store = event_store
        self.settings = settings
        self.runtime_epoch = runtime_epoch or uuid.uuid4().hex
        self.call_timeout_seconds = call_timeout_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._lock = threading.RLock()

    def attach_running_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if not loop.is_running():
            raise ControlPlaneError(
                "runtime_not_ready", "Xiaozhi runtime loop is not running", http_status=503
            )
        with self._lock:
            if self._loop is not None and self._loop is not loop:
                raise ControlPlaneError(
                    "runtime_changed", "Xiaozhi runtime loop changed; restart the console", http_status=503
                )
            self._loop = loop
            self._loop_thread_id = threading.get_ident()

    def _call(self, coroutine: object) -> object:
        with self._lock:
            loop = self._loop
            loop_thread_id = self._loop_thread_id
        if loop is None or not loop.is_running():
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            raise ControlPlaneError(
                "runtime_not_ready",
                "Xiaozhi runtime has not attached the control coordinator",
                http_status=503,
            )
        if loop_thread_id == threading.get_ident():
            close = getattr(coroutine, "close", None)
            if callable(close):
                close()
            raise ControlPlaneError(
                "invalid_call_context",
                "synchronous browser facade cannot run on the Xiaozhi event-loop thread",
                http_status=500,
            )
        future = asyncio.run_coroutine_threadsafe(cast(object, coroutine), loop)
        try:
            return future.result(timeout=self.call_timeout_seconds)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise ControlPlaneError(
                "runtime_timeout", "Xiaozhi control operation timed out", http_status=504
            ) from None
        except ControlPlaneError:
            raise
        except Exception as exc:
            raise ControlPlaneError(
                "operation_unavailable",
                f"control operation failed: {type(exc).__name__}",
                http_status=409,
            ) from None

    def snapshot(self) -> dict[str, object]:
        runtime: Mapping[str, object]
        try:
            runtime = cast(Mapping[str, object], self._call(self.backend.snapshot()))
        except ControlPlaneError as exc:
            runtime = {
                "status": "unavailable",
                "error_code": exc.code,
                "message": str(exc),
                "devices": [],
            }
        return {
            "schema_version": "1.0",
            "runtime_epoch": self.runtime_epoch,
            "runtime": dict(runtime),
            "settings": self.settings.snapshot(),
            "events": self.event_store.snapshot(after_event_id=0),
            "maintenance": {
                "status": "blocked",
                "executable": False,
                "blockers": [
                    "signed_ota_trust_anchor_unavailable",
                    "dual_slot_assets_unavailable",
                    "authenticated_provisioning_unavailable",
                    "device_acknowledged_confirmation_unavailable",
                ],
                "physical_only": ["clear_wifi", "clear_nvs", "factory_reset"],
            },
        }

    def list_agents(self) -> dict[str, object]:
        rows = cast(tuple[Mapping[str, object], ...], self._call(self.backend.list_agents()))
        agents = []
        for row in rows:
            host = row.get("host")
            agent = row.get("id")
            name = row.get("name")
            if host not in _HOSTS or type(agent) is not str or _AGENT_RE.fullmatch(agent) is None:
                continue
            agents.append(
                {
                    "host": host,
                    "id": agent,
                    "name": name if type(name) is str and name else agent,
                    "available": row.get("available") is not False,
                }
            )
        return {"schema_version": "1.0", "agents": agents}

    def list_sessions(self, *, host: str, agent: str, limit: int = 20) -> dict[str, object]:
        self._validate_host_agent(host, agent)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ControlPlaneError("invalid_request", "limit must be 1..100", http_status=400)
        rows = cast(
            tuple[Mapping[str, object], ...],
            self._call(self.backend.list_sessions(host=host, agent=agent, limit=limit)),
        )
        sessions = []
        for row in rows:
            session_id = row.get("id")
            if type(session_id) is not str or not session_id or len(session_id) > 512:
                continue
            title = row.get("title")
            message_count = row.get("message_count")
            sessions.append(
                {
                    "id": session_id,
                    "title": title if type(title) is str else "",
                    "message_count": message_count if type(message_count) is int else None,
                }
            )
        return {"schema_version": "1.0", "host": host, "agent": agent, "sessions": sessions}

    def bind(
        self,
        *,
        device_id: str,
        host: str,
        agent: str,
        stored_session_id: str,
        expected_binding_revision: int,
        expected_generation: str,
    ) -> dict[str, object]:
        self._validate_device(device_id)
        self._validate_generation(expected_generation)
        self._validate_host_agent(host, agent)
        if not stored_session_id.strip() or len(stored_session_id) > 512:
            raise ControlPlaneError("invalid_request", "stored session id is invalid", http_status=400)
        self._validate_expected_revision(expected_binding_revision)
        result = cast(
            Mapping[str, object],
            self._call(
                self.backend.bind(
                    device_id=device_id,
                    host=host,
                    agent=agent,
                    stored_session_id=stored_session_id,
                    expected_binding_revision=expected_binding_revision,
                    expected_generation=expected_generation,
                )
            ),
        )
        self.event_store.publish(
            state="success",
            device_id=device_id,
            operation="bind",
            host=host,
            agent=agent,
            stored_session_id=stored_session_id,
            binding_revision=(
                cast(int, result["binding_revision"])
                if type(result.get("binding_revision")) is int
                else None
            ),
        )
        return {"schema_version": "1.0", **dict(result)}

    def unbind(
        self,
        *,
        device_id: str,
        expected_binding_revision: int,
        expected_generation: str,
    ) -> dict[str, object]:
        self._validate_device(device_id)
        self._validate_generation(expected_generation)
        self._validate_expected_revision(expected_binding_revision)
        result = cast(
            Mapping[str, object],
            self._call(
                self.backend.unbind(
                    device_id=device_id,
                    expected_binding_revision=expected_binding_revision,
                    expected_generation=expected_generation,
                )
            ),
        )
        self.event_store.publish(state="idle", device_id=device_id, operation="unbind")
        return {"schema_version": "1.0", **dict(result)}

    def consume_action(
        self,
        *,
        device_id: str,
        action_id: str,
        decision: str,
        expected_generation: str,
    ) -> dict[str, object]:
        self._validate_device(device_id)
        self._validate_generation(expected_generation)
        if _ACTION_RE.fullmatch(action_id) is None or decision not in {"confirm", "cancel"}:
            raise ControlPlaneError("invalid_request", "action decision is invalid", http_status=400)
        result = cast(
            Mapping[str, object],
            self._call(
                self.backend.consume_action(
                    device_id=device_id,
                    action_id=action_id,
                    decision=decision,
                    expected_generation=expected_generation,
                )
            ),
        )
        state = "thinking" if decision == "confirm" else "idle"
        self.event_store.publish(
            state=state,
            device_id=device_id,
            operation=f"action_{decision}",
            action_id=action_id,
        )
        return {"schema_version": "1.0", **dict(result)}

    def stop_speaking(
        self, *, device_id: str, expected_generation: str
    ) -> dict[str, object]:
        self._validate_device(device_id)
        self._validate_generation(expected_generation)
        result = cast(
            Mapping[str, object],
            self._call(
                self.backend.stop_speaking(
                    device_id=device_id, expected_generation=expected_generation
                )
            ),
        )
        self.event_store.publish(state="idle", device_id=device_id, operation="stop_speaking")
        return {"schema_version": "1.0", **dict(result)}

    def set_volume(
        self, *, device_id: str, volume: int, expected_generation: str
    ) -> dict[str, object]:
        self._validate_device(device_id)
        self._validate_generation(expected_generation)
        if type(volume) is not int or not 0 <= volume <= 100:
            raise ControlPlaneError("invalid_request", "volume must be 0..100", http_status=400)
        result = cast(
            Mapping[str, object],
            self._call(
                self.backend.set_volume(
                    device_id=device_id,
                    volume=volume,
                    expected_generation=expected_generation,
                )
            ),
        )
        return {"schema_version": "1.0", **dict(result)}

    def audio_diagnostics(
        self, *, device_id: str, expected_generation: str
    ) -> dict[str, object]:
        self._validate_device(device_id)
        self._validate_generation(expected_generation)
        result = cast(
            Mapping[str, object],
            self._call(
                self.backend.audio_diagnostics(
                    device_id=device_id,
                    expected_generation=expected_generation,
                )
            ),
        )
        return {"schema_version": "1.0", **dict(result)}

    def events(self, *, after_event_id: int) -> dict[str, object]:
        return self.event_store.snapshot(after_event_id=after_event_id)

    def config_preview(
        self, patch: dict[str, object], *, expected_configured_sha256: str
    ) -> dict[str, object]:
        return self.settings.preview(
            patch, expected_configured_sha256=expected_configured_sha256
        )

    def config_apply(self, *, plan_id: str, candidate_sha256: str) -> dict[str, object]:
        return self.settings.apply(plan_id=plan_id, candidate_sha256=candidate_sha256)

    def config_rollback(
        self, *, backup_id: str, expected_configured_sha256: str
    ) -> dict[str, object]:
        return self.settings.rollback(
            backup_id=backup_id,
            expected_configured_sha256=expected_configured_sha256,
        )

    @staticmethod
    def _validate_device(device_id: str) -> None:
        if type(device_id) is not str or _DEVICE_RE.fullmatch(device_id) is None:
            raise ControlPlaneError("invalid_request", "device id is invalid", http_status=400)

    @staticmethod
    def _validate_host_agent(host: str, agent: str) -> None:
        if host not in _HOSTS or type(agent) is not str or _AGENT_RE.fullmatch(agent) is None:
            raise ControlPlaneError("invalid_request", "host or agent is invalid", http_status=400)

    @staticmethod
    def _validate_expected_revision(value: int) -> None:
        if type(value) is not int or not 0 <= value <= 2_147_483_647:
            raise ControlPlaneError(
                "invalid_request", "expected binding revision is invalid", http_status=400
            )

    @staticmethod
    def _validate_generation(value: str) -> None:
        if type(value) is not str or _ACTION_RE.fullmatch(value) is None:
            raise ControlPlaneError(
                "invalid_request", "expected connection generation is invalid", http_status=400
            )


__all__ = [
    "ControlPlaneError",
    "DeviceControlBackend",
    "DeviceControlCoordinator",
]
