"""Hermes Desktop JSON-RPC transport used for shared, multi-client sessions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

from .contracts import BridgeAttachment

_ATTACHABLE_PURPOSES = frozenset(("dashboard", "serve"))
_LOOPBACK_DIALABLE = frozenset(("", "0.0.0.0", "127.0.0.1", "::", "::1", "localhost"))
_PUBLISHED_ROLES = ("serve", "desktop-serve")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
_MAX_CONTROL_FILE_BYTES = 1_048_576
_MAX_FRAME_CHARS = 4_194_304


class DesktopGatewayError(RuntimeError):
    """A safe, non-secret-bearing desktop gateway failure."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True, slots=True)
class BackendRecord:
    pid: int
    host: str
    port: int
    purpose: str
    registered_at: float


@dataclass(frozen=True, slots=True)
class DiscoveredBackend:
    record: BackendRecord
    token: str = field(repr=False)

    @property
    def websocket_url(self) -> str:
        return f"ws://127.0.0.1:{self.record.port}/api/ws?token={quote(self.token, safe='')}"


@dataclass(frozen=True, slots=True)
class SharedTurnResult:
    stored_session_id: str
    runtime_session_id: str
    status: str
    text: str
    user_row_id: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CreatedSessionResult:
    stored_session_id: str
    runtime_session_id: str


@dataclass(frozen=True, slots=True)
class CompressionResult:
    stored_session_id: str
    runtime_session_id: str
    status: str
    removed_message_count: int | None = None
    before_messages: int | None = None
    after_messages: int | None = None
    message: str = ""


class WebSocketLike(Protocol):
    def send(self, message: str) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...
    def close(self) -> None: ...


ConnectWebSocket = Callable[..., WebSocketLike]


def parse_spawn_ledger(contents: str) -> tuple[BackendRecord, ...]:
    try:
        value = json.loads(contents)
    except json.JSONDecodeError:
        return ()
    if not isinstance(value, list):
        return ()
    records: list[BackendRecord] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        pid, port = raw.get("pid"), raw.get("port")
        purpose = raw.get("purpose")
        host = raw.get("host", "")
        if (
            type(pid) is not int
            or pid <= 0
            or type(port) is not int
            or not 1 <= port <= 65535
            or type(purpose) is not str
            or purpose not in _ATTACHABLE_PURPOSES
            or type(host) is not str
            or host.lower() not in _LOOPBACK_DIALABLE
            or raw.get("isolated") is True
        ):
            continue
        registered = raw.get("registered_at", 0)
        records.append(
            BackendRecord(
                pid=pid,
                host=host,
                port=port,
                purpose=purpose,
                registered_at=(
                    float(registered) if type(registered) in (int, float) else 0.0
                ),
            )
        )
    return tuple(sorted(records, key=lambda item: item.registered_at, reverse=True))


def _read_small_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise DesktopGatewayError("Hermes publication file is not a regular file")
    try:
        if path.stat().st_size > _MAX_CONTROL_FILE_BYTES:
            raise DesktopGatewayError("Hermes publication file exceeds the size limit")
        return path.read_text(encoding="utf-8")
    except DesktopGatewayError:
        raise
    except (OSError, UnicodeError):
        raise DesktopGatewayError("cannot read Hermes publication file") from None


