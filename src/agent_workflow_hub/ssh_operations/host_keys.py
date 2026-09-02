from __future__ import annotations

import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class HostKeyError(RuntimeError):
    pass


class HostKeyMismatch(HostKeyError):
    pass


class HostKeyLockTimeout(HostKeyError):
    pass


def _public_key_line(key: Any) -> str:
    if isinstance(key, bytes):
        value = key.decode("ascii")
    elif isinstance(key, str):
        value = key
    elif hasattr(key, "export_public_key"):
        exported = key.export_public_key("openssh")
        value = exported.decode("ascii") if isinstance(exported, bytes) else str(exported)
    else:
        raise HostKeyError("unsupported host key representation")
    fields = value.strip().split()
    if len(fields) < 2:
        raise HostKeyError("invalid OpenSSH host key")
    return f"{fields[0]} {fields[1]}"


class KnownHostStore:
    """Small atomic TOFU store scoped to this workflow."""

    def __init__(self, path: Path, *, lock_timeout: float = 5.0) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.lock_timeout = lock_timeout

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_timeout
        descriptor: int | None = None
        while descriptor is None:
            try:
                descriptor = os.open(
                    self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.write(descriptor, str(os.getpid()).encode("ascii"))
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise HostKeyLockTimeout(f"timed out locking {self.path}")
                time.sleep(0.01)
        try:
            yield
        finally:
            os.close(descriptor)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def _entries(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        entries: dict[str, str] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) >= 3:
                entries[fields[0]] = f"{fields[1]} {fields[2]}"
        return entries

    def _atomic_append(self, line: str) -> None:
        existing = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temp_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(prefix)
                handle.write(line.rstrip() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def validate_or_record(
        self, alias: str, host: str, port: int, key: Any
    ) -> str:
        endpoint = f"[{host}]:{port}"
        public_key = _public_key_line(key)
        with self._lock():
            known = self._entries().get(endpoint)
            if known is None:
                safe_alias = alias.replace("\n", " ").replace("\r", " ")
                self._atomic_append(
                    f"{endpoint} {public_key} # alias={safe_alias}"
                )
                return "recorded"
            if known != public_key:
                raise HostKeyMismatch(f"host key changed for {endpoint}")
            return "trusted"


def make_tofu_client(store: KnownHostStore, alias: str) -> Any:
    """Create an AsyncSSH client callback without importing it during doctor use."""
    try:
        import asyncssh
    except ImportError as exc:  # pragma: no cover - exercised by doctor/CLI handling
        raise HostKeyError("AsyncSSH is not installed") from exc

    class TofuSSHClient(asyncssh.SSHClient):
        def validate_host_public_key(
            self, host: str, addr: str, port: int, key: Any
        ) -> bool:
            store.validate_or_record(alias, addr or host, port, key)
            return True

    return TofuSSHClient()
