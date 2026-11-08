"""Loopback-only HTTP surfaces for the Lingxi admin and companion UIs."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import mimetypes
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections.abc import Callable
from typing import cast
from urllib.parse import parse_qs, urlsplit

from .control_plane import ControlPlaneError, DeviceControlCoordinator
from .live_companion import TransientCompanionBroker
from .runtime_settings import RuntimeSettingsError


_MAX_BODY_BYTES = 65_536
_SESSION_TTL_SECONDS = 8 * 60 * 60
_MAX_ACTIVE_SESSIONS = 16
_MAX_FAILED_LOGINS = 5
_FAILED_LOGIN_WINDOW_SECONDS = 30
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


@dataclass(frozen=True, slots=True)
class BrowserSession:
    token: str
    csrf: str
    expires_at: float


class ScopedAuth:
    """Repeatable password login with fixed-scope, bounded browser sessions."""

    def __init__(
        self,
        scope: str,
        password: str,
        *,
        on_session_invalidated: Callable[[str], None] | None = None,
    ) -> None:
        if not password or not password.strip():
            raise ControlPlaneError(
                "invalid_control_password",
                "configured control password must not be empty or whitespace",
                http_status=500,
            )
        if len(password) > 256:
            raise ControlPlaneError(
                "invalid_control_password",
                "configured control password must contain at most 256 characters",
                http_status=500,
            )
        self.scope = scope
        self.cookie_name = f"lingxi_{scope}_session"
        self._password_digest = hashlib.sha256(password.encode("utf-8")).digest()
        self._sessions: dict[str, BrowserSession] = {}
        self._failed_logins: deque[float] = deque()
        self._lock = threading.RLock()
        self._on_session_invalidated = on_session_invalidated or (lambda _token: None)

    def login(self, password: str) -> BrowserSession:
        candidate = hashlib.sha256(password.encode("utf-8")).digest()
        with self._lock:
            now = time.monotonic()
            self._prune_locked(now)
            self._prune_failures_locked(now)
            if len(self._failed_logins) >= _MAX_FAILED_LOGINS:
                raise ControlPlaneError(
                    "login_rate_limited",
                    "too many failed login attempts; try again shortly",
                    http_status=429,
                )
            if not hmac.compare_digest(candidate, self._password_digest):
                self._failed_logins.append(now)
                raise ControlPlaneError(
                    "login_rejected", "password is incorrect", http_status=403
                )
            self._failed_logins.clear()
            while len(self._sessions) >= _MAX_ACTIVE_SESSIONS:
                evicted = self._sessions.pop(next(iter(self._sessions)))
                self._on_session_invalidated(evicted.token)
            session = BrowserSession(
                token=secrets.token_urlsafe(32),
                csrf=secrets.token_urlsafe(24),
                expires_at=now + _SESSION_TTL_SECONDS,
            )
            self._sessions[session.token] = session
            return session

    def authenticate(self, cookie_header: str) -> BrowserSession:
        cookie = SimpleCookie()
        with contextlib.suppress(Exception):
            cookie.load(cookie_header)
        morsel = cookie.get(self.cookie_name)
        token = morsel.value if morsel is not None else ""
        with self._lock:
            self._prune_locked(time.monotonic())
            session = self._sessions.get(token)
        if session is None:
            raise ControlPlaneError(
                "authentication_required", "browser session is not authenticated", http_status=401
            )
        return session

    def validate_csrf(self, session: BrowserSession, value: str) -> None:
        if not value or not hmac.compare_digest(value, session.csrf):
            raise ControlPlaneError(
                "csrf_rejected", "request verification failed", http_status=403
            )

    def _prune_locked(self, now: float) -> None:
        for token, session in list(self._sessions.items()):
            if session.expires_at <= now:
                self._sessions.pop(token, None)
                self._on_session_invalidated(token)

    def _prune_failures_locked(self, now: float) -> None:
        cutoff = now - _FAILED_LOGIN_WINDOW_SECONDS
        while self._failed_logins and self._failed_logins[0] <= cutoff:
            self._failed_logins.popleft()


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        coordinator: DeviceControlCoordinator,
        scope: str,
        auth: ScopedAuth,
        static_root: Path,
        live_companion: TransientCompanionBroker,
    ) -> None:
        self.coordinator = coordinator
        self.scope = scope
        self.auth = auth
        self.static_root = static_root
        self.live_companion = live_companion
        super().__init__(address, handler)


class LingxiRequestHandler(BaseHTTPRequestHandler):
    server_version = "LingxiControl/1.0"
    sys_version = ""

    @property
    def app(self) -> _LoopbackServer:
        return cast(_LoopbackServer, self.server)

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._validate_host()
            parsed = urlsplit(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._serve_static("index-admin.html" if self.app.scope == "admin" else "index-companion.html")
                return
            if parsed.path.startswith("/static/"):
                self._serve_static(parsed.path.removeprefix("/static/"))
                return
            session = self.app.auth.authenticate(self.headers.get("Cookie", ""))
            if parsed.path == "/api/session":
                self._json(
                    HTTPStatus.OK,
                    {"schema_version": "1.0", "scope": self.app.scope, "csrf": session.csrf},
                )
                return
            if parsed.path == "/api/snapshot":
                self._json(HTTPStatus.OK, self.app.coordinator.snapshot())
                return
            if parsed.path == "/api/events" and self.app.scope == "companion":
                query = parse_qs(parsed.query, strict_parsing=False)
                raw = query.get("after", ["0"])[0]
                try:
                    after = int(raw)
                except ValueError:
                    raise ControlPlaneError(
                        "invalid_request", "after must be a non-negative integer", http_status=400
                    ) from None
                self._json(HTTPStatus.OK, self.app.coordinator.events(after_event_id=after))
                return
            if parsed.path == "/api/live" and self.app.scope == "companion":
                query = parse_qs(parsed.query, strict_parsing=False)
                subscription = self._one_query(query, "subscription")
                raw = query.get("after", ["0"])
                if len(raw) != 1:
                    raise ControlPlaneError(
                        "invalid_request", "after is invalid", http_status=400
                    )
                try:
                    after = int(raw[0])
                except ValueError:
                    raise ControlPlaneError(
                        "invalid_request",
                        "after must be a non-negative integer",
                        http_status=400,
                    ) from None
                self._json(
                    HTTPStatus.OK,
                    self.app.live_companion.poll(
                        session_token=session.token,
                        subscription_id=subscription,
                        after_sequence=after,
                    ),
                )
                return
            if parsed.path == "/api/agents" and self.app.scope == "admin":
                self._json(HTTPStatus.OK, self.app.coordinator.list_agents())
                return
            if parsed.path == "/api/sessions" and self.app.scope == "admin":
                try:
                    query = parse_qs(parsed.query, strict_parsing=True)
                except ValueError:
                    raise ControlPlaneError(
                        "invalid_request", "query string is invalid", http_status=400
                    ) from None
                host = self._one_query(query, "host")
                agent = self._one_query(query, "agent")
                raw_limit = query.get("limit", ["20"])
                if len(raw_limit) != 1:
                    raise ControlPlaneError("invalid_request", "limit is invalid", http_status=400)
                try:
                    limit = int(raw_limit[0])
                except ValueError:
                    raise ControlPlaneError("invalid_request", "limit is invalid", http_status=400) from None
                self._json(
                    HTTPStatus.OK,
                    self.app.coordinator.list_sessions(host=host, agent=agent, limit=limit),
                )
                return
            raise ControlPlaneError("not_found", "resource was not found", http_status=404)
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._validate_host()
            self._validate_origin()
            parsed = urlsplit(self.path)
            body = self._read_json()
            if parsed.path == "/api/login":
                self._closed(body, {"password"})
                password = body.get("password")
                if type(password) is not str:
                    raise ControlPlaneError("invalid_request", "password is required", http_status=400)
                session = self.app.auth.login(password)
                self._json(
                    HTTPStatus.OK,
                    {"schema_version": "1.0", "scope": self.app.scope, "csrf": session.csrf},
                    extra_headers={
                        "Set-Cookie": (
                            f"{self.app.auth.cookie_name}={session.token}; Path=/; "
                            f"HttpOnly; SameSite=Strict; Max-Age={_SESSION_TTL_SECONDS}"
                        )
                    },
                )
                return
            session = self.app.auth.authenticate(self.headers.get("Cookie", ""))
            self.app.auth.validate_csrf(session, self.headers.get("X-Lingxi-CSRF", ""))
            if self.app.scope == "admin":
                result = self._post_admin(parsed.path, body)
            else:
                result = self._post_companion(parsed.path, body, session)
            self._json(HTTPStatus.OK, result)
        except Exception as exc:
            self._error(exc)

    def do_HEAD(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        try:
            self._validate_host()
            self._json(
                HTTPStatus.METHOD_NOT_ALLOWED,
                {
                    "schema_version": "1.0",
                    "status": "error",
                    "error": {
                        "code": "method_not_allowed",
                        "message": "request method is not allowed",
                    },
                },
                extra_headers={"Allow": "GET, POST"},
            )
        except Exception as exc:
            self._error(exc)

    def _post_admin(self, path: str, body: dict[str, object]) -> dict[str, object]:
        if path == "/api/bind":
            self._closed(
                body,
                {
                    "device_id",
                    "host",
                    "agent",
                    "stored_session_id",
                    "expected_binding_revision",
                    "expected_generation",
                },
            )
            return self.app.coordinator.bind(
                device_id=self._string(body, "device_id"),
                host=self._string(body, "host"),
                agent=self._string(body, "agent"),
                stored_session_id=self._string(body, "stored_session_id"),
                expected_binding_revision=self._integer(body, "expected_binding_revision"),
                expected_generation=self._string(body, "expected_generation"),
            )
        if path == "/api/unbind":
            self._closed(
                body,
                {"device_id", "expected_binding_revision", "expected_generation"},
            )
            return self.app.coordinator.unbind(
                device_id=self._string(body, "device_id"),
                expected_binding_revision=self._integer(body, "expected_binding_revision"),
                expected_generation=self._string(body, "expected_generation"),
            )
        if path == "/api/diagnostics":
            self._closed(body, {"device_id", "expected_generation"})
            return self.app.coordinator.audio_diagnostics(
                device_id=self._string(body, "device_id"),
                expected_generation=self._string(body, "expected_generation"),
            )
        if path == "/api/config/preview":
            self._closed(body, {"patch", "expected_configured_sha256"})
            patch = body.get("patch")
            if not isinstance(patch, dict) or not all(type(key) is str for key in patch):
                raise ControlPlaneError("invalid_request", "patch must be an object", http_status=400)
            return self.app.coordinator.config_preview(
                cast(dict[str, object], patch),
                expected_configured_sha256=self._string(body, "expected_configured_sha256"),
            )
        if path == "/api/config/apply":
            self._closed(body, {"plan_id", "candidate_sha256"})
            return self.app.coordinator.config_apply(
                plan_id=self._string(body, "plan_id"),
                candidate_sha256=self._string(body, "candidate_sha256"),
            )
        if path == "/api/config/rollback":
            self._closed(body, {"backup_id", "expected_configured_sha256"})
            return self.app.coordinator.config_rollback(
                backup_id=self._string(body, "backup_id"),
                expected_configured_sha256=self._string(body, "expected_configured_sha256"),
            )
        raise ControlPlaneError("not_found", "resource was not found", http_status=404)

    def _post_companion(
        self,
        path: str,
        body: dict[str, object],
        session: BrowserSession,
    ) -> dict[str, object]:
        if path == "/api/live/wait":
            self._closed(body, {"previous"})
            return self.app.live_companion.wait_for_connection(session.token, self._string(body, "previous"))
        if path == "/api/live/subscribe":
            fields = set(body)
            initial_fields = {"device_id", "connection_generation"}
            resume_fields = initial_fields | {"resume_token", "after"}
            if fields not in (initial_fields, resume_fields):
                raise ControlPlaneError(
                    "invalid_request", "request fields are invalid", http_status=400
                )
            return self.app.live_companion.subscribe(
                session_token=session.token,
                device_id=self._string(body, "device_id"),
                connection_generation=self._string(body, "connection_generation"),
                resume_token=(self._string(body, "resume_token") if fields == resume_fields else ""),
                after_sequence=(self._integer(body, "after") if fields == resume_fields else None),
            )
        if path == "/api/live/unsubscribe":
            fields = set(body)
            if fields not in ({"subscription"}, {"subscription", "resume_token"}):
                raise ControlPlaneError(
                    "invalid_request", "request fields are invalid", http_status=400
                )
            self.app.live_companion.unsubscribe(
                session_token=session.token,
                subscription_id=self._string(body, "subscription"),
                resume_token=(self._string(body, "resume_token") if "resume_token" in body else ""),
            )
            return {"schema_version": "1.0", "status": "unsubscribed"}
        if path == "/api/action":
            self._closed(
                body,
                {"device_id", "action_id", "decision", "expected_generation"},
            )
            return self.app.coordinator.consume_action(
                device_id=self._string(body, "device_id"),
                action_id=self._string(body, "action_id"),
                decision=self._string(body, "decision"),
                expected_generation=self._string(body, "expected_generation"),
            )
        if path == "/api/stop":
            self._closed(body, {"device_id", "expected_generation"})
            return self.app.coordinator.stop_speaking(
                device_id=self._string(body, "device_id"),
                expected_generation=self._string(body, "expected_generation"),
            )
        if path == "/api/volume":
            self._closed(body, {"device_id", "volume", "expected_generation"})
            return self.app.coordinator.set_volume(
                device_id=self._string(body, "device_id"),
                volume=self._integer(body, "volume"),
                expected_generation=self._string(body, "expected_generation"),
            )
        raise ControlPlaneError("not_found", "resource was not found", http_status=404)

    def _read_json(self) -> dict[str, object]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ControlPlaneError("invalid_content_type", "application/json is required", http_status=415)
        raw_length = self.headers.get("Content-Length", "")
        try:
            length = int(raw_length)
        except ValueError:
            raise ControlPlaneError("invalid_request", "content length is required", http_status=411) from None
        if not 0 <= length <= _MAX_BODY_BYTES:
            raise ControlPlaneError("request_too_large", "request body is too large", http_status=413)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ControlPlaneError("invalid_json", "request body must be valid JSON", http_status=400) from None
        if not isinstance(value, dict) or not all(type(key) is str for key in value):
            raise ControlPlaneError("invalid_request", "request body must be an object", http_status=400)
        return cast(dict[str, object], value)

    def _validate_host(self) -> None:
        host = self.headers.get("Host", "")
        port = self.app.server_address[1]
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            raise ControlPlaneError("host_rejected", "request host is not allowed", http_status=403)

    def _validate_origin(self) -> None:
        origin = self.headers.get("Origin", "")
        port = self.app.server_address[1]
        if origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            raise ControlPlaneError("origin_rejected", "request origin is not allowed", http_status=403)

    def _serve_static(self, name: str) -> None:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            raise ControlPlaneError("not_found", "resource was not found", http_status=404)
        target = (self.app.static_root / name).resolve()
        try:
            target.relative_to(self.app.static_root)
        except ValueError:
            raise ControlPlaneError("not_found", "resource was not found", http_status=404) from None
        if not target.is_file() or target.is_symlink():
            raise ControlPlaneError("not_found", "resource was not found", http_status=404)
        data = target.read_bytes()
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if mime.startswith("text/") else mime)
        self.send_header("Content-Length", str(len(data)))
        for key, value in _SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(
        self,
        status: HTTPStatus | int,
        value: dict[str, object],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, header_value in _SECURITY_HEADERS.items():
            self.send_header(key, header_value)
        for key, header_value in (extra_headers or {}).items():
            self.send_header(key, header_value)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, ControlPlaneError):
            status, code, message = exc.http_status, exc.code, str(exc)
        elif isinstance(exc, RuntimeSettingsError):
            status, code, message = 409, exc.code, str(exc)
        elif isinstance(
            exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
        ):
            return
        else:
            status, code, message = 500, "internal_error", "control request failed"
        with contextlib.suppress(
            BrokenPipeError, ConnectionResetError, ConnectionAbortedError
        ):
            self._json(
                status,
                {"schema_version": "1.0", "status": "error", "error": {"code": code, "message": message}},
            )

    @staticmethod
    def _closed(value: dict[str, object], required: set[str]) -> None:
        if set(value) != required:
            raise ControlPlaneError("invalid_request", "request fields are invalid", http_status=400)

    @staticmethod
    def _string(value: dict[str, object], key: str) -> str:
        item = value.get(key)
        if type(item) is not str:
            raise ControlPlaneError("invalid_request", f"{key} must be a string", http_status=400)
        return cast(str, item)

    @staticmethod
    def _integer(value: dict[str, object], key: str) -> int:
        item = value.get(key)
        if type(item) is not int:
            raise ControlPlaneError("invalid_request", f"{key} must be an integer", http_status=400)
        return cast(int, item)

    @staticmethod
    def _one_query(value: dict[str, list[str]], key: str) -> str:
        items = value.get(key, [])
        if len(items) != 1 or not items[0]:
            raise ControlPlaneError("invalid_request", f"{key} is required", http_status=400)
        return items[0]


class ControlHttpServers:
    """Two loopback origins with disjoint fixed privileges."""

    def __init__(
        self,
        coordinator: DeviceControlCoordinator,
        *,
        admin_port: int = 8765,
        companion_port: int = 8766,
        static_root: Path | None = None,
        admin_password: str = "admin",
        companion_password: str = "admin",
        live_companion: TransientCompanionBroker | None = None,
    ) -> None:
        if admin_port == companion_port or not all(1024 <= port <= 65535 for port in (admin_port, companion_port)):
            raise ControlPlaneError("invalid_listener", "control listener ports are invalid", http_status=500)
        root = static_root or Path(__file__).with_name("static")
        if not root.is_absolute() or not root.is_dir():
            raise ControlPlaneError("missing_assets", "control UI assets are unavailable", http_status=500)
        self.live_companion = live_companion or TransientCompanionBroker()
        self.auth = {
            "admin": ScopedAuth("admin", admin_password),
            "companion": ScopedAuth(
                "companion",
                companion_password,
                on_session_invalidated=self.live_companion.drop_session,
            ),
        }
        admin_server = _LoopbackServer(
            ("127.0.0.1", admin_port),
            LingxiRequestHandler,
            coordinator=coordinator,
            scope="admin",
            auth=self.auth["admin"],
            static_root=root.resolve(),
            live_companion=self.live_companion,
        )
        try:
            companion_server = _LoopbackServer(
                ("127.0.0.1", companion_port),
                LingxiRequestHandler,
                coordinator=coordinator,
                scope="companion",
                auth=self.auth["companion"],
                static_root=root.resolve(),
                live_companion=self.live_companion,
            )
        except Exception:
            admin_server.server_close()
            raise
        self.servers = {
            "admin": admin_server,
            "companion": companion_server,
        }
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        if self._threads:
            return
        for scope, server in self.servers.items():
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"lingxi-{scope}-http",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def close(self) -> None:
        if self._threads:
            for server in self.servers.values():
                server.shutdown()
            for thread in self._threads:
                thread.join(timeout=2.0)
        for server in self.servers.values():
            server.server_close()
        self._threads.clear()

__all__ = ["ControlHttpServers", "LingxiRequestHandler", "ScopedAuth"]