def _published_token(record: BackendRecord, lock_dir: Path) -> str | None:
    if lock_dir.is_symlink() or not lock_dir.is_dir():
        return None
    for role in _PUBLISHED_ROLES:
        try:
            publication_text = _read_small_text(lock_dir / f"host-{role}.json")
            token = _read_small_text(lock_dir / f"host-{role}.token").strip()
            publication = json.loads(publication_text)
        except (DesktopGatewayError, json.JSONDecodeError):
            continue
        if not isinstance(publication, dict):
            continue
        fingerprint = publication.get("tokenFingerprint")
        if (
            publication.get("role") != role
            or publication.get("protocolVersion") != 1
            or publication.get("pid") != record.pid
            or publication.get("port") != record.port
            or not token
            or len(token) > 4096
            or type(fingerprint) is not str
            or _FINGERPRINT_RE.fullmatch(fingerprint) is None
            or hashlib.sha256(token.encode("utf-8")).hexdigest()[:16] != fingerprint
        ):
            continue
        return token
    return None


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information, False, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except (AttributeError, OSError):
            return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def discover_desktop_backend(
    *,
    machine_root: Path,
    lock_dir: Path,
    process_alive: Callable[[int], bool] = _pid_alive,
) -> DiscoveredBackend:
    if not machine_root.is_absolute() or not lock_dir.is_absolute():
        raise DesktopGatewayError("Hermes discovery paths must be absolute")
    try:
        ledger = _read_small_text(machine_root / "spawn-ledger.json")
    except DesktopGatewayError as exc:
        raise DesktopGatewayError(f"Hermes serve discovery failed: {exc}") from None
    for record in parse_spawn_ledger(ledger):
        if not process_alive(record.pid):
            continue
        token = _published_token(record, lock_dir)
        if token is not None:
            return DiscoveredBackend(record=record, token=token)
    raise DesktopGatewayError("no live Hermes serve backend with a matching published token")


