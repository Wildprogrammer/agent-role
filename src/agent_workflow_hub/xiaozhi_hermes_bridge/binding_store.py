"""Persistent device-to-host-session bindings for the Xiaozhi bridge."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import cast


_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_HOSTS = frozenset(("hermes", "deepseek-harness"))
_MAX_STATE_BYTES = 1_048_576


class BindingStoreError(RuntimeError):
    """Raised when binding state is malformed or cannot be saved safely."""


class BindingConflictError(BindingStoreError):
    """Raised when a binding changed after a lifecycle action was prepared."""


@dataclass(frozen=True, slots=True)
class SessionBinding:
    device_id: str
    profile: str
    stored_session_id: str
    title: str = ""
    revision: int = 1
    host: str = "hermes"

    def __post_init__(self) -> None:
        if _DEVICE_ID_RE.fullmatch(self.device_id) is None:
            raise BindingStoreError("device_id contains unsupported characters")
        if _PROFILE_RE.fullmatch(self.profile) is None:
            raise BindingStoreError("profile contains unsupported characters")
        if self.host not in _HOSTS:
            raise BindingStoreError("binding host is unsupported")
        if not self.stored_session_id.strip() or len(self.stored_session_id) > 512:
            raise BindingStoreError("stored_session_id must contain 1..512 characters")
        if len(self.title) > 1_000:
            raise BindingStoreError("binding title exceeds 1000 characters")
        if type(self.revision) is not int or self.revision < 1:
            raise BindingStoreError("binding revision must be a positive integer")


class BindingStore:
    """Small closed-schema JSON store written with atomic replacement."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise BindingStoreError("binding store path must be absolute")
        self.path = path.resolve()

    @contextlib.contextmanager
    def _exclusive_lock(
        self, lock_path: Path, *, timeout_seconds: float, timeout_message: str
    ):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.is_symlink():
            raise BindingStoreError("binding lock path must not be a symlink")
        try:
            handle = lock_path.open("a+b")
        except OSError as exc:
            raise BindingStoreError(
                f"cannot open binding lock: {type(exc).__name__}"
            ) from None
        deadline = time.monotonic() + timeout_seconds
        acquired = False
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise BindingStoreError(timeout_message) from None
                    time.sleep(0.05)
            yield
        finally:
            if acquired:
                with contextlib.suppress(OSError):
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.lock")
        with self._exclusive_lock(
            lock_path,
            timeout_seconds=5.0,
            timeout_message="timed out waiting for the binding lock",
        ):
            yield

    @contextlib.contextmanager
    def device_operation(self, device_id: str):
        """Hold the cross-process mutation/write lock for one stable device."""

        if _DEVICE_ID_RE.fullmatch(device_id) is None:
            raise BindingStoreError("device_id contains unsupported characters")
        digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
        lock_path = self.path.with_name(
            f"{self.path.name}.device-{digest[:32]}.lock"
        )
        with self._exclusive_lock(
            lock_path,
            timeout_seconds=0.25,
            timeout_message="another operation is already in progress for this device",
        ):
            yield

    def _read_unlocked(self) -> dict[str, SessionBinding]:
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > _MAX_STATE_BYTES:
                raise BindingStoreError("binding store exceeds the size limit")
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except BindingStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BindingStoreError(
                f"cannot read binding store: {type(exc).__name__}"
            ) from None
        if not isinstance(value, dict) or set(value) != {"schema_version", "bindings"}:
            raise BindingStoreError("binding store has an unsupported shape")
        schema_version = value.get("schema_version")
        if schema_version not in {"1.0", "1.1", "1.2"} or not isinstance(value.get("bindings"), dict):
            raise BindingStoreError("binding store has an unsupported schema version")
        bindings: dict[str, SessionBinding] = {}
        for key, raw in cast(dict[object, object], value["bindings"]).items():
            if type(key) is not str or not isinstance(raw, dict):
                raise BindingStoreError("binding store contains an invalid entry")
            expected_fields = {"device_id", "profile", "stored_session_id", "title"}
            if schema_version in {"1.1", "1.2"}:
                expected_fields.add("revision")
            if schema_version == "1.2":
                expected_fields.add("host")
            if set(raw) != expected_fields:
                raise BindingStoreError("binding store contains an unknown field")
            if not all(
                type(raw.get(name)) is str
                for name in ("device_id", "profile", "stored_session_id", "title")
            ):
                raise BindingStoreError("binding store fields must be strings")
            if schema_version == "1.2" and type(raw.get("host")) is not str:
                raise BindingStoreError("binding host must be a string")
            binding = SessionBinding(
                device_id=cast(str, raw["device_id"]),
                profile=cast(str, raw["profile"]),
                stored_session_id=cast(str, raw["stored_session_id"]),
                host=cast(str, raw["host"]) if schema_version == "1.2" else "hermes",
                title=cast(str, raw["title"]),
                revision=(
                    cast(int, raw["revision"])
                    if schema_version in {"1.1", "1.2"}
                    else 1
                ),
            )
            if key != binding.device_id or key in bindings:
                raise BindingStoreError("binding key does not match device_id")
            bindings[key] = binding
        return bindings

    def get(self, device_id: str) -> SessionBinding | None:
        if _DEVICE_ID_RE.fullmatch(device_id) is None:
            raise BindingStoreError("device_id contains unsupported characters")
        with self._locked():
            return self._read_unlocked().get(device_id)

    def list(self) -> tuple[SessionBinding, ...]:
        with self._locked():
            bindings = self._read_unlocked()
            return tuple(bindings[key] for key in sorted(bindings))

    def bind(self, binding: SessionBinding) -> SessionBinding:
        with self._locked():
            bindings = self._read_unlocked()
            current = bindings.get(binding.device_id)
            persisted = replace(
                binding,
                revision=current.revision + 1 if current is not None else 1,
            )
            bindings[binding.device_id] = persisted
            self._write_unlocked(bindings)
            return persisted

    def compare_and_swap(
        self,
        expected: SessionBinding,
        replacement: SessionBinding,
    ) -> SessionBinding:
        if expected.device_id != replacement.device_id:
            raise BindingStoreError("binding replacement device_id does not match")
        with self._locked():
            bindings = self._read_unlocked()
            if bindings.get(expected.device_id) != expected:
                raise BindingConflictError("device binding changed before replacement")
            persisted = replace(replacement, revision=expected.revision + 1)
            bindings[expected.device_id] = persisted
            self._write_unlocked(bindings)
            return persisted

    def unbind(self, device_id: str) -> bool:
        with self._locked():
            bindings = self._read_unlocked()
            removed = bindings.pop(device_id, None) is not None
            if removed:
                self._write_unlocked(bindings)
            return removed

    def _write_unlocked(self, bindings: dict[str, SessionBinding]) -> None:
        value = {
            "schema_version": "1.2",
            "bindings": {
                key: asdict(bindings[key]) for key in sorted(bindings)
            },
        }
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > _MAX_STATE_BYTES:
            raise BindingStoreError("binding store exceeds the size limit")
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
                replace_deadline = time.monotonic() + 2.0
                while True:
                    try:
                        os.replace(temporary, self.path)
                        break
                    except PermissionError:
                        if time.monotonic() >= replace_deadline:
                            raise
                        time.sleep(0.05)
                with contextlib.suppress(OSError):
                    self.path.chmod(0o600)
            finally:
                with contextlib.suppress(OSError):
                    Path(temporary).unlink()
        except OSError as exc:
            raise BindingStoreError(
                f"cannot write binding store: {type(exc).__name__}"
            ) from None


def default_binding_store_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environment is None else environment
    override = env.get("XIAOZHI_HERMES_BINDING_STORE", "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise BindingStoreError(
                "XIAOZHI_HERMES_BINDING_STORE must be an absolute path"
            )
        return path.resolve()
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise BindingStoreError("LOCALAPPDATA is unavailable")
    return (
        Path(local_app_data)
        / "agent-workflow-hub"
        / "xiaozhi-hermes-bindings.json"
    ).resolve()

__all__ = [
    "BindingConflictError",
    "BindingStore",
    "BindingStoreError",
    "SessionBinding",
    "default_binding_store_path",
]
