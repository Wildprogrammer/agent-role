"""Bounded, privacy-preserving state events for the Lingxi companion UI."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .device_center import DeviceContractError, project_state


_STATES = frozenset(
    (
        "idle",
        "listening",
        "thinking",
        "speaking",
        "waiting_confirmation",
        "success",
        "error",
        "offline",
        "provisioning",
        "transcribing",
        "interrupted",
        "audio_testing",
        "updating",
    )
)
_MAX_EVENTS = 128
_MAX_STORE_BYTES = 512 * 1024


class ControlEventStoreError(RuntimeError):
    """Raised when the bounded event store cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ControlEvent:
    event_id: int
    runtime_epoch: str
    state_version: int
    at: str
    state: str
    device_id: str = ""
    operation: str = ""
    host: str = ""
    agent: str = ""
    stored_session_id: str = ""
    binding_revision: int | None = None
    action_id: str = ""
    expires_at: str = ""
    device_state: str = "idle"
    conversation_phase: str = "none"
    expression: str = "neutral"
    expression_version: str = "1.0"


class ControlEventStore:
    """Persist only projected state, never transcript text or credentials."""

    def __init__(self, path: Path, *, runtime_epoch: str) -> None:
        if not path.is_absolute():
            raise ControlEventStoreError("event store path must be absolute")
        if not runtime_epoch or len(runtime_epoch) > 128:
            raise ControlEventStoreError("runtime epoch is invalid")
        self.path = path.resolve()
        self.runtime_epoch = runtime_epoch
        self._lock = threading.RLock()

    def publish(
        self,
        *,
        state: str,
        device_id: str = "",
        operation: str = "",
        host: str = "",
        agent: str = "",
        stored_session_id: str = "",
        binding_revision: int | None = None,
        action_id: str = "",
        expires_at: str = "",
    ) -> ControlEvent:
        if state not in _STATES:
            raise ControlEventStoreError("event state is unsupported")
        try:
            projection = project_state(state)
        except DeviceContractError as exc:
            raise ControlEventStoreError(str(exc)) from None
        for label, value, limit in (
            ("device_id", device_id, 128),
            ("operation", operation, 64),
            ("host", host, 64),
            ("agent", agent, 128),
            ("stored_session_id", stored_session_id, 512),
            ("action_id", action_id, 128),
            ("expires_at", expires_at, 64),
        ):
            if type(value) is not str or len(value) > limit or "\0" in value:
                raise ControlEventStoreError(f"event {label} is invalid")
        if binding_revision is not None and (
            type(binding_revision) is not int or binding_revision < 1
        ):
            raise ControlEventStoreError("binding revision is invalid")
        with self._lock:
            value = self._read_unlocked()
            events = cast(list[dict[str, object]], value["events"])
            previous_id = cast(int, value["last_event_id"])
            previous_version = cast(int, value["state_version"])
            event = ControlEvent(
                event_id=previous_id + 1,
                runtime_epoch=self.runtime_epoch,
                state_version=previous_version + 1,
                at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                state=state,
                device_id=device_id,
                operation=operation,
                host=host,
                agent=agent,
                stored_session_id=stored_session_id,
                binding_revision=binding_revision,
                action_id=action_id,
                expires_at=expires_at,
                device_state=projection.device_state,
                conversation_phase=projection.conversation_phase,
                expression=projection.expression,
                expression_version=projection.expression_version,
            )
            events.append(asdict(event))
            value = {
                "schema_version": "1.0",
                "runtime_epoch": self.runtime_epoch,
                "last_event_id": event.event_id,
                "state_version": event.state_version,
                "events": events[-_MAX_EVENTS:],
            }
            self._write_unlocked(value)
            return event

    def snapshot(self, *, after_event_id: int = 0) -> dict[str, object]:
        if type(after_event_id) is not int or after_event_id < 0:
            raise ControlEventStoreError("after_event_id must be non-negative")
        with self._lock:
            value = self._read_unlocked()
            events = [
                item
                for item in cast(list[dict[str, object]], value["events"])
                if cast(int, item["event_id"]) > after_event_id
            ]
            return {
                "schema_version": "1.0",
                "runtime_epoch": self.runtime_epoch,
                "last_event_id": value["last_event_id"],
                "state_version": value["state_version"],
                "events": events,
            }

    def _read_unlocked(self) -> dict[str, object]:
        empty: dict[str, object] = {
            "schema_version": "1.0",
            "runtime_epoch": self.runtime_epoch,
            "last_event_id": 0,
            "state_version": 0,
            "events": [],
        }
        if not self.path.exists():
            return empty
        try:
            if self.path.stat().st_size > _MAX_STORE_BYTES:
                raise ControlEventStoreError("event store exceeds the size limit")
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except ControlEventStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ControlEventStoreError(
                f"cannot read event store: {type(exc).__name__}"
            ) from None
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "runtime_epoch",
            "last_event_id",
            "state_version",
            "events",
        }:
            raise ControlEventStoreError("event store has an unsupported shape")
        if value.get("schema_version") != "1.0" or not isinstance(
            value.get("events"), list
        ):
            raise ControlEventStoreError("event store schema is unsupported")
        if value.get("runtime_epoch") != self.runtime_epoch:
            return empty
        if type(value.get("last_event_id")) is not int or type(
            value.get("state_version")
        ) is not int:
            raise ControlEventStoreError("event store counters are invalid")
        return cast(dict[str, object], value)

    def _write_unlocked(self, value: Mapping[str, object]) -> None:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_STORE_BYTES:
            raise ControlEventStoreError("event store exceeds the size limit")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                with contextlib.suppress(OSError):
                    self.path.chmod(0o600)
            finally:
                with contextlib.suppress(OSError):
                    Path(temporary).unlink()
        except OSError as exc:
            raise ControlEventStoreError(
                f"cannot write event store: {type(exc).__name__}"
            ) from None


def default_control_event_store_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environment is None else environment
    override = env.get("LINGXI_CONTROL_EVENT_STORE", "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ControlEventStoreError(
                "LINGXI_CONTROL_EVENT_STORE must be an absolute path"
            )
        return path.resolve()
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise ControlEventStoreError("LOCALAPPDATA is unavailable")
    return (
        Path(local_app_data)
        / "agent-workflow-hub"
        / "lingxi-control-events.json"
    ).resolve()


__all__ = [
    "ControlEvent",
    "ControlEventStore",
    "ControlEventStoreError",
    "default_control_event_store_path",
]