def default_discovery_paths(
    environment: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    env = os.environ if environment is None else environment
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    user_profile = env.get("USERPROFILE", "").strip()
    if not local_app_data or not user_profile:
        raise DesktopGatewayError("LOCALAPPDATA or USERPROFILE is unavailable")
    machine_root = Path(local_app_data) / "hermes"
    lock_override = env.get("HERMES_GATEWAY_LOCK_DIR", "").strip()
    lock_dir = (
        Path(lock_override)
        if lock_override
        else Path(user_profile) / ".local" / "state" / "hermes" / "gateway-locks"
    )
    if not machine_root.is_absolute() or not lock_dir.is_absolute():
        raise DesktopGatewayError("Hermes discovery paths must be absolute")
    return machine_root.resolve(), lock_dir.resolve()


def _default_connect(uri: str, **kwargs: object) -> WebSocketLike:
    try:
        from websockets.sync.client import connect
    except ImportError:
        raise DesktopGatewayError("the websockets package is required") from None
    try:
        return cast(WebSocketLike, connect(uri, **kwargs))
    except Exception as exc:
        raise DesktopGatewayError(
            f"cannot connect to Hermes Desktop gateway: {type(exc).__name__}"
        ) from None


class _RpcConnection:
    def __init__(
        self,
        socket: WebSocketLike,
        *,
        request_timeout_seconds: float,
    ) -> None:
        self.socket = socket
        self.request_timeout_seconds = request_timeout_seconds
        self.events: list[dict[str, object]] = []

    def _receive(self, timeout: float) -> dict[str, object]:
        try:
            raw = self.socket.recv(timeout=timeout)
        except Exception as exc:
            raise DesktopGatewayError(
                f"Hermes gateway receive failed: {type(exc).__name__}",
                outcome_unknown=True,
            ) from None
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise DesktopGatewayError("Hermes gateway returned non-UTF-8 data") from None
        if type(raw) is not str or len(raw) > _MAX_FRAME_CHARS:
            raise DesktopGatewayError("Hermes gateway frame is invalid or too large")
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            raise DesktopGatewayError("Hermes gateway returned invalid JSON") from None
        if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
            raise DesktopGatewayError("Hermes gateway returned an invalid JSON-RPC frame")
        return cast(dict[str, object], frame)

    def _handle_unsolicited(self, frame: dict[str, object]) -> None:
        if frame.get("method") == "event" and isinstance(frame.get("params"), dict):
            self.events.append(cast(dict[str, object], frame["params"]))
            return
        if type(frame.get("id")) is str and type(frame.get("method")) is str:
            self.socket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": frame["id"],
                        "error": {
                            "code": -32601,
                            "message": "Xiaozhi bridge does not answer Hermes UI requests",
                        },
                    },
                    separators=(",", ":"),
                )
            )

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout_seconds: float | None = None,
        mutating: bool = False,
    ) -> dict[str, object]:
        request_id = uuid.uuid4().hex
        try:
            self.socket.send(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": method,
                        "params": dict(params),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            )
        except Exception as exc:
            raise DesktopGatewayError(
                f"Hermes {method} request send failed: {type(exc).__name__}",
                outcome_unknown=True,
            ) from None
        deadline = time.monotonic() + (
            self.request_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DesktopGatewayError(
                    f"Hermes {method} response timed out", outcome_unknown=True
                )
            try:
                frame = self._receive(remaining)
            except DesktopGatewayError as exc:
                if mutating and not exc.outcome_unknown:
                    raise DesktopGatewayError(
                        str(exc), outcome_unknown=True
                    ) from None
                raise
            if frame.get("id") != request_id:
                self._handle_unsolicited(frame)
                continue
            if isinstance(frame.get("error"), dict):
                error = cast(dict[str, object], frame["error"])
                message = error.get("message")
                raise DesktopGatewayError(
                    f"Hermes {method} rejected the request: "
                    f"{message if type(message) is str else 'unknown error'}"
                )
            result = frame.get("result")
            if not isinstance(result, dict):
                raise DesktopGatewayError(
                    f"Hermes {method} returned no object result",
                    outcome_unknown=mutating,
                )
            return cast(dict[str, object], result)

    def wait_for_completion(
        self,
        *,
        runtime_session_id: str,
        user_row_id: int | None,
        timeout_seconds: float,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self.events:
                event = self.events.pop(0)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DesktopGatewayError(
                        "Hermes turn completion timed out", outcome_unknown=True
                    )
                frame = self._receive(remaining)
                if frame.get("method") != "event" or not isinstance(
                    frame.get("params"), dict
                ):
                    self._handle_unsolicited(frame)
                    continue
                event = cast(dict[str, object], frame["params"])
            if (
                event.get("type") != "message.complete"
                or event.get("session_id") != runtime_session_id
                or not isinstance(event.get("payload"), dict)
            ):
                continue
            payload = cast(dict[str, object], event["payload"])
            persisted = payload.get("persisted_turn")
            if user_row_id is not None and isinstance(persisted, dict):
                persisted_user = persisted.get("user_row_id")
                if type(persisted_user) is int and persisted_user != user_row_id:
                    continue
            return payload


class HermesDesktopGateway:
    """Attach as an additional frontend to the existing Hermes serve process."""

    def __init__(
        self,
        backend: DiscoveredBackend,
        *,
        connect_websocket: ConnectWebSocket = _default_connect,
        request_timeout_seconds: float = 15.0,
        turn_timeout_seconds: float = 300.0,
        compression_timeout_seconds: float = 720.0,
    ) -> None:
        self.backend = backend
        self._connect_websocket = connect_websocket
        self._request_timeout_seconds = request_timeout_seconds
        self._turn_timeout_seconds = turn_timeout_seconds
        self._compression_timeout_seconds = compression_timeout_seconds

    def _connect(self) -> tuple[WebSocketLike, _RpcConnection]:
        socket = self._connect_websocket(
            self.backend.websocket_url,
            open_timeout=self._request_timeout_seconds,
            close_timeout=2,
            max_size=_MAX_FRAME_CHARS,
            proxy=None,
        )
        rpc = _RpcConnection(
            socket, request_timeout_seconds=self._request_timeout_seconds
        )
        deadline = time.monotonic() + self._request_timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                socket.close()
                raise DesktopGatewayError("Hermes gateway.ready timed out")
            frame = rpc._receive(remaining)
            if frame.get("method") == "event" and isinstance(frame.get("params"), dict):
                event = cast(dict[str, object], frame["params"])
                if event.get("type") == "gateway.ready":
                    break
                rpc.events.append(event)
                continue
            rpc._handle_unsolicited(frame)
        rpc.request("client.capabilities", {"server_requests": False})
        return socket, rpc

    def list_sessions(self, profile: str, *, limit: int = 20) -> tuple[dict[str, object], ...]:
        if not 1 <= limit <= 100:
            raise DesktopGatewayError("session list limit must be in 1..100")
        socket, rpc = self._connect()
        try:
            result = rpc.request("session.list", {"profile": profile, "limit": limit})
        finally:
            socket.close()
        rows = result.get("sessions")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise DesktopGatewayError("Hermes session.list returned invalid sessions")
        return tuple(cast(dict[str, object], row) for row in rows)

    def create_session(
        self,
        *,
        profile: str,
        idempotency_key: str,
    ) -> CreatedSessionResult:
        if not idempotency_key.strip() or len(idempotency_key) > 256:
            raise DesktopGatewayError("session create idempotency key is invalid")
        socket, rpc = self._connect()
        try:
            result = rpc.request(
                "session.create",
                {
                    "profile": profile,
                    "source": "voice-live",
                    "close_on_disconnect": False,
                    "idempotency_key": idempotency_key,
                },
                mutating=True,
            )
        finally:
            socket.close()
        runtime_id = result.get("session_id")
        stored_id = result.get("stored_session_id")
        if type(runtime_id) is not str or not runtime_id:
            raise DesktopGatewayError(
                "Hermes session.create returned no runtime session id",
                outcome_unknown=True,
            )
        if type(stored_id) is not str or not stored_id:
            raise DesktopGatewayError(
                "Hermes session.create returned no stored session id",
                outcome_unknown=True,
            )
        return CreatedSessionResult(
            stored_session_id=stored_id,
            runtime_session_id=runtime_id,
        )

    def compress_session(
        self,
        *,
        profile: str,
        stored_session_id: str,
    ) -> CompressionResult:
        socket, rpc = self._connect()
        submitted = False
        try:
            resumed = rpc.request(
                "session.resume",
                {
                    "session_id": stored_session_id,
                    "profile": profile,
                    "close_on_disconnect": False,
                    "inline_images": False,
                    "omit_messages": True,
                },
            )
            runtime_id = resumed.get("session_id")
            if type(runtime_id) is not str or not runtime_id:
                raise DesktopGatewayError(
                    "Hermes session.resume returned no runtime session id"
                )
            if resumed.get("running") is True or resumed.get("status") in {
                "working",
                "streaming",
                "resuming",
                "compressing",
            }:
                raise DesktopGatewayError(
                    "Hermes conversation is busy; wait before compressing"
                )
            result = rpc.request(
                "session.compress",
                {"session_id": runtime_id, "profile": profile},
                timeout_seconds=self._compression_timeout_seconds,
                mutating=True,
            )
            submitted = True
        except DesktopGatewayError as exc:
            if submitted and not exc.outcome_unknown:
                raise DesktopGatewayError(str(exc), outcome_unknown=True) from None
            raise
        finally:
            socket.close()
        status = result.get("status")
        if status not in {"compressed", "aborted", "pending"}:
            if result.get("compressed") is False and result.get("lock_held") is True:
                status = "aborted"
            else:
                raise DesktopGatewayError(
                    "Hermes session.compress returned an ambiguous result",
                    outcome_unknown=True,
                )
        info = result.get("info")
        replacement = info.get("stored_session_id") if isinstance(info, dict) else None
        if status == "compressed" and (type(replacement) is not str or not replacement):
            raise DesktopGatewayError(
                "Hermes session.compress omitted the replacement stored session id",
                outcome_unknown=True,
            )
        final_stored_id = (
            replacement
            if status == "compressed" and type(replacement) is str and replacement
            else stored_session_id
        )
        message = result.get("message")
        return CompressionResult(
            stored_session_id=final_stored_id,
            runtime_session_id=runtime_id,
            status=status,
            removed_message_count=(
                result.get("removed") if type(result.get("removed")) is int else None
            ),
            before_messages=(
                result.get("before_messages")
                if type(result.get("before_messages")) is int
                else None
            ),
            after_messages=(
                result.get("after_messages")
                if type(result.get("after_messages")) is int
                else None
            ),
            message=message if type(message) is str else "",
        )

    def send_to_session(
        self,
        *,
        profile: str,
        stored_session_id: str,
        text: str,
        attachments: tuple[BridgeAttachment, ...] = (),
    ) -> SharedTurnResult:
        clean_text = text.strip()
        if not clean_text:
            raise DesktopGatewayError("prompt must not be blank")
        socket, rpc = self._connect()
        submitted = False
        queued_image_paths: list[str] = []
        try:
            resumed = rpc.request(
                "session.resume",
                {
                    "session_id": stored_session_id,
                    "profile": profile,
                    "close_on_disconnect": False,
                    "inline_images": False,
                },
            )
            runtime_id = resumed.get("session_id")
            if type(runtime_id) is not str or not runtime_id:
                raise DesktopGatewayError("Hermes session.resume returned no runtime session id")
            if resumed.get("running") is True or resumed.get("status") in {
                "working",
                "streaming",
                "resuming",
            }:
                raise DesktopGatewayError(
                    "Hermes conversation is busy; wait for the desktop turn to finish"
                )
            prompt_text = clean_text
            file_refs: list[str] = []
            for attachment in attachments:
                common = {"session_id": runtime_id, "profile": profile}
                if attachment.kind == "text":
                    attached = rpc.request(
                        "file.attach",
                        {
                            **common,
                            "path": str(attachment.path),
                            "name": attachment.name,
                        },
                        mutating=True,
                    )
                    ref_text = attached.get("ref_text")
                    if attached.get("attached") is not True or type(ref_text) is not str:
                        raise DesktopGatewayError("Hermes did not accept the file attachment")
                    file_refs.append(cast(str, ref_text))
                elif attachment.kind == "pdf":
                    attached = rpc.request(
                        "pdf.attach",
                        {
                            **common,
                            "path": str(attachment.path),
                            "filename": attachment.name,
                        },
                        mutating=True,
                    )
                    if attached.get("attached") is not True:
                        raise DesktopGatewayError("Hermes did not accept the PDF attachment")
                    pages = attached.get("pages", [])
                    if isinstance(pages, list):
                        queued_image_paths.extend(
                            cast(str, page["path"])
                            for page in pages
                            if isinstance(page, Mapping) and type(page.get("path")) is str
                        )
                else:
                    attached = rpc.request(
                        "image.attach",
                        {**common, "path": str(attachment.path)},
                        mutating=True,
                    )
                    attached_path = attached.get("path")
                    if attached.get("attached") is not True or type(attached_path) is not str:
                        raise DesktopGatewayError("Hermes did not accept the image attachment")
                    queued_image_paths.append(cast(str, attached_path))
            if file_refs:
                prompt_text = f"{clean_text}\n\n" + "\n".join(file_refs)
            response = rpc.request(
                "prompt.submit",
                {
                    "session_id": runtime_id,
                    "profile": profile,
                    "text": prompt_text,
                    "surface": "voice-live",
                },
                mutating=True,
            )
            submitted = True
            status = response.get("status")
            if status != "streaming":
                raise DesktopGatewayError(
                    f"Hermes accepted the prompt with unsupported status: {status}",
                    outcome_unknown=True,
                )
            user_row_id = response.get("user_row_id")
            if type(user_row_id) is not int:
                user_row_id = None
            payload = rpc.wait_for_completion(
                runtime_session_id=runtime_id,
                user_row_id=cast(int | None, user_row_id),
                timeout_seconds=self._turn_timeout_seconds,
            )
            final_status = payload.get("status")
            final_text = payload.get("text", "")
            if type(final_status) is not str:
                final_status = "complete"
            if type(final_text) is not str:
                final_text = json.dumps(final_text, ensure_ascii=False, separators=(",", ":"))
            return SharedTurnResult(
                stored_session_id=stored_session_id,
                runtime_session_id=runtime_id,
                status=cast(str, final_status),
                text=cast(str, final_text),
                user_row_id=cast(int | None, user_row_id),
            )
        except DesktopGatewayError as exc:
            if not submitted and queued_image_paths and not exc.outcome_unknown:
                for queued_path in reversed(queued_image_paths):
                    try:
                        rpc.request(
                            "image.detach",
                            {
                                "session_id": runtime_id,
                                "profile": profile,
                                "path": queued_path,
                            },
                            mutating=True,
                        )
                    except DesktopGatewayError:
                        raise DesktopGatewayError(
                            "Hermes attachment rollback failed",
                            outcome_unknown=True,
                        ) from None
            if submitted and not exc.outcome_unknown:
                raise DesktopGatewayError(str(exc), outcome_unknown=True) from None
            raise
        finally:
            socket.close()


__all__ = [
    "BackendRecord",
    "CompressionResult",
    "CreatedSessionResult",
    "DesktopGatewayError",
    "DiscoveredBackend",
    "HermesDesktopGateway",
    "SharedTurnResult",
    "default_discovery_paths",
    "discover_desktop_backend",
    "parse_spawn_ledger",
]
