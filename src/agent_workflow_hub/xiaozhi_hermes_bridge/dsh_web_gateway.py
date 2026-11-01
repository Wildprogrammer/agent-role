"""Authenticated DeepSeek Harness Web Session Controller adapter.

The adapter deliberately speaks to the same Web host used by the browser.  It
does not boot a second headless Harness runtime, so a Xiaozhi turn and a browser
turn share one canonical Session log and one writer.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import quote, urlsplit

import httpx

from .contracts import BridgeAttachment

from .desktop_gateway import CreatedSessionResult, SharedTurnResult


_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "::1"))
_PRESET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SUPPORTED_VERSION = "0.2.0-rc.2"
_OUTCOME_SENSITIVE_METHODS = frozenset(
    ("session/prompt", "session/create", "session/cancel")
)
_TURN_STATUS = {
    "completed": "complete",
    "aborted": "cancelled",
    "cancelled": "cancelled",
    "blocked": "failed",
    "max-tokens": "failed",
    "forked": "interrupted",
    "interrupted": "interrupted",
    "failed": "failed",
}


class DshGatewayError(RuntimeError):
    """Raised when a DSH request is rejected or its outcome is unknown."""

    def __init__(self, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class HttpClientLike(Protocol):
    @property
    def cookies(self) -> object: ...

    def get(self, url: str, **kwargs: object) -> object: ...

    def post(self, url: str, **kwargs: object) -> object: ...


@dataclass(frozen=True, slots=True)
class DshRuntimeFacts:
    version: str
    preset_count: int
    session_count: int


def validate_dsh_origin(value: str) -> str:
    """Return a normalized numeric-loopback HTTP origin."""

    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise DshGatewayError("DSH base URL is invalid") from None
    expected_netloc = f"[{host}]" if host is not None and ":" in host else host or ""
    if port is not None:
        expected_netloc += f":{port}"
    if (
        parsed.scheme.lower() != "http"
        or host not in _LOOPBACK_HOSTS
        or parsed.netloc != expected_netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DshGatewayError("DSH base URL must be a numeric-loopback HTTP origin")
    return f"http://{expected_netloc}"


def read_dsh_version(dsh_home: Path) -> str:
    """Read only the installed DSH package version from an explicit home."""

    if not dsh_home.is_absolute():
        raise DshGatewayError("DSH home must be absolute")
    package = (
        dsh_home
        / "profiles"
        / "node_modules"
        / "@deepseek-ai"
        / "dsh"
        / "package.json"
    )
    try:
        if package.is_symlink() or package.stat().st_size > 1_048_576:
            raise DshGatewayError("DSH package metadata is unsafe")
        import json

        value = json.loads(package.read_text(encoding="utf-8"))
    except DshGatewayError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise DshGatewayError("DSH package metadata is unavailable") from None
    version = value.get("version") if isinstance(value, dict) else None
    if type(version) is not str:
        raise DshGatewayError("DSH package version is unavailable")
    return version


class DeepSeekHarnessWebGateway:
    """Drive the authenticated DSH Web host through its typed Remote API."""

    def __init__(
        self,
        *,
        base_url: str,
        launch_token: str,
        dsh_home: Path,
        expected_version: str = _SUPPORTED_VERSION,
        allowed_presets: tuple[str, ...] = (),
        request_timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 1.0,
        max_polls: int = 120,
        client: HttpClientLike | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = validate_dsh_origin(base_url)
        if not launch_token.strip() or len(launch_token) > 4096:
            raise DshGatewayError("DSH launch token is unavailable")
        if expected_version != _SUPPORTED_VERSION:
            raise DshGatewayError("DSH expected version is not supported")
        installed = read_dsh_version(dsh_home)
        if installed != expected_version:
            raise DshGatewayError(
                f"DSH version is unverified: expected {expected_version}, found {installed}"
            )
        if any(_PRESET_RE.fullmatch(item) is None for item in allowed_presets):
            raise DshGatewayError("DSH preset allowlist contains an invalid name")
        if len(set(allowed_presets)) != len(allowed_presets):
            raise DshGatewayError("DSH preset allowlist contains duplicates")
        self.version = installed
        self._allowed_presets = frozenset(allowed_presets)
        self._timeout = request_timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._max_polls = max_polls
        self._sleep = sleep
        self._client = client or httpx.Client(
            follow_redirects=False,
            timeout=request_timeout_seconds,
            trust_env=False,
        )
        self._authenticate(launch_token)

    def _authenticate(self, launch_token: str) -> None:
        try:
            response = self._client.get(
                f"{self.base_url}/?token={quote(launch_token, safe='')}",
                headers={"host": urlsplit(self.base_url).netloc},
            )
        except Exception:
            raise DshGatewayError("cannot reach the DSH Web host") from None
        status = getattr(response, "status_code", None)
        if status not in {302, 303, 307, 308}:
            raise DshGatewayError("DSH Web authentication was rejected")
        location = getattr(response, "headers", {}).get("location")
        if location not in {"/", "./"}:
            raise DshGatewayError("DSH Web authentication returned an unsafe redirect")
        cookies = getattr(self._client, "cookies", None)
        if cookies is None or len(cookies) == 0:  # type: ignore[arg-type]
            raise DshGatewayError("DSH Web authentication did not establish a session")

    @staticmethod
    def _mapping(value: object, *, name: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping) or not all(type(key) is str for key in value):
            raise DshGatewayError(f"DSH returned an invalid {name}")
        return cast(Mapping[str, object], value)

    def _rpc(self, method: str, args: Mapping[str, object]) -> object:
        rpc_id = str(uuid.uuid4())
        outcome_sensitive = method in _OUTCOME_SENSITIVE_METHODS
        body = {
            "type": "client-request",
            "rpcId": rpc_id,
            "method": method,
            "payload": {"args": dict(args)},
        }
        try:
            response = self._client.post(
                f"{self.base_url}/api/{method}",
                headers={"content-type": "application/json"},
                json=body,
                timeout=self._timeout,
            )
        except Exception:
            raise DshGatewayError(
                f"DSH {method} transport failed", outcome_unknown=outcome_sensitive
            ) from None
        if getattr(response, "status_code", None) != 200:
            raise DshGatewayError(
                f"DSH {method} returned an HTTP error",
                outcome_unknown=outcome_sensitive,
            )
        try:
            envelope = self._mapping(response.json(), name="RPC envelope")  # type: ignore[attr-defined]
        except DshGatewayError:
            raise DshGatewayError(
                f"DSH {method} returned an invalid RPC envelope",
                outcome_unknown=outcome_sensitive,
            ) from None
        except Exception:
            raise DshGatewayError(
                f"DSH {method} returned invalid JSON",
                outcome_unknown=outcome_sensitive,
            ) from None
        if envelope.get("type") != "server-response" or envelope.get("rpcId") != rpc_id:
            raise DshGatewayError(
                f"DSH {method} returned a mismatched response",
                outcome_unknown=outcome_sensitive,
            )
        try:
            result = self._mapping(envelope.get("result"), name="RPC result")
        except DshGatewayError:
            raise DshGatewayError(
                f"DSH {method} returned an invalid RPC result",
                outcome_unknown=outcome_sensitive,
            ) from None
        if result.get("ok") is True:
            return result.get("value")
        error = self._mapping(result.get("error"), name="RPC error")
        code = error.get("code")
        message = error.get("message")
        safe_code = code if type(code) is str else "unknown"
        safe_message = message if type(message) is str else "request rejected"
        raise DshGatewayError(f"DSH {safe_code}: {safe_message}")

    def list_agents(self) -> tuple[dict[str, object], ...]:
        value = self._mapping(self._rpc("agentPresets/list", {}), name="preset roster")
        rows = value.get("presets")
        if type(rows) is not list:
            raise DshGatewayError("DSH preset roster is invalid")
        result: list[dict[str, object]] = []
        for raw in rows:
            row = self._mapping(raw, name="preset row")
            preset_id = row.get("id")
            if type(preset_id) is not str or _PRESET_RE.fullmatch(preset_id) is None:
                raise DshGatewayError("DSH preset roster contains an invalid id")
            if self._allowed_presets and preset_id not in self._allowed_presets:
                continue
            if row.get("broken") is not None:
                continue
            result.append(
                {
                    "host": "deepseek-harness",
                    "id": preset_id,
                    "name": row.get("name") if type(row.get("name")) is str else preset_id,
                    "default": row.get("isDefault") is True,
                    "available": True,
                }
            )
        return tuple(result)

    def _all_sessions(self) -> tuple[dict[str, object], ...]:
        value = self._mapping(
            self._rpc("session/list", {"_request": {}}), name="session list"
        )
        rows = value.get("items")
        if type(rows) is not list:
            raise DshGatewayError("DSH session list is invalid")
        return tuple(dict(self._mapping(row, name="session row")) for row in rows)

    @staticmethod
    def _projection_value(row: Mapping[str, object], key: str) -> object:
        projections = row.get("projections")
        if not isinstance(projections, Mapping):
            return None
        values = projections.get("values")
        return values.get(key) if isinstance(values, Mapping) else None

    def list_sessions(
        self, profile: str, *, limit: int = 20
    ) -> tuple[dict[str, object], ...]:
        available = {cast(str, item["id"]) for item in self.list_agents()}
        if profile not in available:
            raise DshGatewayError("DSH preset is not available")
        result: list[dict[str, object]] = []
        for row in self._all_sessions():
            if self._projection_value(row, "agentPreset") != profile:
                continue
            session_id = row.get("sessionId")
            if type(session_id) is not str or not session_id:
                raise DshGatewayError("DSH session list contains an invalid id")
            title = self._projection_value(row, "title")
            result.append(
                {
                    "id": session_id,
                    "title": title if type(title) is str else "",
                    "updated_at": row.get("updatedAt"),
                    "message_count": None,
                    "running": row.get("running") is True,
                    "blank": row.get("blank") is True,
                    "cwd": row.get("cwd") if type(row.get("cwd")) is str else None,
                }
            )
            if len(result) >= limit:
                break
        return tuple(result)

    def doctor(self) -> DshRuntimeFacts:
        agents = self.list_agents()
        sessions = self._all_sessions()
        return DshRuntimeFacts(
            version=self.version,
            preset_count=len(agents),
            session_count=len(sessions),
        )

    def _session_row(self, session_id: str) -> Mapping[str, object] | None:
        return next(
            (row for row in self._all_sessions() if row.get("sessionId") == session_id),
            None,
        )

    def _projection_seq(self, session_id: str) -> int:
        value = self._rpc(
            "session/projections", {"request": {"sessionId": session_id}}
        )
        if value is None:
            raise DshGatewayError("DSH session is unavailable")
        projection = self._mapping(value, name="session projections")
        seq = projection.get("asOfSeq")
        if type(seq) is not int or seq < 0:
            raise DshGatewayError("DSH session projection cursor is invalid")
        return seq

    def _page(self, session_id: str, through_seq: int) -> tuple[Mapping[str, object], ...]:
        value = self._mapping(
            self._rpc(
                "session/page",
                {
                    "request": {
                        "address": {"kind": "session", "sessionId": session_id},
                        "throughSeq": through_seq,
                        "maxMessages": 500,
                    }
                },
            ),
            name="session page",
        )
        records = value.get("records")
        if type(records) is not list:
            raise DshGatewayError("DSH session page is invalid")
        return tuple(self._mapping(item, name="session record") for item in records)

    @staticmethod
    def _event(record: Mapping[str, object]) -> Mapping[str, object] | None:
        event = record.get("event")
        return cast(Mapping[str, object], event) if isinstance(event, Mapping) else None

    @classmethod
    def _failure_detail(
        cls, reason: Mapping[str, object]
    ) -> tuple[str | None, str | None]:
        error = reason.get("error")
        if not isinstance(error, Mapping):
            return None, None
        code = error.get("code")
        message = error.get("message")
        safe_code = code if type(code) is str and code.strip() else None
        if type(message) is not str:
            return safe_code, None
        safe_message = re.sub(r"[\x00-\x1f\x7f]+", " ", message).strip()
        if len(safe_message) > 1_000:
            safe_message = safe_message[:997] + "..."
        return safe_code, safe_message or None

    @classmethod
    def _turn_result(
        cls,
        records: tuple[Mapping[str, object], ...],
        *,
        request_id: str,
    ) -> tuple[int, str, str, str | None, str | None] | None:
        user_seq: int | None = None
        for record in records:
            event = cls._event(record)
            if event is None or event.get("type") != "user/message":
                continue
            data = event.get("data")
            source = data.get("source") if isinstance(data, Mapping) else None
            if (
                isinstance(source, Mapping)
                and source.get("kind") == "user"
                and source.get("rpcId") == request_id
                and type(event.get("seq")) is int
            ):
                user_seq = cast(int, event["seq"])
                break
        if user_seq is None:
            return None
        final_text = ""
        for record in records:
            event = cls._event(record)
            if event is None or type(event.get("seq")) is not int or cast(int, event["seq"]) <= user_seq:
                continue
            data = event.get("data")
            if event.get("type") == "assistant/message" and isinstance(data, Mapping):
                message = data.get("message")
                content = message.get("content") if isinstance(message, Mapping) else None
                if isinstance(content, list):
                    parts = [
                        block.get("text")
                        for block in content
                        if isinstance(block, Mapping)
                        and block.get("type") == "text"
                        and type(block.get("text")) is str
                    ]
                    if parts:
                        final_text = "\n".join(cast(list[str], parts))
            if event.get("type") == "turn/end" and isinstance(data, Mapping):
                reason = data.get("reason")
                if isinstance(reason, Mapping) and type(reason.get("kind")) is str:
                    error_code, error_message = cls._failure_detail(reason)
                    return (
                        user_seq,
                        cast(str, reason["kind"]),
                        final_text,
                        error_code,
                        error_message,
                    )
                elif type(reason) is str:
                    return user_seq, cast(str, reason), final_text, None, None
        return None

    def send_to_session(
        self,
        *,
        profile: str,
        stored_session_id: str,
        text: str,
        attachments: tuple[BridgeAttachment, ...] = (),
    ) -> SharedTurnResult:
        row = self._session_row(stored_session_id)
        if row is None or self._projection_value(row, "agentPreset") != profile:
            raise DshGatewayError("DSH session is unavailable for the selected preset")
        if row.get("running") is True:
            raise DshGatewayError("DSH session is busy")
        request_id = str(uuid.uuid4())
        content: list[dict[str, str]] = [{"type": "text", "text": text}]
        for attachment in attachments:
            receipt_id = self._upload_attachment(stored_session_id, attachment)
            content.append({"type": "file", "receiptId": receipt_id})
        self._rpc(
            "session/prompt",
            {
                "request": {
                    "requestId": request_id,
                    "sessionId": stored_session_id,
                    "mode": "queue",
                    "content": content,
                    "clientTimeZone": "Asia/Shanghai",
                }
            },
        )
        for _ in range(self._max_polls):
            try:
                seq = self._projection_seq(stored_session_id)
                result = self._turn_result(
                    self._page(stored_session_id, seq), request_id=request_id
                )
            except DshGatewayError:
                result = None
            if result is not None:
                user_seq, status, output, error_code, error_message = result
                public_status = _TURN_STATUS.get(status, "failed")
                return SharedTurnResult(
                    status=public_status,
                    stored_session_id=stored_session_id,
                    runtime_session_id=stored_session_id,
                    text=output,
                    user_row_id=user_seq,
                    error_code=error_code,
                    error_message=error_message,
                )
            self._sleep(self._poll_interval)
        raise DshGatewayError(
            "DSH prompt outcome is unknown after the polling limit",
            outcome_unknown=True,
        )

    def _upload_attachment(
        self, session_id: str, attachment: BridgeAttachment
    ) -> str:
        url = (
            f"{self.base_url}/api/session/uploadFileBinary"
            f"?sessionId={quote(session_id, safe='')}"
            f"&name={quote(attachment.name, safe='')}"
        )

        def chunks() -> Iterator[bytes]:
            with attachment.path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    yield chunk

        try:
            response = self._client.post(
                url,
                headers={"content-type": "application/octet-stream"},
                content=chunks(),
                timeout=self._timeout,
            )
        except OSError:
            raise DshGatewayError("DSH attachment file is unavailable") from None
        except Exception:
            raise DshGatewayError("DSH attachment upload failed") from None
        if getattr(response, "status_code", None) != 200:
            raise DshGatewayError("DSH attachment upload returned an HTTP error")
        try:
            envelope = self._mapping(response.json(), name="attachment upload result")  # type: ignore[attr-defined]
            if envelope.get("ok") is not True:
                raise DshGatewayError("DSH rejected the attachment upload")
            value = self._mapping(envelope.get("value"), name="attachment upload value")
            receipt_id = value.get("receiptId")
        except DshGatewayError:
            raise
        except Exception:
            raise DshGatewayError("DSH returned an invalid attachment receipt") from None
        if type(receipt_id) is not str or not receipt_id or len(receipt_id) > 256:
            raise DshGatewayError("DSH returned an invalid attachment receipt")
        return receipt_id

    def create_session(
        self, *, profile: str, idempotency_key: str
    ) -> CreatedSessionResult:
        available = {cast(str, item["id"]) for item in self.list_agents()}
        if profile not in available:
            raise DshGatewayError("DSH preset is not available")
        session_id = f"xiaozhi-{uuid.uuid5(uuid.NAMESPACE_URL, idempotency_key)}"
        raw_value = self._rpc(
            "session/create",
            {"request": {"sessionId": session_id, "agentPreset": profile}},
        )
        try:
            value = self._mapping(raw_value, name="created session")
        except DshGatewayError:
            raise DshGatewayError(
                f"DSH session creation result is invalid; expected {session_id}",
                outcome_unknown=True,
            ) from None
        created_id = value.get("sessionId")
        if created_id != session_id:
            raise DshGatewayError(
                "DSH session creation returned a mismatched id", outcome_unknown=True
            )
        return CreatedSessionResult(
            stored_session_id=session_id,
            runtime_session_id=session_id,
        )

    def cancel_session(self, session_id: str) -> None:
        value = self._mapping(
            self._rpc("session/cancel", {"request": {"sessionId": session_id}}),
            name="cancel result",
        )
        if value.get("accepted") is not True:
            raise DshGatewayError("DSH cancellation was not accepted")

    def remove_queued_item(self, *, session_id: str, item_id: str) -> None:
        value = self._mapping(
            self._rpc(
                "session/updateQueue",
                {
                    "request": {
                        "sessionId": session_id,
                        "itemId": item_id,
                        "action": {"kind": "remove"},
                    }
                },
            ),
            name="queue update result",
        )
        if value.get("accepted") is not True:
            raise DshGatewayError("DSH queue removal was not accepted")


__all__ = [
    "DeepSeekHarnessWebGateway",
    "DshGatewayError",
    "DshRuntimeFacts",
    "read_dsh_version",
    "validate_dsh_origin",
]
