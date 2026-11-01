"""In-process Lingxi device controls backed by the live Xiaozhi adapter."""

from __future__ import annotations

import asyncio
import inspect as inspect_module
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from .control_plane import ControlPlaneError
from .device_center import (
    DeviceContractError,
    capability_inventory,
    project_audio_diagnostics,
    project_device_status,
)
from .xiaozhi_server_adapter import XiaozhiHermesAdapter


StopCallback = Callable[[], Awaitable[Mapping[str, object]]]
VolumeCallback = Callable[[int], Awaitable[Mapping[str, object]]]
AliveCallback = Callable[[], bool]
SwitchGuard = Callable[[str], bool]
InspectCallback = Callable[[str], Awaitable[Mapping[str, object]]]


@dataclass(slots=True)
class LiveDevice:
    device_id: str
    session_id: str
    connection_generation: str
    stop: StopCallback
    set_volume: VolumeCallback | None
    inspect: InspectCallback | None
    tool_names: tuple[str, ...]
    alive: AliveCallback
    registered_at: float


class InProcessDeviceControlBackend:
    """The only browser backend allowed to mutate live Xiaozhi state.

    The instance must be owned by the same process and event loop as the
    ``XiaozhiHermesAdapter`` used by voice tools.  It intentionally accepts
    callbacks instead of importing xiaozhi-server internals into this package.
    """

    def __init__(
        self,
        adapter: XiaozhiHermesAdapter,
        *,
        switch_in_progress: SwitchGuard | None = None,
        control_session_id: str = "lingxi-control-plane",
    ) -> None:
        self.adapter = adapter
        self.switch_in_progress = switch_in_progress or (lambda _device_id: False)
        self.control_session_id = control_session_id
        self._devices: dict[str, LiveDevice] = {}
        self._lock = asyncio.Lock()

    async def register_connection(
        self,
        *,
        device_id: str,
        session_id: str,
        connection_generation: str,
        stop: StopCallback,
        set_volume: VolumeCallback | None,
        inspect: InspectCallback | None,
        tool_names: Sequence[str],
        alive: AliveCallback,
    ) -> None:
        if (
            not device_id
            or not session_id
            or not connection_generation
            or len(connection_generation) > 128
        ):
            raise ControlPlaneError(
                "invalid_live_device", "live device identity is unavailable", http_status=400
            )
        if not inspect_module.iscoroutinefunction(stop):
            raise ControlPlaneError(
                "invalid_live_device", "stop callback must be asynchronous", http_status=500
            )
        if set_volume is not None and not inspect_module.iscoroutinefunction(set_volume):
            raise ControlPlaneError(
                "invalid_live_device", "volume callback must be asynchronous", http_status=500
            )
        if inspect is not None and not inspect_module.iscoroutinefunction(inspect):
            raise ControlPlaneError(
                "invalid_live_device", "inspect callback must be asynchronous", http_status=500
            )
        if not all(type(item) is str and item for item in tool_names):
            raise ControlPlaneError(
                "invalid_live_device", "device tool inventory is invalid", http_status=500
            )
        async with self._lock:
            self._devices[device_id] = LiveDevice(
                device_id=device_id,
                session_id=session_id,
                connection_generation=connection_generation,
                stop=stop,
                set_volume=set_volume,
                inspect=inspect,
                tool_names=tuple(sorted(set(tool_names))),
                alive=alive,
                registered_at=time.monotonic(),
            )

    async def unregister_connection(
        self, *, device_id: str, session_id: str, connection_generation: str
    ) -> None:
        async with self._lock:
            current = self._devices.get(device_id)
            if (
                current is not None
                and current.session_id == session_id
                and current.connection_generation == connection_generation
            ):
                self._devices.pop(device_id, None)

    async def _live(
        self, device_id: str, expected_generation: str | None = None
    ) -> LiveDevice:
        async with self._lock:
            live = self._devices.get(device_id)
            if live is not None and live.alive():
                if (
                    expected_generation is not None
                    and live.connection_generation != expected_generation
                ):
                    raise ControlPlaneError(
                        "stale_connection",
                        "the selected Lingxi device reconnected before the operation",
                        http_status=409,
                    )
                return live
            if live is not None:
                self._devices.pop(device_id, None)
        raise ControlPlaneError(
            "device_offline", "the selected Lingxi device is offline", http_status=409
        )

    async def _require_same_connection(self, live: LiveDevice) -> None:
        current = await self._live(live.device_id)
        if current.connection_generation != live.connection_generation:
            raise ControlPlaneError(
                "outcome_unknown",
                "the Lingxi device reconnected before the result was acknowledged",
                http_status=409,
            )

    async def _inspect(self, live: LiveDevice, name: str) -> Mapping[str, object]:
        if live.inspect is None:
            raise ControlPlaneError(
                "capability_unavailable",
                "this device does not expose safe diagnostics",
                http_status=409,
            )
        result = await live.inspect(name)
        await self._require_same_connection(live)
        return result

    async def _live_rows(self) -> tuple[LiveDevice, ...]:
        async with self._lock:
            stale = [key for key, value in self._devices.items() if not value.alive()]
            for key in stale:
                self._devices.pop(key, None)
            return tuple(self._devices.values())

    async def snapshot(self) -> Mapping[str, object]:
        devices: list[dict[str, object]] = []
        for live in await self._live_rows():
            binding = await self.adapter.show_binding(
                live.session_id, device_id=live.device_id
            )
            pending = await self.adapter.pending_for_device(device_id=live.device_id)
            status: dict[str, object] = {}
            status_state = "unavailable"
            if live.inspect is not None and "self_get_device_status" in live.tool_names:
                try:
                    status = project_device_status(await self._inspect(live, "device_status"))
                    status_state = "observed"
                except (ControlPlaneError, DeviceContractError):
                    status = {}
            devices.append(
                {
                    "device_id": live.device_id,
                    "online": True,
                    "session_id": live.session_id,
                    "connection_generation": live.connection_generation,
                    "binding": self._project_binding(binding),
                    "pending_action": self._project_pending(pending),
                    "device_status": status,
                    "device_status_state": status_state,
                    "capabilities": {
                        "stop_speaking": True,
                        "set_volume": live.set_volume is not None,
                    },
                    "capability_inventory": capability_inventory(
                        live.tool_names,
                        generation=live.connection_generation,
                        stop_available=True,
                    ),
                }
            )
        return {"status": "ok", "devices": devices}

    async def list_agents(self) -> tuple[Mapping[str, object], ...]:
        result = await self.adapter.list_agents(self.control_session_id)
        rows = result.get("agents", [])
        if not isinstance(rows, list):
            raise ControlPlaneError(
                "invalid_runtime_response", "agent inventory is unavailable", http_status=502
            )
        return tuple(cast(Mapping[str, object], row) for row in rows if isinstance(row, Mapping))

    async def list_sessions(
        self, *, host: str, agent: str, limit: int
    ) -> tuple[Mapping[str, object], ...]:
        result = await self.adapter.list_sessions(
            self.control_session_id,
            host=host,
            profile=agent,
            limit=limit,
        )
        rows = result.get("sessions", [])
        if not isinstance(rows, list):
            raise ControlPlaneError(
                "invalid_runtime_response", "conversation inventory is unavailable", http_status=502
            )
        return tuple(cast(Mapping[str, object], row) for row in rows if isinstance(row, Mapping))

    async def bind(
        self,
        *,
        device_id: str,
        host: str,
        agent: str,
        stored_session_id: str,
        expected_binding_revision: int,
        expected_generation: str,
    ) -> Mapping[str, object]:
        live = await self._live(device_id, expected_generation)
        if self.switch_in_progress(device_id):
            raise ControlPlaneError(
                "switch_in_progress",
                "a voice-side agent switch is still in progress",
                http_status=409,
            )
        result = await self.adapter.bind_session(
            live.session_id,
            device_id=device_id,
            host=host,
            profile=agent,
            stored_session_id=stored_session_id,
            expected_binding_revision=expected_binding_revision,
        )
        self._require_ok(result, "bind")
        return self._project_binding(result)

    async def unbind(
        self,
        *,
        device_id: str,
        expected_binding_revision: int,
        expected_generation: str,
    ) -> Mapping[str, object]:
        live = await self._live(device_id, expected_generation)
        if self.switch_in_progress(device_id):
            raise ControlPlaneError(
                "switch_in_progress",
                "a voice-side agent switch is still in progress",
                http_status=409,
            )
        result = await self.adapter.unbind_session(
            live.session_id,
            device_id=device_id,
            expected_binding_revision=expected_binding_revision,
        )
        self._require_ok(result, "unbind")
        return {"status": "ok", "removed": result.get("removed") is True}

    async def consume_action(
        self,
        *,
        device_id: str,
        action_id: str,
        decision: str,
        expected_generation: str,
    ) -> Mapping[str, object]:
        await self._live(device_id, expected_generation)
        pending = await self.adapter.pending_for_device(device_id=device_id)
        if pending.get("pending") is not True:
            raise ControlPlaneError(
                "action_absent", "there is no pending device action", http_status=409
            )
        if pending.get("pending_state") != "prepared":
            raise ControlPlaneError(
                "action_busy", "the pending action is already being processed", http_status=409
            )
        if pending.get("action_id") != action_id:
            raise ControlPlaneError(
                "stale_action", "the pending action changed before confirmation", http_status=409
            )
        pending_session_id = pending.get("pending_session_id")
        if type(pending_session_id) is not str:
            raise ControlPlaneError(
                "invalid_runtime_response", "pending action identity is unavailable", http_status=502
            )
        if decision == "confirm":
            result = await self.adapter.confirm_shared_dispatch(
                pending_session_id, device_id=device_id
            )
        else:
            result = await self.adapter.cancel_dispatch(pending_session_id)
        self._require_ok(result, f"action_{decision}", allow_completed=True)
        return {
            "status": result.get("status", "ok"),
            "operation": result.get("operation", pending.get("pending_operation", "")),
        }

    async def stop_speaking(
        self, *, device_id: str, expected_generation: str
    ) -> Mapping[str, object]:
        live = await self._live(device_id, expected_generation)
        result = await live.stop()
        await self._require_same_connection(live)
        self._require_ok(result, "stop_speaking")
        return {
            "status": "ok",
            "interrupt_state": result.get("interrupt_state", "requested"),
        }

    async def set_volume(
        self, *, device_id: str, volume: int, expected_generation: str
    ) -> Mapping[str, object]:
        live = await self._live(device_id, expected_generation)
        if live.set_volume is None:
            raise ControlPlaneError(
                "capability_unavailable",
                "this device does not expose volume control",
                http_status=409,
            )
        result = await live.set_volume(volume)
        await self._require_same_connection(live)
        self._require_ok(result, "set_volume")
        return {"status": "ok", "volume": volume}

    async def audio_diagnostics(
        self, *, device_id: str, expected_generation: str
    ) -> Mapping[str, object]:
        live = await self._live(device_id, expected_generation)
        if "self_audio_get_diagnostics" not in live.tool_names:
            raise ControlPlaneError(
                "capability_unavailable",
                "this device firmware does not expose audio diagnostics",
                http_status=409,
            )
        try:
            projected = project_audio_diagnostics(
                await self._inspect(live, "audio_diagnostics")
            )
        except DeviceContractError as exc:
            raise ControlPlaneError(
                "invalid_runtime_response", str(exc), http_status=502
            ) from None
        return {"status": "ok", "diagnostics": projected}

    @staticmethod
    def _require_ok(
        result: Mapping[str, object], operation: str, *, allow_completed: bool = False
    ) -> None:
        accepted = {"ok"}
        if allow_completed:
            accepted.add("completed")
        if result.get("status") not in accepted:
            raise ControlPlaneError(
                "operation_rejected", f"{operation} was rejected by the runtime", http_status=409
            )

    @staticmethod
    def _project_binding(value: Mapping[str, object]) -> dict[str, object]:
        if value.get("bound") is not True:
            return {"bound": False, "binding_revision": 0}
        return {
            "bound": True,
            "device_id": value.get("device_id", ""),
            "host": value.get("host", ""),
            "agent": value.get("profile", ""),
            "stored_session_id": value.get("stored_session_id", ""),
            "title": value.get("title", ""),
            "binding_revision": value.get("binding_revision"),
        }

    @staticmethod
    def _project_pending(value: Mapping[str, object]) -> dict[str, object] | None:
        if value.get("pending") is not True:
            return None
        return {
            "action_id": value.get("action_id", ""),
            "operation": value.get("pending_operation", ""),
            "state": value.get("pending_state", ""),
            "expires_in_seconds": value.get("expires_in_seconds", 0),
        }


__all__ = ["InProcessDeviceControlBackend", "LiveDevice"]
