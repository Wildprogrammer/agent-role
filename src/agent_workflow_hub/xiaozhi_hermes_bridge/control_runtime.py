"""Composition root for the optional in-process Lingxi control surfaces."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from .control_events import ControlEventStore, default_control_event_store_path
from .control_http import ControlHttpServers
from .control_plane import ControlPlaneError, DeviceControlCoordinator
from .inprocess_control import InProcessDeviceControlBackend
from .live_companion import TransientCompanionBroker
from .runtime_settings import RuntimeSettingsManager
from .xiaozhi_server_adapter import XiaozhiHermesAdapter


class LingxiControlRuntime:
    """Own one coordinator, backend, and pair of loopback HTTP listeners."""

    def __init__(
        self,
        adapter: XiaozhiHermesAdapter,
        *,
        runtime_config: Path,
        backup_dir: Path,
        admin_port: int,
        companion_port: int,
        admin_password: str,
        companion_password: str,
        switch_in_progress: Callable[[str], bool] | None = None,
    ) -> None:
        epoch = uuid.uuid4().hex
        self.events = ControlEventStore(
            default_control_event_store_path(), runtime_epoch=epoch
        )
        self.settings = RuntimeSettingsManager(
            runtime_config, backup_dir=backup_dir
        )
        self.backend = InProcessDeviceControlBackend(
            adapter, switch_in_progress=switch_in_progress
        )
        self.live_companion = TransientCompanionBroker()
        self.coordinator = DeviceControlCoordinator(
            self.backend,
            event_store=self.events,
            settings=self.settings,
            runtime_epoch=epoch,
        )
        self.servers = ControlHttpServers(
            self.coordinator,
            admin_port=admin_port,
            companion_port=companion_port,
            admin_password=admin_password,
            companion_password=companion_password,
            live_companion=self.live_companion,
        )
        self._started = False
        try:
            self.servers.start()
        except Exception:
            self.servers.close()
            raise
        self._started = True

    def attach_running_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.coordinator.attach_running_loop(loop)

    async def register_connection(
        self,
        *,
        device_id: str,
        session_id: str,
        connection_generation: str,
        stop: Callable[[], Awaitable[Mapping[str, object]]],
        set_volume: Callable[[int], Awaitable[Mapping[str, object]]] | None,
        inspect: Callable[[str], Awaitable[Mapping[str, object]]] | None,
        tool_names: tuple[str, ...],
        alive: Callable[[], bool],
    ) -> None:
        await self.backend.register_connection(
            device_id=device_id,
            session_id=session_id,
            connection_generation=connection_generation,
            stop=stop,
            set_volume=set_volume,
            inspect=inspect,
            tool_names=tool_names,
            alive=alive,
        )
        self.live_companion.register_connection(
            device_id=device_id,
            connection_generation=connection_generation,
        )

    async def unregister_connection(
        self, *, device_id: str, session_id: str, connection_generation: str
    ) -> None:
        await self.backend.unregister_connection(
            device_id=device_id,
            session_id=session_id,
            connection_generation=connection_generation,
        )
        self.live_companion.unregister_connection(
            device_id=device_id,
            connection_generation=connection_generation,
        )

    def register_live_projection_connection(
        self, *, device_id: str, connection_generation: str
    ) -> None:
        """Register the ephemeral projection before MCP capabilities are ready."""

        self.live_companion.register_connection(
            device_id=device_id,
            connection_generation=connection_generation,
        )

    def start_live_turn(
        self,
        *,
        device_id: str,
        connection_generation: str,
        capture_active: bool,
    ) -> str | None:
        return self.live_companion.start_turn(
            device_id=device_id,
            connection_generation=connection_generation,
            capture_active=capture_active,
        )

    def set_live_capture(
        self, *, device_id: str, connection_generation: str, active: bool
    ) -> bool:
        return self.live_companion.set_capture(
            device_id=device_id,
            connection_generation=connection_generation,
            active=active,
        )

    def publish_live_question(
        self,
        *,
        device_id: str,
        connection_generation: str,
        turn_id: str,
        text: str,
        replace: bool = True,
    ) -> bool:
        return self.live_companion.publish_question(
            device_id=device_id,
            connection_generation=connection_generation,
            turn_id=turn_id,
            text=text,
            replace=replace,
        )

    def publish_live_answer(
        self,
        *,
        device_id: str,
        connection_generation: str,
        turn_id: str,
        text: str,
        source: str = "agent",
    ) -> bool:
        return self.live_companion.publish_answer(
            device_id=device_id,
            connection_generation=connection_generation,
            turn_id=turn_id,
            text=text,
            source=source,
        )

    def append_live_spoken_answer(
        self,
        *,
        device_id: str,
        connection_generation: str,
        turn_id: str,
        text: str,
    ) -> bool:
        return self.live_companion.append_spoken_answer(
            device_id=device_id,
            connection_generation=connection_generation,
            turn_id=turn_id,
            text=text,
        )

    def set_live_speech(
        self, *, device_id: str, connection_generation: str, active: bool
    ) -> bool:
        return self.live_companion.set_speech(
            device_id=device_id,
            connection_generation=connection_generation,
            active=active,
        )

    def publish(self, *, state: str, **fields: object) -> None:
        allowed = {
            "device_id",
            "operation",
            "host",
            "agent",
            "stored_session_id",
            "binding_revision",
            "action_id",
            "expires_at",
        }
        projected = {key: value for key, value in fields.items() if key in allowed}
        self.events.publish(state=state, **projected)  # type: ignore[arg-type]

    def close(self) -> None:
        if self._started:
            self.servers.close()
            self._started = False


def resolve_control_passwords(environment: Mapping[str, str]) -> tuple[str, str]:
    shared = environment.get("LINGXI_CONTROL_PASSWORD")
    admin = environment.get("LINGXI_CONTROL_ADMIN_PASSWORD", shared if shared is not None else "admin")
    companion = environment.get(
        "LINGXI_CONTROL_COMPANION_PASSWORD", shared if shared is not None else "admin"
    )
    for name, value in (("admin", admin), ("companion", companion)):
        if not value or not value.strip():
            raise ControlPlaneError(
                "invalid_control_password",
                f"{name} control password must not be empty or whitespace",
                http_status=500,
            )
        if len(value) > 256:
            raise ControlPlaneError(
                "invalid_control_password",
                f"{name} control password must contain at most 256 characters",
                http_status=500,
            )
    return admin, companion


def control_runtime_from_environment(
    adapter: XiaozhiHermesAdapter,
    *,
    switch_in_progress: Callable[[str], bool] | None = None,
    environment: Mapping[str, str] | None = None,
) -> LingxiControlRuntime | None:
    env = os.environ if environment is None else environment
    if env.get("LINGXI_CONTROL_ENABLED", "").strip().lower() not in {"1", "true", "yes"}:
        return None
    raw_config = env.get("LINGXI_RUNTIME_CONFIG", "").strip()
    if not raw_config:
        raise ControlPlaneError(
            "missing_runtime_config",
            "LINGXI_RUNTIME_CONFIG is required when the control UI is enabled",
            http_status=500,
        )
    runtime_config = Path(raw_config)
    if not runtime_config.is_absolute() or not runtime_config.is_file():
        raise ControlPlaneError(
            "invalid_runtime_config",
            "LINGXI_RUNTIME_CONFIG must name an existing absolute file",
            http_status=500,
        )
    raw_backup = env.get("LINGXI_CONTROL_BACKUP_DIR", "").strip()
    backup_dir = (
        Path(raw_backup)
        if raw_backup
        else runtime_config.parent / "lingxi-control-backups"
    )
    admin_password, companion_password = resolve_control_passwords(env)
    try:
        admin_port = int(env.get("LINGXI_CONTROL_ADMIN_PORT", "8765"))
        companion_port = int(env.get("LINGXI_CONTROL_COMPANION_PORT", "8766"))
    except ValueError:
        raise ControlPlaneError(
            "invalid_listener", "control listener ports must be integers", http_status=500
        ) from None
    if not backup_dir.is_absolute():
        raise ControlPlaneError(
            "invalid_control_path", "control backup path must be absolute", http_status=500
        )
    return LingxiControlRuntime(
        adapter,
        runtime_config=runtime_config.resolve(),
        backup_dir=backup_dir.resolve(),
        admin_port=admin_port,
        companion_port=companion_port,
        admin_password=admin_password,
        companion_password=companion_password,
        switch_in_progress=switch_in_progress,
    )


__all__ = [
    "LingxiControlRuntime",
    "control_runtime_from_environment",
    "resolve_control_passwords",
]
