"""Public MCP facade for one server-owned XiaoAi logical device.

The HTTP listener is intentionally loopback-only.  A separately managed HTTPS
reverse tunnel may publish only ``/mcp``.  Network authentication never reuses
the private stdio ``caller_id``; the latter remains an internal child-process
boundary behind :class:`XiaozhiHermesAdapter`.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import tempfile
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import parse_qs, quote, unquote, urlsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .attachments import (
    AttachmentBinding,
    AttachmentError,
    AttachmentStore,
    MAX_ATTACHMENT_BYTES,
    MAX_HERMES_IMAGE_BYTES,
)
from .xiaozhi_server_adapter import (
    XiaozhiAdapterError,
    XiaozhiHermesAdapter,
    adapter_from_environment,
)


_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ALLOWED_HOSTS = {"hermes", "deepseek-harness"}
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_AGENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


class XiaoAiAdapter(Protocol):
    async def list_agents(self, session_id: str) -> Mapping[str, object]: ...

    async def list_sessions(
        self,
        session_id: str,
        *,
        profile: str,
        limit: int = 10,
        host: str = "hermes",
    ) -> Mapping[str, object]: ...

    async def show_binding(
        self, session_id: str, *, device_id: str
    ) -> Mapping[str, object]: ...

    async def bind_session(
        self,
        session_id: str,
        *,
        device_id: str,
        host: str = "hermes",
        profile: str,
        stored_session_id: str,
        expected_binding_revision: int | None = None,
    ) -> Mapping[str, object]: ...

    async def dispatch_bound(
        self,
        session_id: str,
        *,
        device_id: str,
        prompt: str,
        attachments: Sequence[Mapping[str, object]] = (),
    ) -> Mapping[str, object]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class XiaoAiMcpSettings:
    host: str
    port: int
    token: str
    device_id: str
    session_id: str
    max_body_bytes: int
    requests_per_minute: int
    max_concurrent_requests: int
    body_read_timeout_seconds: int
    max_input_chars: int
    max_output_chars: int
    send_cache_seconds: int
    public_base_url: str = ""
    attachment_root: Path = Path(tempfile.gettempdir()) / "AgentWorkflowHub" / "xiaoai-attachments"
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES
    upload_ttl_seconds: int = 600
    attachment_ttl_seconds: int = 1800

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> XiaoAiMcpSettings:
        env = os.environ if environment is None else environment
        host = env.get("XIAOAI_MCP_HOST", "127.0.0.1").strip()
        if host != "127.0.0.1":
            raise XiaoAiMcpConfigurationError(
                "XIAOAI_MCP_HOST must remain 127.0.0.1; publish it through an HTTPS tunnel"
            )
        port = _bounded_int(env, "XIAOAI_MCP_PORT", 8770, 1024, 65535)
        token = env.get("XIAOAI_MCP_TOKEN", "").strip()
        if len(token) < 32 or len(token) > 512 or any(ch.isspace() for ch in token):
            raise XiaoAiMcpConfigurationError(
                "XIAOAI_MCP_TOKEN must contain 32..512 non-whitespace characters"
            )
        device_id = env.get("XIAOAI_MCP_DEVICE_ID", "").strip()
        if _IDENTITY_RE.fullmatch(device_id) is None:
            raise XiaoAiMcpConfigurationError(
                "XIAOAI_MCP_DEVICE_ID is missing or invalid"
            )
        session_id = env.get("XIAOAI_MCP_SESSION_ID", "xiaoai-mcp").strip()
        if _IDENTITY_RE.fullmatch(session_id) is None:
            raise XiaoAiMcpConfigurationError("XIAOAI_MCP_SESSION_ID is invalid")
        public_base_url = _public_https_origin(
            env.get("XIAOAI_MCP_PUBLIC_BASE_URL", "").strip()
        )
        local_app_data = (
            env.get("LOCALAPPDATA", "").strip()
            or os.environ.get("LOCALAPPDATA", "").strip()
            or tempfile.gettempdir()
        )
        default_attachment_root = str(
            Path(local_app_data) / "AgentWorkflowHub" / "xiaoai-attachments"
        )
        attachment_root = Path(
            env.get("XIAOAI_MCP_ATTACHMENT_ROOT", default_attachment_root).strip()
        )
        if not attachment_root.is_absolute():
            raise XiaoAiMcpConfigurationError(
                "XIAOAI_MCP_ATTACHMENT_ROOT must be an absolute path"
            )
        return cls(
            host=host,
            port=port,
            token=token,
            device_id=device_id,
            session_id=session_id,
            max_body_bytes=_bounded_int(
                env, "XIAOAI_MCP_MAX_BODY_BYTES", 262_144, 4_096, 1_048_576
            ),
            requests_per_minute=_bounded_int(
                env, "XIAOAI_MCP_REQUESTS_PER_MINUTE", 60, 5, 600
            ),
            max_concurrent_requests=_bounded_int(
                env, "XIAOAI_MCP_MAX_CONCURRENT_REQUESTS", 8, 1, 32
            ),
            body_read_timeout_seconds=_bounded_int(
                env, "XIAOAI_MCP_BODY_READ_TIMEOUT_SECONDS", 10, 1, 60
            ),
            max_input_chars=_bounded_int(
                env, "XIAOAI_MCP_MAX_INPUT_CHARS", 8_000, 200, 8_000
            ),
            max_output_chars=_bounded_int(
                env, "XIAOAI_MCP_MAX_OUTPUT_CHARS", 4_000, 200, 20_000
            ),
            send_cache_seconds=_bounded_int(
                env, "XIAOAI_MCP_SEND_CACHE_SECONDS", 600, 30, 3_600
            ),
            public_base_url=public_base_url,
            attachment_root=attachment_root.resolve(),
            max_attachment_bytes=_bounded_int(
                env,
                "XIAOAI_MCP_MAX_ATTACHMENT_BYTES",
                MAX_ATTACHMENT_BYTES,
                1_048_576,
                MAX_ATTACHMENT_BYTES,
            ),
            upload_ttl_seconds=_bounded_int(
                env, "XIAOAI_MCP_UPLOAD_TTL_SECONDS", 600, 60, 1_800
            ),
            attachment_ttl_seconds=_bounded_int(
                env, "XIAOAI_MCP_ATTACHMENT_TTL_SECONDS", 1_800, 300, 3_600
            ),
        )


class XiaoAiMcpConfigurationError(ValueError):
    """The public MCP boundary is not configured safely."""


def _public_https_origin(raw: str) -> str:
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise XiaoAiMcpConfigurationError(
            "XIAOAI_MCP_PUBLIC_BASE_URL must be a valid HTTPS origin"
        ) from None
    expected_netloc = parsed.hostname or ""
    if port is not None:
        expected_netloc += f":{port}"
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.netloc != expected_netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise XiaoAiMcpConfigurationError(
            "XIAOAI_MCP_PUBLIC_BASE_URL must be an HTTPS origin without credentials or a path"
        )
    return f"https://{expected_netloc}"


def _bounded_int(
    environment: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environment.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise XiaoAiMcpConfigurationError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise XiaoAiMcpConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


@dataclass(frozen=True, slots=True)
class _PendingRebind:
    confirmation_id: str
    host: str
    agent: str
    stored_session_id: str
    expected_revision: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class _SendRecord:
    text: str
    attachment_ids: tuple[str, ...]
    result: Mapping[str, object]
    expires_at: float


class XiaoAiMcpGateway:
    """Six bounded MCP operations for one server-owned logical device."""

    def __init__(
        self,
        adapter: XiaoAiAdapter,
        *,
        device_id: str,
        session_id: str = "xiaoai-mcp",
        max_input_chars: int = 8_000,
        max_output_chars: int = 4_000,
        send_cache_seconds: int = 600,
        attachment_store: AttachmentStore | None = None,
        public_base_url: str = "",
    ) -> None:
        if _IDENTITY_RE.fullmatch(device_id) is None:
            raise XiaoAiMcpConfigurationError("device_id is invalid")
        if _IDENTITY_RE.fullmatch(session_id) is None:
            raise XiaoAiMcpConfigurationError("session_id is invalid")
        self._adapter = adapter
        self._device_id = device_id
        self._session_id = session_id
        self._max_input_chars = max_input_chars
        self._max_output_chars = max_output_chars
        self._send_cache_seconds = send_cache_seconds
        self._attachment_store = attachment_store
        self._public_base_url = public_base_url.rstrip("/")
        self._pending_rebind: _PendingRebind | None = None
        self._send_cache: OrderedDict[str, _SendRecord] = OrderedDict()
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._adapter.close()

    async def list_agents(self) -> Mapping[str, object]:
        try:
            response = await self._adapter.list_agents(self._session_id)
        except Exception as exc:
            return _unavailable(exc)
        if response.get("status") != "ok":
            return _project_failure(response)
        rows = []
        for value in response.get("agents", []):
            if not isinstance(value, Mapping):
                continue
            host = value.get("host")
            agent = value.get("id")
            name = value.get("name")
            if host not in _ALLOWED_HOSTS or type(agent) is not str:
                continue
            rows.append(
                {
                    "host": host,
                    "agent": agent,
                    "name": name if type(name) is str else agent,
                    "available": value.get("available") is not False,
                }
            )
        return {"status": "ok", "agents": rows}

    async def list_sessions(
        self, *, host: str, agent: str, limit: int = 10
    ) -> Mapping[str, object]:
        if host not in _ALLOWED_HOSTS:
            return {"status": "rejected", "message": "不支持这个宿主。"}
        if not agent or len(agent) > 64 or not 1 <= limit <= 20:
            return {"status": "rejected", "message": "智能体或数量参数无效。"}
        try:
            response = await self._adapter.list_sessions(
                self._session_id, host=host, profile=agent, limit=limit
            )
        except Exception as exc:
            return _unavailable(exc)
        if response.get("status") != "ok":
            return _project_failure(response)
        sessions = []
        for value in response.get("sessions", []):
            if not isinstance(value, Mapping) or type(value.get("id")) is not str:
                continue
            projected: dict[str, object] = {
                "session_id": value["id"],
                "title": value.get("title", "") if type(value.get("title")) is str else "",
            }
            for key in (
                "updated_at",
                "started_at",
                "message_count",
                "running",
                "blank",
                "source",
            ):
                item = value.get(key)
                if item is None or type(item) in {str, int, float, bool}:
                    projected[key] = item
            sessions.append(projected)
        return {"status": "ok", "host": host, "agent": agent, "sessions": sessions}

    async def show_binding(self) -> Mapping[str, object]:
        try:
            response = await self._adapter.show_binding(
                self._session_id, device_id=self._device_id
            )
        except Exception as exc:
            return _unavailable(exc)
        if response.get("status") != "ok":
            return _project_failure(response)
        return _project_binding(response)

    async def bind_session(
        self,
        *,
        host: str,
        agent: str,
        session_id: str,
        confirmation_id: str | None = None,
    ) -> Mapping[str, object]:
        if host not in _ALLOWED_HOSTS:
            return {"status": "rejected", "message": "不支持这个宿主。"}
        if not agent or len(agent) > 64 or not session_id or len(session_id) > 512:
            return {"status": "rejected", "message": "绑定目标无效。"}
        try:
            current = await self._adapter.show_binding(
                self._session_id, device_id=self._device_id
            )
        except Exception as exc:
            return _unavailable(exc)
        if current.get("status") != "ok":
            return _project_failure(current)
        current_revision = current.get("binding_revision", 0)
        if type(current_revision) is not int:
            return {"status": "failed", "message": "当前绑定状态不可用。"}
        same_target = (
            current.get("bound") is True
            and current.get("host") == host
            and current.get("profile") == agent
            and current.get("stored_session_id") == session_id
        )
        if same_target:
            return _project_binding(current)

        if current.get("bound") is True:
            pending = await self._prepare_or_consume_rebind(
                host=host,
                agent=agent,
                stored_session_id=session_id,
                expected_revision=current_revision,
                confirmation_id=confirmation_id,
            )
            if isinstance(pending, Mapping):
                return pending
        elif confirmation_id is not None:
            return {"status": "rejected", "message": "当前没有需要确认的改绑操作。"}

        try:
            response = await self._adapter.bind_session(
                self._session_id,
                device_id=self._device_id,
                host=host,
                profile=agent,
                stored_session_id=session_id,
                expected_binding_revision=current_revision,
            )
        except Exception as exc:
            return _unavailable(exc)
        if response.get("status") != "ok":
            return _project_failure(response)
        return _project_binding(response)

    async def _prepare_or_consume_rebind(
        self,
        *,
        host: str,
        agent: str,
        stored_session_id: str,
        expected_revision: int,
        confirmation_id: str | None,
    ) -> Mapping[str, object] | None:
        now = time.monotonic()
        async with self._state_lock:
            pending = self._pending_rebind
            if pending is not None and pending.expires_at <= now:
                self._pending_rebind = None
                pending = None
            target = (host, agent, stored_session_id, expected_revision)
            if confirmation_id is None:
                if pending is None or (
                    pending.host,
                    pending.agent,
                    pending.stored_session_id,
                    pending.expected_revision,
                ) != target:
                    pending = _PendingRebind(
                        confirmation_id=uuid.uuid4().hex,
                        host=host,
                        agent=agent,
                        stored_session_id=stored_session_id,
                        expected_revision=expected_revision,
                        expires_at=now + 180.0,
                    )
                    self._pending_rebind = pending
                return {
                    "status": "confirmation_required",
                    "message": "当前已经绑定其他会话。请向用户说明新目标，得到明确确认后再次调用本工具并原样传回 confirmation_id。",
                    "confirmation_id": pending.confirmation_id,
                    "expires_in_seconds": max(1, int(pending.expires_at - now)),
                    "target": {
                        "host": host,
                        "agent": agent,
                        "session_id": stored_session_id,
                    },
                }
            if (
                pending is None
                or not hmac.compare_digest(pending.confirmation_id, confirmation_id)
                or (
                    pending.host,
                    pending.agent,
                    pending.stored_session_id,
                    pending.expected_revision,
                )
                != target
            ):
                return {"status": "rejected", "message": "改绑确认已失效或与目标不匹配。"}
            self._pending_rebind = None
            return None

    async def prepare_attachment_upload(
        self, *, request_id: str
    ) -> Mapping[str, object]:
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            return {"status": "rejected", "message": "request_id 格式无效。"}
        if self._attachment_store is None or not self._public_base_url:
            return {"status": "failed", "message": "附件上传入口尚未配置。"}
        try:
            response = await self._adapter.show_binding(
                self._session_id, device_id=self._device_id
            )
        except Exception as exc:
            return _unavailable(exc)
        if response.get("status") != "ok":
            return _project_failure(response)
        if response.get("bound") is not True:
            return {
                "status": "rejected",
                "message": "当前尚未绑定电脑端会话，请先绑定会话。",
            }
        try:
            binding = self._attachment_binding(response)
            grant = await self._attachment_store.prepare(
                request_id=request_id, binding=binding
            )
        except AttachmentError as exc:
            return {"status": "rejected", "message": str(exc)}
        return {
            "status": "ok",
            "attachment_id": grant.attachment_id,
            "upload_url": (
                f"{self._public_base_url}/upload?token={quote(grant.token, safe='')}"
            ),
            "expires_at": int(grant.expires_at),
            "expires_in_seconds": max(1, int(grant.expires_at - time.time())),
            "max_bytes": self._attachment_store.max_attachment_bytes,
            "message": (
                "请让用户在十分钟内打开链接并选择一个文件。上传完成后，只有再次调用 "
                "send_message 且原样传入 attachment_id 才会发送到当前会话。"
            ),
        }

    def _attachment_binding(self, response: Mapping[str, object]) -> AttachmentBinding:
        host = response.get("host")
        profile = response.get("profile")
        stored_session_id = response.get("stored_session_id")
        revision = response.get("binding_revision")
        if (
            host not in _ALLOWED_HOSTS
            or type(profile) is not str
            or type(stored_session_id) is not str
            or type(revision) is not int
            or revision < 1
        ):
            raise AttachmentError("当前绑定状态无效。", status_code=409)
        return AttachmentBinding(
            device_id=self._device_id,
            host=cast(str, host),
            profile=cast(str, profile),
            stored_session_id=cast(str, stored_session_id),
            binding_revision=cast(int, revision),
        )

    async def send_message(
        self,
        *,
        text: str,
        request_id: str,
        attachment_ids: Sequence[str] = (),
    ) -> Mapping[str, object]:
        clean = text.strip()
        if not clean:
            return {"status": "rejected", "message": "消息不能为空。"}
        if len(clean) > self._max_input_chars:
            return {"status": "rejected", "message": "消息过长，请缩短后重试。"}
        if _REQUEST_ID_RE.fullmatch(request_id) is None:
            return {"status": "rejected", "message": "request_id 格式无效。"}
        if (
            isinstance(attachment_ids, (str, bytes))
            or not all(type(item) is str for item in attachment_ids)
        ):
            return {"status": "rejected", "message": "attachment_ids 格式无效。"}
        selected_ids = tuple(attachment_ids)
        if selected_ids and self._attachment_store is None:
            return {"status": "rejected", "message": "附件上传功能尚未启用。"}
        async with self._send_lock:
            now = time.monotonic()
            self._prune_send_cache(now)
            cached = self._send_cache.get(request_id)
            if cached is not None:
                if cached.text != clean or cached.attachment_ids != selected_ids:
                    return {
                        "status": "rejected",
                        "message": "同一 request_id 不能用于不同消息或附件。",
                    }
                replayed = dict(cached.result)
                replayed["replayed"] = True
                return replayed
            try:
                binding = await self._adapter.show_binding(
                    self._session_id, device_id=self._device_id
                )
                if binding.get("status") != "ok":
                    return _project_failure(binding)
                if binding.get("bound") is not True:
                    return {
                        "status": "rejected",
                        "message": "当前尚未绑定电脑端会话，请先查看并绑定已有会话。",
                    }
                attachment_records = ()
                if selected_ids:
                    assert self._attachment_store is not None
                    attachment_binding = self._attachment_binding(binding)
                    attachment_records = await self._attachment_store.resolve(
                        selected_ids,
                        binding=attachment_binding,
                        request_id=request_id,
                    )
                    if attachment_binding.host == "hermes" and any(
                        item.kind == "image" and item.bytes > MAX_HERMES_IMAGE_BYTES
                        for item in attachment_records
                    ):
                        return {
                            "status": "rejected",
                            "message": "Hermes 单张图片最大支持 25 MiB；PDF 和通用文件仍可到 50 MiB。",
                        }
                if attachment_records:
                    response = await self._adapter.dispatch_bound(
                        self._session_id,
                        device_id=self._device_id,
                        prompt=clean,
                        attachments=tuple(
                            item.to_bridge_mapping() for item in attachment_records
                        ),
                    )
                else:
                    response = await self._adapter.dispatch_bound(
                        self._session_id,
                        device_id=self._device_id,
                        prompt=clean,
                    )
                result = self._project_send(response)
                if result.get("status") == "completed" and selected_ids:
                    assert self._attachment_store is not None
                    await self._attachment_store.remove(selected_ids)
            except AttachmentError as exc:
                result = {"status": "rejected", "message": str(exc)}
            except asyncio.CancelledError as exc:
                # Cancellation can arrive after the canonical host accepted the
                # write.  Preserve an uncertain result before propagating the
                # cancellation so a client retry cannot duplicate the turn.
                self._cache_send_result(
                    request_id,
                    clean,
                    selected_ids,
                    _unavailable(exc, outcome_unknown=True),
                )
                raise
            except Exception as exc:
                # The adapter marks uncertain writes and refuses automatic replay.
                # Cache the public failure under this request ID for the same reason.
                result = _unavailable(exc, outcome_unknown=True)
            self._cache_send_result(request_id, clean, selected_ids, result)
            return result

    def _cache_send_result(
        self,
        request_id: str,
        text: str,
        attachment_ids: tuple[str, ...],
        result: Mapping[str, object],
    ) -> None:
        completed_at = time.monotonic()
        self._prune_send_cache(completed_at)
        self._send_cache[request_id] = _SendRecord(
            text=text,
            attachment_ids=attachment_ids,
            result=dict(result),
            expires_at=completed_at + self._send_cache_seconds,
        )
        self._send_cache.move_to_end(request_id)
        while len(self._send_cache) > 256:
            self._send_cache.popitem(last=False)

    def _prune_send_cache(self, now: float) -> None:
        expired = [
            request_id
            for request_id, value in self._send_cache.items()
            if value.expires_at <= now
        ]
        for request_id in expired:
            self._send_cache.pop(request_id, None)

    def _project_send(self, response: Mapping[str, object]) -> Mapping[str, object]:
        status = response.get("status")
        if status != "completed":
            return _project_failure(response)
        output = response.get("output")
        if type(output) is not str:
            return {"status": "failed", "message": "智能体没有返回可展示的文字。"}
        answer = output
        truncated = len(answer) > self._max_output_chars
        if truncated:
            answer = answer[: self._max_output_chars].rstrip() + "\n……（完整回答请在电脑端同一会话查看）"
        return {
            "status": "completed",
            "host": response.get("host", ""),
            "session_id": response.get("stored_session_id", ""),
            "answer": answer,
            "truncated": truncated,
        }


def _project_binding(response: Mapping[str, object]) -> Mapping[str, object]:
    if response.get("bound") is not True:
        return {"status": "ok", "bound": False, "binding_revision": 0}
    return {
        "status": "ok",
        "bound": True,
        "host": response.get("host", ""),
        "agent": response.get("profile", ""),
        "session_id": response.get("stored_session_id", ""),
        "title": response.get("title", ""),
        "binding_revision": response.get("binding_revision", 0),
    }


def _project_failure(response: Mapping[str, object]) -> Mapping[str, object]:
    status = response.get("status")
    public_status = status if type(status) is str else "failed"
    messages = {
        "rejected": "请求被安全策略拒绝，请检查绑定或参数。",
        "outcome_unknown": "结果状态未知；为避免重复写入，请不要自动重试。",
        "conflict": "绑定在操作期间发生变化，请重新查看当前绑定。",
        "partial_failure": "宿主可能已完成操作，但本地状态需要人工核对。",
        "reconciliation_required": "当前会话需要在电脑端核对后才能继续。",
        "pending": "操作仍在宿主处理中，请不要重复发送。",
        "failed": "电脑端智能体操作失败。",
    }
    return {
        "status": public_status,
        "message": messages.get(public_status, "电脑端通道暂时不可用。"),
    }


def _unavailable(
    exc: Exception, *, outcome_unknown: bool = False
) -> Mapping[str, object]:
    status = "outcome_unknown" if outcome_unknown else "failed"
    return {
        "status": status,
        "error_code": type(exc).__name__,
        "message": (
            "调用结果未知；为避免重复写入，请不要自动重试。"
            if outcome_unknown
            else "电脑端通道暂时不可用。"
        ),
    }


def create_xiaoai_mcp_server(gateway: XiaoAiMcpGateway, settings: XiaoAiMcpSettings) -> FastMCP:
    server = FastMCP(
        name="Lingxi XiaoAi Gateway",
        instructions=(
            "把小爱接入电脑端已有 Hermes 或 DeepSeek Harness 会话。"
            "先查看当前绑定；不要自动选择会话。首次绑定可直接执行，改绑必须向用户复述目标并取得明确确认。"
            "普通消息调用 send_message；不要把管理意图伪装成普通消息。"
            "用户要发送文件时，先调用 prepare_attachment_upload，把一次性链接展示给用户；"
            "用户确认网页上传完成后，再把返回的 attachment_id 原样传给 send_message。"
        ),
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        # This gateway has one server-owned logical device and no per-transport
        # state. Stateless transport prevents abandoned public clients from
        # accumulating server-side MCP sessions.
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*"],
            allowed_origins=[],
        ),
    )

    @server.tool(
        name="list_agents",
        description="列出允许由小爱使用的 Hermes Profile 和 DeepSeek Harness 智能体及可用状态。",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def list_agents() -> Mapping[str, object]:
        return await gateway.list_agents()

    @server.tool(
        name="list_sessions",
        description="按宿主和智能体列出现有会话，只返回标题和最小元数据，不返回历史正文。",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def list_sessions(host: str, agent: str, limit: int = 10) -> Mapping[str, object]:
        return await gateway.list_sessions(host=host, agent=agent, limit=limit)

    @server.tool(
        name="show_binding",
        description="查看这个小爱入口当前绑定的电脑端宿主、智能体和会话。",
        annotations=_READ_ONLY,
        structured_output=True,
    )
    async def show_binding() -> Mapping[str, object]:
        return await gateway.show_binding()

    @server.tool(
        name="bind_session",
        description=(
            "绑定一个已经存在且刚由 list_sessions 列出的会话。首次绑定可直接执行；"
            "如果已经绑定其他会话，第一次调用只返回 confirmation_id，必须向用户复述新目标并得到明确确认后，"
            "再用相同目标和 confirmation_id 调用。不要静默改绑。"
        ),
        annotations=_LOCAL_WRITE,
        structured_output=True,
    )
    async def bind_session(
        host: str,
        agent: str,
        session_id: str,
        confirmation_id: str | None = None,
    ) -> Mapping[str, object]:
        return await gateway.bind_session(
            host=host,
            agent=agent,
            session_id=session_id,
            confirmation_id=confirmation_id,
        )

    @server.tool(
        name="prepare_attachment_upload",
        description=(
            "为当前绑定会话生成一个10分钟有效、只能使用一次的手机附件上传链接。"
            "本工具不接收文件字节、不发送消息、不改绑会话。request_id 必须稳定；"
            "上传完成后，后续 send_message 必须原样传入本工具返回的 attachment_id。"
        ),
        annotations=_LOCAL_WRITE,
        structured_output=True,
    )
    async def prepare_attachment_upload(request_id: str) -> Mapping[str, object]:
        return await gateway.prepare_attachment_upload(request_id=request_id)

    @server.tool(
        name="send_message",
        description=(
            "把用户的普通消息发送到当前绑定的 canonical 会话并返回该回合回答。"
            "request_id 必须为本次用户请求生成的稳定唯一标识；网络重试必须复用同一个 request_id，"
            "不得因超时自动换宿主、换会话或生成新的 request_id 重发。attachment_ids 只能使用 "
            "prepare_attachment_upload 返回且已在网页上传完成的 ID；不得传 Base64、路径或远程 URL。"
        ),
        annotations=_AGENT_WRITE,
        structured_output=True,
    )
    async def send_message(
        text: str,
        request_id: str,
        attachment_ids: list[str] | None = None,
    ) -> Mapping[str, object]:
        return await gateway.send_message(
            text=text,
            request_id=request_id,
            attachment_ids=attachment_ids or (),
        )

    return server


class XiaoAiMcpSecurityMiddleware:
    """Bearer auth, request-size bounds, and coarse abuse throttling."""

    def __init__(
        self,
        app: Any,
        *,
        token: str,
        attachment_store: AttachmentStore | None = None,
        max_body_bytes: int,
        requests_per_minute: int,
        max_concurrent_requests: int = 8,
        body_read_timeout_seconds: int = 10,
    ) -> None:
        self._app = app
        self._token = token
        self._attachment_store = attachment_store
        self._max_body_bytes = max_body_bytes
        self._requests_per_minute = requests_per_minute
        self._body_read_timeout_seconds = body_read_timeout_seconds
        self._requests: deque[float] = deque()
        self._rate_lock = asyncio.Lock()
        self._request_slots = asyncio.Semaphore(max_concurrent_requests)
        self._upload_slots = asyncio.Semaphore(2)

    async def __call__(self, scope: MutableMapping[str, object], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        path = scope.get("path")
        if path == "/upload":
            await self._serve_upload_page(scope, send)
            return
        if path == "/attachments":
            await self._receive_attachment(scope, receive, send)
            return
        if path != "/mcp":
            await _send_json(send, 404, {"error": "not found"})
            return
        raw_headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
        authorization_headers = [
            value.decode("latin-1").strip()
            for key, value in raw_headers
            if key.decode("latin-1").lower() == "authorization"
        ]
        accepted = False
        if len(authorization_headers) == 1:
            parts = authorization_headers[0].split(" ")
            accepted = (
                len(parts) == 2
                and parts[0].lower() == "bearer"
                and bool(parts[1])
                and hmac.compare_digest(parts[1], self._token)
            )
        if not accepted:
            await _send_json(
                send,
                401,
                {"error": "unauthorized"},
                extra_headers=[(b"www-authenticate", b"Bearer")],
            )
            return
        now = time.monotonic()
        async with self._rate_lock:
            while self._requests and self._requests[0] <= now - 60.0:
                self._requests.popleft()
            if len(self._requests) >= self._requests_per_minute:
                await _send_json(send, 429, {"error": "rate limit exceeded"})
                return
            self._requests.append(now)
        try:
            await asyncio.wait_for(self._request_slots.acquire(), timeout=1.0)
        except asyncio.TimeoutError:
            await _send_json(
                send,
                503,
                {"error": "server busy"},
                extra_headers=[(b"retry-after", b"1")],
            )
            return
        try:
            if scope.get("method") == "POST":
                upstream_receive = receive
                body = bytearray()
                deadline = time.monotonic() + self._body_read_timeout_seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        await _send_json(send, 408, {"error": "request body timeout"})
                        return
                    try:
                        message = await asyncio.wait_for(
                            upstream_receive(), timeout=remaining
                        )
                    except asyncio.TimeoutError:
                        await _send_json(send, 408, {"error": "request body timeout"})
                        return
                    message_type = message.get("type")
                    if message_type == "http.disconnect":
                        return
                    if message_type != "http.request":
                        await _send_json(send, 400, {"error": "invalid request stream"})
                        return
                    chunk = message.get("body", b"")
                    if isinstance(chunk, bytes):
                        body.extend(chunk)
                    else:
                        await _send_json(send, 400, {"error": "invalid request body"})
                        return
                    if len(body) > self._max_body_bytes:
                        await _send_json(send, 413, {"error": "request body too large"})
                        return
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def replay_receive() -> Mapping[str, object]:
                    nonlocal delivered
                    if delivered:
                        return await upstream_receive()
                    delivered = True
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }

                receive = replay_receive
            await self._app(scope, receive, send)
        finally:
            self._request_slots.release()

    async def _serve_upload_page(
        self, scope: Mapping[str, object], send: Any
    ) -> None:
        if scope.get("method") != "GET" or self._attachment_store is None:
            await _send_json(send, 404, {"error": "not found"})
            return
        try:
            token = _query_token(scope)
            grant = await self._attachment_store.inspect_grant(token)
        except AttachmentError as exc:
            await _send_json(send, exc.status_code, {"error": str(exc)})
            return
        await _send_html(
            send,
            _upload_html(
                attachment_id=grant.attachment_id,
                max_bytes=self._attachment_store.max_attachment_bytes,
            ),
        )

    async def _receive_attachment(
        self,
        scope: Mapping[str, object],
        receive: Any,
        send: Any,
    ) -> None:
        store = self._attachment_store
        if scope.get("method") != "POST" or store is None:
            await _send_json(send, 404, {"error": "not found"})
            return
        headers = _header_map(scope)
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/octet-stream":
            await _send_json(send, 415, {"error": "content type must be application/octet-stream"})
            return
        token = headers.get("x-upload-token", "")
        encoded_name = headers.get("x-file-name", "")
        media_type = headers.get("x-file-type", "application/octet-stream")
        if len(encoded_name) > 768 or len(media_type) > 128:
            await _send_json(send, 400, {"error": "attachment metadata is too long"})
            return
        try:
            name = unquote(encoded_name, encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            await _send_json(send, 400, {"error": "attachment name is invalid"})
            return
        raw_length = headers.get("content-length")
        content_length: int | None = None
        if raw_length:
            try:
                content_length = int(raw_length)
            except ValueError:
                await _send_json(send, 400, {"error": "content length is invalid"})
                return
            if content_length < 0:
                await _send_json(send, 400, {"error": "content length is invalid"})
                return
        try:
            await asyncio.wait_for(self._upload_slots.acquire(), timeout=1.0)
        except asyncio.TimeoutError:
            await _send_json(
                send,
                503,
                {"error": "upload service is busy"},
                extra_headers=[(b"retry-after", b"2")],
            )
            return
        try:
            record = await store.upload(
                token=token,
                name=name,
                declared_media_type=media_type,
                receive=receive,
                content_length=content_length,
            )
        except AttachmentError as exc:
            if exc.status_code != 499:
                await _send_json(send, exc.status_code, {"error": str(exc)})
            return
        finally:
            self._upload_slots.release()
        await _send_json(
            send,
            201,
            {
                "status": "uploaded",
                "attachment_id": record.attachment_id,
                "name": record.name,
                "bytes": record.bytes,
                "media_type": record.media_type,
                "message": "上传完成。请返回小爱并继续说明要发送的问题。",
            },
        )


async def _send_json(
    send: Any,
    status: int,
    body: Mapping[str, object],
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"cache-control", b"no-store"),
        (b"content-length", str(len(encoded)).encode("ascii")),
    ]
    headers.extend(extra_headers or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": encoded})


def _header_map(scope: Mapping[str, object]) -> dict[str, str]:
    raw_headers = cast(list[tuple[bytes, bytes]], scope.get("headers", []))
    result: dict[str, str] = {}
    for raw_name, raw_value in raw_headers:
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1").strip()
        result[name] = value if name not in result else "\x00"
    return result


def _query_token(scope: Mapping[str, object]) -> str:
    raw = scope.get("query_string", b"")
    if not isinstance(raw, bytes) or len(raw) > 1024:
        raise AttachmentError("上传链接无效。", status_code=401)
    try:
        values = parse_qs(
            raw.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=4,
        )
    except (UnicodeDecodeError, ValueError):
        raise AttachmentError("上传链接无效。", status_code=401) from None
    tokens = values.get("token", [])
    if set(values) != {"token"} or len(tokens) != 1:
        raise AttachmentError("上传链接无效。", status_code=401)
    return tokens[0]


async def _send_html(send: Any, html: str) -> None:
    encoded = html.encode("utf-8")
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(encoded)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (
            b"content-security-policy",
            b"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; form-action 'none'; frame-ancestors 'none'",
        ),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    await send({"type": "http.response.body", "body": encoded})


def _upload_html(*, attachment_id: str, max_bytes: int) -> str:
    max_mib = max_bytes // (1024 * 1024)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>灵溪附件上传</title><style>
body{{margin:0;background:#0b1220;color:#e9f5f1;font-family:system-ui,sans-serif;display:grid;min-height:100vh;place-items:center}}
main{{width:min(88vw,520px);background:#121d2d;border:1px solid #29404c;border-radius:22px;padding:28px;box-shadow:0 18px 60px #0007}}
h1{{margin:0 0 10px;color:#85f1c5}}p{{line-height:1.6;color:#b9c9d2}}input{{width:100%;margin:18px 0;box-sizing:border-box}}
button{{width:100%;border:0;border-radius:14px;padding:14px;background:#62dba8;color:#07130e;font-weight:700;font-size:16px}}
button:disabled{{opacity:.45}}#status{{min-height:48px;color:#8debd0}}code{{word-break:break-all;color:#a8c8ff}}
</style></head><body><main><h1>上传给当前智能体</h1>
<p>单文件最多 {max_mib} MiB。支持 JPEG、PNG、WebP、PDF、TXT、Markdown、CSV、JSON。链接十分钟内且只能使用一次。</p>
<input id="file" type="file" accept=".jpg,.jpeg,.png,.webp,.pdf,.txt,.md,.csv,.json">
<button id="upload">开始上传</button><p id="status">附件编号：<code>{attachment_id}</code></p>
<script>
const q=new URLSearchParams(location.search),token=q.get('token')||'';history.replaceState(null,'','/upload');
const f=document.querySelector('#file'),b=document.querySelector('#upload'),s=document.querySelector('#status'),limit={max_bytes};
b.onclick=async()=>{{const file=f.files[0];if(!file){{s.textContent='请先选择文件。';return}}if(file.size>limit){{s.textContent='文件超过 {max_mib} MiB。';return}}
b.disabled=true;s.textContent='正在上传……';try{{const r=await fetch('/attachments',{{method:'POST',headers:{{'content-type':'application/octet-stream','x-upload-token':token,'x-file-name':encodeURIComponent(file.name),'x-file-type':file.type||'application/octet-stream'}},body:file}});const data=await r.json();if(!r.ok)throw new Error(data.error||'上传失败');s.textContent='上传完成，请返回小爱并继续说明问题。附件编号：'+data.attachment_id}}catch(e){{s.textContent=e.message||'上传失败';b.disabled=false}}}};
</script></main></body></html>"""


async def serve_xiaoai_mcp(
    settings: XiaoAiMcpSettings,
    *,
    adapter: XiaoAiAdapter | None = None,
) -> None:
    import uvicorn

    selected_adapter = adapter or adapter_from_environment()
    attachment_store = AttachmentStore(
        settings.attachment_root,
        max_attachment_bytes=settings.max_attachment_bytes,
        upload_ttl_seconds=settings.upload_ttl_seconds,
        attachment_ttl_seconds=settings.attachment_ttl_seconds,
    )
    gateway = XiaoAiMcpGateway(
        selected_adapter,
        device_id=settings.device_id,
        session_id=settings.session_id,
        max_input_chars=settings.max_input_chars,
        max_output_chars=settings.max_output_chars,
        send_cache_seconds=settings.send_cache_seconds,
        attachment_store=attachment_store,
        public_base_url=settings.public_base_url,
    )
    server = create_xiaoai_mcp_server(gateway, settings)
    app = XiaoAiMcpSecurityMiddleware(
        server.streamable_http_app(),
        token=settings.token,
        attachment_store=attachment_store,
        max_body_bytes=settings.max_body_bytes,
        requests_per_minute=settings.requests_per_minute,
        max_concurrent_requests=settings.max_concurrent_requests,
        body_read_timeout_seconds=settings.body_read_timeout_seconds,
    )
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_level="warning",
            access_log=False,
        )
    )
    cleanup_task = asyncio.create_task(_attachment_cleanup_loop(attachment_store))
    try:
        await uvicorn_server.serve()
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        await gateway.close()


async def _attachment_cleanup_loop(store: AttachmentStore) -> None:
    while True:
        await asyncio.sleep(60)
        await store.cleanup_expired()


__all__ = [
    "XiaoAiMcpConfigurationError",
    "XiaoAiMcpGateway",
    "XiaoAiMcpSecurityMiddleware",
    "XiaoAiMcpSettings",
    "create_xiaoai_mcp_server",
    "serve_xiaoai_mcp",
]
