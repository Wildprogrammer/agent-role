"""Native Xiaozhi server adapter for the private stdio bridge."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol, cast


_CALLER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_LINE_BYTES = 1_048_576
_MAX_PENDING_SESSIONS = 1_024
_PENDING_TTL_SECONDS = 180.0


class XiaozhiAdapterError(RuntimeError):
    """Raised when the Xiaozhi adapter cannot safely complete an operation."""


class BridgeClient(Protocol):
    async def request(
        self,
        operation: str,
        *,
        session_id: str,
        profile: str | None = None,
        input: str | None = None,
        confirmed: bool | None = None,
        device_id: str | None = None,
        stored_session_id: str | None = None,
        limit: int | None = None,
        idempotency_key: str | None = None,
        host: str | None = None,
        binding_revision: int | None = None,
        attachments: Sequence[Mapping[str, object]] | None = None,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class BridgeProcessSettings:
    command: tuple[str, ...]
    caller_id: str
    response_timeout_seconds: float = 750.0
    max_line_bytes: int = _MAX_LINE_BYTES

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> BridgeProcessSettings:
        env = os.environ if environment is None else environment
        python_path = _absolute_file(env.get("XIAOZHI_HERMES_BRIDGE_PYTHON", ""), "bridge Python")
        script_path = _absolute_file(env.get("XIAOZHI_HERMES_BRIDGE_SCRIPT", ""), "bridge script")
        config_path = _absolute_file(env.get("XIAOZHI_HERMES_BRIDGE_CONFIG", ""), "bridge config")
        digest = env.get("XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256", "").strip()
        if _SHA256_RE.fullmatch(digest) is None:
            raise XiaozhiAdapterError(
                "XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256 must be a 64-character SHA-256"
            )
        caller_id = env.get("XIAOZHI_HERMES_CALLER_ID", "").strip()
        if _CALLER_RE.fullmatch(caller_id) is None:
            raise XiaozhiAdapterError(
                "XIAOZHI_HERMES_CALLER_ID is missing or contains unsupported characters"
            )
        return cls(
            command=(
                str(python_path),
                str(script_path),
                "serve-stdio",
                "--config",
                str(config_path),
                "--confirmed-config-sha256",
                digest.lower(),
            ),
            caller_id=caller_id,
        )


def _absolute_file(raw: str, label: str) -> Path:
    value = raw.strip()
    if not value:
        raise XiaozhiAdapterError(f"{label} path is required")
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise XiaozhiAdapterError(f"{label} must be an existing absolute file")
    return path.resolve()


class BridgeProcessClient:
    """Serialize JSON-line requests through one private child process."""

    def __init__(
        self,
        settings: BridgeProcessSettings,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = settings
        self._environment = dict(os.environ if environment is None else environment)
        self._process: asyncio.subprocess.Process | None = None
        self._io_lock = asyncio.Lock()
        self._started_once = False

    async def _start_locked(self, *, operation: str) -> asyncio.subprocess.Process:
        process = self._process
        if process is not None and process.returncode is None:
            return process
        crashed = self._started_once
        self._process = None
        if crashed and operation in {
            "dispatch",
            "dispatch_shared",
            "dispatch_bound_voice",
            "create_session",
            "compress_session",
        }:
            raise XiaozhiAdapterError(
                "bridge process restarted; list or switch the profile before dispatching again"
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *self._settings.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=self._environment,
            )
        except OSError as exc:
            raise XiaozhiAdapterError(
                f"cannot start bridge process: {type(exc).__name__}"
            ) from None
        self._process = process
        self._started_once = True
        return process

    async def request(
        self,
        operation: str,
        *,
        session_id: str,
        profile: str | None = None,
        input: str | None = None,
        confirmed: bool | None = None,
        device_id: str | None = None,
        stored_session_id: str | None = None,
        limit: int | None = None,
        idempotency_key: str | None = None,
        host: str | None = None,
        binding_revision: int | None = None,
        attachments: Sequence[Mapping[str, object]] | None = None,
    ) -> Mapping[str, object]:
        request: dict[str, object] = {
            "schema_version": "1.0",
            "caller_id": self._settings.caller_id,
            "session_id": session_id,
            "operation": operation,
        }
        if profile is not None:
            request["profile"] = profile
        if input is not None:
            request["input"] = input
        if confirmed is not None:
            request["confirmed"] = confirmed
        if device_id is not None:
            request["device_id"] = device_id
        if stored_session_id is not None:
            request["stored_session_id"] = stored_session_id
        if limit is not None:
            request["limit"] = limit
        if idempotency_key is not None:
            request["idempotency_key"] = idempotency_key
        if host is not None:
            request["host"] = host
        if binding_revision is not None:
            request["binding_revision"] = binding_revision
        if attachments:
            request["attachments"] = [dict(item) for item in attachments]
        payload = json.dumps(
            request, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8") + b"\n"
        if len(payload) > self._settings.max_line_bytes:
            raise XiaozhiAdapterError("bridge request exceeds the line-size limit")

        async with self._io_lock:
            process = await self._start_locked(operation=operation)
            if process.stdin is None or process.stdout is None:
                raise XiaozhiAdapterError("bridge process has no private stdio channel")
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
                line = await asyncio.wait_for(
                    process.stdout.readline(),
                    timeout=self._settings.response_timeout_seconds,
                )
            except (BrokenPipeError, ConnectionError, asyncio.TimeoutError) as exc:
                await self._stop_locked()
                raise XiaozhiAdapterError(
                    f"bridge response is unavailable: {type(exc).__name__}"
                ) from None
            if not line:
                await self._stop_locked()
                raise XiaozhiAdapterError("bridge process closed without a response")
            if len(line) > self._settings.max_line_bytes:
                await self._stop_locked()
                raise XiaozhiAdapterError("bridge response exceeds the line-size limit")
            try:
                decoded = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                await self._stop_locked()
                raise XiaozhiAdapterError("bridge returned invalid UTF-8 JSON") from None
            if not isinstance(decoded, Mapping) or not all(
                type(key) is str for key in decoded
            ):
                raise XiaozhiAdapterError("bridge response must be a JSON object")
            return cast(Mapping[str, object], decoded)

    async def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def close(self) -> None:
        async with self._io_lock:
            await self._stop_locked()


@dataclass(frozen=True, slots=True)
class PendingDispatch:
    host: str
    profile: str
    operation: str
    operation_id: str
    prepared_at: float
    prompt: str = ""
    device_id: str | None = None
    stored_session_id: str | None = None
    idempotency_key: str | None = None
    binding_revision: int | None = None
    state: str = "prepared"


class XiaozhiHermesAdapter:
    """Map Xiaozhi conversations to bridge sessions with two-step dispatch."""

    def __init__(
        self,
        client: BridgeClient,
        *,
        max_input_chars: int = 8_000,
        max_pending_sessions: int = _MAX_PENDING_SESSIONS,
    ) -> None:
        self._client = client
        self._max_input_chars = max_input_chars
        self._max_pending_sessions = max_pending_sessions
        self._pending: dict[str, PendingDispatch] = {}
        self._state_lock = asyncio.Lock()

    @staticmethod
    def _is_expired_prepared(pending: PendingDispatch, now: float) -> bool:
        return (
            pending.state == "prepared"
            and now - pending.prepared_at > _PENDING_TTL_SECONDS
        )

    def _drop_expired_prepared_locked(self, now: float) -> None:
        expired_sessions = [
            pending_session_id
            for pending_session_id, pending in self._pending.items()
            if self._is_expired_prepared(pending, now)
        ]
        for pending_session_id in expired_sessions:
            self._pending.pop(pending_session_id, None)

    def _pending_for_device_locked(
        self, device_id: str
    ) -> tuple[str, PendingDispatch] | None:
        for pending_session_id, pending in self._pending.items():
            if pending.device_id == device_id:
                return pending_session_id, pending
        return None

    def _drop_prepared_for_device_locked(self, device_id: str) -> None:
        prepared_sessions = [
            pending_session_id
            for pending_session_id, pending in self._pending.items()
            if pending.device_id == device_id and pending.state == "prepared"
        ]
        for pending_session_id in prepared_sessions:
            self._pending.pop(pending_session_id, None)

    async def list_profiles(self, session_id: str) -> Mapping[str, object]:
        return await self._client.request("list_profiles", session_id=session_id)

    async def list_agents(self, session_id: str) -> Mapping[str, object]:
        return await self._client.request("list_agents", session_id=session_id)

    async def switch_profile(
        self, session_id: str, profile: str
    ) -> Mapping[str, object]:
        response = await self._client.request(
            "switch_profile", session_id=session_id, profile=profile
        )
        if response.get("status") == "ok":
            async with self._state_lock:
                pending = self._pending.get(session_id)
                if pending is not None and pending.state == "prepared":
                    self._pending.pop(session_id, None)
        return response

    async def list_sessions(
        self,
        session_id: str,
        *,
        profile: str,
        limit: int = 10,
        host: str = "hermes",
    ) -> Mapping[str, object]:
        return await self._client.request(
            "list_sessions",
            session_id=session_id,
            profile=profile,
            limit=limit,
            host=host,
        )

    async def bind_session(
        self,
        session_id: str,
        *,
        device_id: str,
        host: str = "hermes",
        profile: str,
        stored_session_id: str,
        expected_binding_revision: int | None = None,
    ) -> Mapping[str, object]:
        response = await self._client.request(
            "bind_session",
            session_id=session_id,
            device_id=device_id,
            host=host,
            profile=profile,
            stored_session_id=stored_session_id,
            binding_revision=expected_binding_revision,
        )
        if response.get("status") == "ok":
            async with self._state_lock:
                self._drop_prepared_for_device_locked(device_id)
                pending = self._pending.get(session_id)
                if pending is not None and pending.state == "prepared":
                    self._pending.pop(session_id, None)
        return response

    async def show_binding(
        self, session_id: str, *, device_id: str
    ) -> Mapping[str, object]:
        return await self._client.request(
            "show_binding", session_id=session_id, device_id=device_id
        )

    async def unbind_session(
        self,
        session_id: str,
        *,
        device_id: str,
        expected_binding_revision: int | None = None,
    ) -> Mapping[str, object]:
        response = await self._client.request(
            "unbind_session",
            session_id=session_id,
            device_id=device_id,
            binding_revision=expected_binding_revision,
        )
        async with self._state_lock:
            self._drop_prepared_for_device_locked(device_id)
            pending = self._pending.get(session_id)
            if pending is not None and pending.state == "prepared":
                self._pending.pop(session_id, None)
        return response

    @staticmethod
    def _extract_forwarding_prompt(prompt: str) -> tuple[str | None, bool]:
        command = re.match(r"^\s*(?:交给|让|请)\s*", prompt, flags=re.IGNORECASE)
        if command is None:
            return None, False
        remainder = prompt[command.end() :]
        targets = (
            r"当前\s*hermes\s*profile(?=$|[^A-Za-z0-9_])",
            r"当前\s*智能体",
            r"当前\s*profile(?=$|[^A-Za-z0-9_])",
            r"当前\s*agent(?=$|[^A-Za-z0-9_])",
            r"hermes(?=$|[^A-Za-z0-9_])",
            r"当前(?=$|[\s：:,，;；])",
        )
        for target in targets:
            matched = re.match(target, remainder, flags=re.IGNORECASE)
            if matched is None:
                continue
            body = re.sub(
                r"^[\s：:,，;；]+", "", remainder[matched.end() :]
            ).strip()
            if re.search(r"[A-Za-z0-9\u3400-\u9fff]", body):
                return body, False
            return None, True
        compact_remainder = "".join(remainder.lower().split())
        return None, compact_remainder.startswith(("当前", "hermes"))

    @staticmethod
    def _clean_dispatch_prompt(prompt: str) -> str:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise XiaozhiAdapterError("prompt must not be blank")
        forwarded_prompt, incomplete = XiaozhiHermesAdapter._extract_forwarding_prompt(
            clean_prompt
        )
        if incomplete:
            raise XiaozhiAdapterError(
                "路由命令缺少正文，请在智能体名称后说出完整问题"
            )
        if forwarded_prompt is not None:
            clean_prompt = forwarded_prompt
        if not re.search(r"[A-Za-z0-9\u3400-\u9fff]", clean_prompt):
            raise XiaozhiAdapterError("prompt must contain message text")
        return clean_prompt

    async def prepare_shared_dispatch(
        self, session_id: str, *, device_id: str, prompt: str
    ) -> Mapping[str, object]:
        clean_prompt = self._clean_dispatch_prompt(prompt)
        if len(clean_prompt) > self._max_input_chars:
            raise XiaozhiAdapterError("prompt exceeds the adapter input limit")
        binding = await self.show_binding(session_id, device_id=device_id)
        if binding.get("status") != "ok":
            return binding
        if binding.get("bound") is not True:
            raise XiaozhiAdapterError("device is not bound to an agent conversation")
        host = binding.get("host", "hermes")
        profile = binding.get("profile")
        stored_session_id = binding.get("stored_session_id")
        binding_revision = binding.get("binding_revision")
        if (
            host not in {"hermes", "deepseek-harness"}
            or type(profile) is not str
            or type(stored_session_id) is not str
            or type(binding_revision) is not int
            or binding_revision < 1
        ):
            raise XiaozhiAdapterError("bridge returned an invalid agent binding")
        async with self._state_lock:
            self._drop_expired_prepared_locked(time.monotonic())
            device_pending = self._pending_for_device_locked(device_id)
            if device_pending is not None:
                _, existing = device_pending
                raise XiaozhiAdapterError(
                    f"a pending agent operation already exists for this device in state "
                    f"{existing.state}; cancel or reconcile it first"
                )
            existing = self._pending.get(session_id)
            if existing is not None:
                raise XiaozhiAdapterError(
                    f"a pending agent operation already exists in state {existing.state}; "
                    "cancel or reconcile it first"
                )
            if (
                session_id not in self._pending
                and len(self._pending) >= self._max_pending_sessions
            ):
                raise XiaozhiAdapterError("pending confirmation session limit reached")
            self._pending[session_id] = PendingDispatch(
                host=cast(str, host),
                profile=cast(str, profile),
                operation="dispatch_shared",
                operation_id=uuid.uuid4().hex,
                prepared_at=time.monotonic(),
                prompt=clean_prompt,
                device_id=device_id,
                stored_session_id=cast(str, stored_session_id),
                binding_revision=cast(int, binding_revision),
            )
        return {
            "schema_version": "1.0",
            "status": "confirmation_required",
            "operation": "prepare_shared_dispatch",
            "session_id": session_id,
            "device_id": device_id,
            "host": host,
            "active_profile": profile,
            "stored_session_id": stored_session_id,
            "binding_revision": binding_revision,
            "operation_id": self._pending[session_id].operation_id,
            "expires_in_seconds": int(_PENDING_TTL_SECONDS),
        }

    async def dispatch_bound(
        self,
        session_id: str,
        *,
        device_id: str,
        prompt: str,
        attachments: Sequence[Mapping[str, object]] = (),
    ) -> Mapping[str, object]:
        """Send ordinary speech to the current binding without faking confirmation."""

        clean_prompt = self._clean_dispatch_prompt(prompt)
        if len(clean_prompt) > self._max_input_chars:
            raise XiaozhiAdapterError("prompt exceeds the adapter input limit")
        binding = await self.show_binding(session_id, device_id=device_id)
        if binding.get("status") != "ok":
            return binding
        if binding.get("bound") is not True:
            raise XiaozhiAdapterError("device is not bound to an agent conversation")
        host = binding.get("host", "hermes")
        profile = binding.get("profile")
        stored_session_id = binding.get("stored_session_id")
        binding_revision = binding.get("binding_revision")
        if (
            host not in {"hermes", "deepseek-harness"}
            or type(profile) is not str
            or type(stored_session_id) is not str
            or type(binding_revision) is not int
            or binding_revision < 1
        ):
            raise XiaozhiAdapterError("bridge returned an invalid agent binding")

        submitting = PendingDispatch(
            host=cast(str, host),
            profile=cast(str, profile),
            operation="dispatch_bound_voice",
            operation_id=uuid.uuid4().hex,
            prepared_at=time.monotonic(),
            prompt=clean_prompt,
            device_id=device_id,
            stored_session_id=cast(str, stored_session_id),
            binding_revision=cast(int, binding_revision),
            state="submitting",
        )
        async with self._state_lock:
            self._drop_expired_prepared_locked(time.monotonic())
            device_pending = self._pending_for_device_locked(device_id)
            if device_pending is not None:
                _, existing = device_pending
                raise XiaozhiAdapterError(
                    "this device already has an Agent operation in state "
                    f"{existing.state}; finish or reconcile it first"
                )
            existing = self._pending.get(session_id)
            if existing is not None:
                raise XiaozhiAdapterError(
                    f"this voice session already has an Agent operation in state "
                    f"{existing.state}; finish or reconcile it first"
                )
            if len(self._pending) >= self._max_pending_sessions:
                raise XiaozhiAdapterError("pending Agent operation limit reached")
            self._pending[session_id] = submitting

        try:
            dispatch_args: dict[str, object] = {
                "device_id": device_id,
                "input": clean_prompt,
                "host": cast(str, host),
                "profile": cast(str, profile),
                "stored_session_id": cast(str, stored_session_id),
                "binding_revision": cast(int, binding_revision),
            }
            if attachments:
                dispatch_args["attachments"] = attachments
            response = await self._client.request(
                "dispatch_bound_voice",
                session_id=session_id,
                **dispatch_args,
            )
        except Exception:
            async with self._state_lock:
                if self._pending.get(session_id) == submitting:
                    self._pending[session_id] = replace(
                        submitting, state="outcome_unknown"
                    )
            raise

        response_status = response.get("status")
        uncertain = {
            "pending",
            "outcome_unknown",
            "conflict",
            "partial_failure",
            "reconciliation_required",
        }
        async with self._state_lock:
            if self._pending.get(session_id) == submitting:
                if response_status in uncertain:
                    self._pending[session_id] = replace(
                        submitting, state=cast(str, response_status)
                    )
                elif type(response_status) is str:
                    self._pending.pop(session_id, None)
                else:
                    self._pending[session_id] = replace(
                        submitting, state="outcome_unknown"
                    )
        return response

    async def _prepare_bound_lifecycle(
        self,
        session_id: str,
        *,
        device_id: str,
        operation: str,
    ) -> Mapping[str, object]:
        binding = await self.show_binding(session_id, device_id=device_id)
        if binding.get("status") != "ok":
            return binding
        if binding.get("bound") is not True:
            raise XiaozhiAdapterError("device is not bound to an agent conversation")
        host = binding.get("host", "hermes")
        profile = binding.get("profile")
        stored_session_id = binding.get("stored_session_id")
        binding_revision = binding.get("binding_revision")
        if (
            host not in {"hermes", "deepseek-harness"}
            or type(profile) is not str
            or type(stored_session_id) is not str
            or type(binding_revision) is not int
            or binding_revision < 1
        ):
            raise XiaozhiAdapterError("bridge returned an invalid agent binding")
        async with self._state_lock:
            self._drop_expired_prepared_locked(time.monotonic())
            device_pending = self._pending_for_device_locked(device_id)
            if device_pending is not None:
                _, existing = device_pending
                raise XiaozhiAdapterError(
                    f"a pending agent operation already exists for this device in state "
                    f"{existing.state}; cancel or reconcile it first"
                )
            existing = self._pending.get(session_id)
            if existing is not None:
                raise XiaozhiAdapterError(
                    f"a pending agent operation already exists in state {existing.state}; "
                    "cancel or reconcile it first"
                )
            if (
                session_id not in self._pending
                and len(self._pending) >= self._max_pending_sessions
            ):
                raise XiaozhiAdapterError("pending confirmation session limit reached")
            self._pending[session_id] = PendingDispatch(
                host=cast(str, host),
                profile=profile,
                operation=operation,
                operation_id=uuid.uuid4().hex,
                prepared_at=time.monotonic(),
                device_id=device_id,
                stored_session_id=stored_session_id,
                binding_revision=cast(int, binding_revision),
                idempotency_key=(
                    f"xiaozhi:new:{uuid.uuid4().hex}"
                    if operation == "create_session"
                    else None
                ),
            )
        return {
            "schema_version": "1.0",
            "status": "confirmation_required",
            "operation": (
                "prepare_new_session"
                if operation == "create_session"
                else "prepare_compress_context"
            ),
            "session_id": session_id,
            "device_id": device_id,
            "host": host,
            "active_profile": profile,
            "stored_session_id": stored_session_id,
            "binding_revision": binding_revision,
            "operation_id": self._pending[session_id].operation_id,
            "expires_in_seconds": int(_PENDING_TTL_SECONDS),
        }

    async def prepare_new_session(
        self, session_id: str, *, device_id: str
    ) -> Mapping[str, object]:
        return await self._prepare_bound_lifecycle(
            session_id,
            device_id=device_id,
            operation="create_session",
        )

    async def prepare_compress_context(
        self, session_id: str, *, device_id: str
    ) -> Mapping[str, object]:
        return await self._prepare_bound_lifecycle(
            session_id,
            device_id=device_id,
            operation="compress_session",
        )

    async def confirm_shared_dispatch(
        self, session_id: str, *, device_id: str
    ) -> Mapping[str, object]:
        async with self._state_lock:
            pending = self._pending.get(session_id)
        if pending is None or pending.device_id != device_id:
            raise XiaozhiAdapterError("no pending Hermes command for this device session")
        if pending.state != "prepared":
            raise XiaozhiAdapterError(
                f"pending Hermes operation is {pending.state}; reconcile it before retrying"
            )
        if time.monotonic() - pending.prepared_at > _PENDING_TTL_SECONDS:
            async with self._state_lock:
                if self._pending.get(session_id) == pending:
                    self._pending.pop(session_id, None)
            raise XiaozhiAdapterError(
                "待确认操作已过期，本次未提交。请重新说出完整请求，并在提示后再次确认"
            )
        submitting = replace(pending, state="submitting")
        async with self._state_lock:
            if self._pending.get(session_id) != pending:
                raise XiaozhiAdapterError("pending Hermes operation changed before confirmation")
            self._pending[session_id] = submitting
        pending = submitting
        binding = await self.show_binding(session_id, device_id=device_id)
        if (
            binding.get("bound") is not True
            or binding.get("host", "hermes") != pending.host
            or binding.get("profile") != pending.profile
            or binding.get("stored_session_id") != pending.stored_session_id
            or binding.get("binding_revision") != pending.binding_revision
        ):
            async with self._state_lock:
                if self._pending.get(session_id) == pending:
                    self._pending.pop(session_id, None)
            raise XiaozhiAdapterError(
                "agent binding changed after preparation; prepare the command again"
            )
        try:
            if pending.operation == "create_session":
                if pending.idempotency_key is None:
                    raise XiaozhiAdapterError("new-session request has no idempotency key")
                response = await self._client.request(
                    "create_session",
                    session_id=session_id,
                    device_id=device_id,
                    confirmed=True,
                    idempotency_key=pending.idempotency_key,
                    host=pending.host,
                    profile=pending.profile,
                    stored_session_id=pending.stored_session_id,
                    binding_revision=pending.binding_revision,
                )
            elif pending.operation == "compress_session":
                response = await self._client.request(
                    "compress_session",
                    session_id=session_id,
                    device_id=device_id,
                    confirmed=True,
                    host=pending.host,
                    profile=pending.profile,
                    stored_session_id=pending.stored_session_id,
                    binding_revision=pending.binding_revision,
                )
            elif pending.operation == "dispatch_shared":
                response = await self._client.request(
                    "dispatch_shared",
                    session_id=session_id,
                    device_id=device_id,
                    input=pending.prompt,
                    confirmed=True,
                    host=pending.host,
                    profile=pending.profile,
                    stored_session_id=pending.stored_session_id,
                    binding_revision=pending.binding_revision,
                )
            else:
                raise XiaozhiAdapterError(
                    "pending action is not a shared Hermes operation"
                )
        except Exception:
            async with self._state_lock:
                if self._pending.get(session_id) == pending:
                    self._pending[session_id] = replace(pending, state="outcome_unknown")
            raise
        response_status = response.get("status")
        uncertain = {
            "pending",
            "outcome_unknown",
            "conflict",
            "partial_failure",
            "reconciliation_required",
        }
        async with self._state_lock:
            if self._pending.get(session_id) == pending:
                if response_status in uncertain:
                    self._pending[session_id] = replace(
                        pending, state=cast(str, response_status)
                    )
                elif type(response_status) is str:
                    self._pending.pop(session_id, None)
                else:
                    self._pending[session_id] = replace(
                        pending, state="outcome_unknown"
                    )
        return response

    async def prepare_dispatch(
        self, session_id: str, prompt: str
    ) -> Mapping[str, object]:
        clean_prompt = self._clean_dispatch_prompt(prompt)
        if len(clean_prompt) > self._max_input_chars:
            raise XiaozhiAdapterError("prompt exceeds the adapter input limit")
        profile_state = await self.list_profiles(session_id)
        if profile_state.get("status") != "ok":
            return profile_state
        profile = profile_state.get("active_profile")
        if type(profile) is not str or not cast(str, profile):
            raise XiaozhiAdapterError("bridge did not return an active profile")
        async with self._state_lock:
            self._drop_expired_prepared_locked(time.monotonic())
            existing = self._pending.get(session_id)
            if existing is not None:
                raise XiaozhiAdapterError(
                    f"a pending agent operation already exists in state {existing.state}; "
                    "cancel or reconcile it first"
                )
            if (
                session_id not in self._pending
                and len(self._pending) >= self._max_pending_sessions
            ):
                raise XiaozhiAdapterError("pending confirmation session limit reached")
            self._pending[session_id] = PendingDispatch(
                host="hermes",
                profile=cast(str, profile),
                operation="dispatch",
                operation_id=uuid.uuid4().hex,
                prepared_at=time.monotonic(),
                prompt=clean_prompt,
            )
        return {
            "schema_version": "1.0",
            "status": "confirmation_required",
            "operation": "prepare_dispatch",
            "session_id": session_id,
            "active_profile": profile,
            "expires_in_seconds": int(_PENDING_TTL_SECONDS),
        }

    async def confirm_dispatch(self, session_id: str) -> Mapping[str, object]:
        async with self._state_lock:
            pending = self._pending.pop(session_id, None)
        if pending is None:
            raise XiaozhiAdapterError("no pending Hermes command for this session")
        if pending.operation != "dispatch":
            raise XiaozhiAdapterError("pending action is not a legacy Hermes dispatch")
        if self._is_expired_prepared(pending, time.monotonic()):
            raise XiaozhiAdapterError(
                "待确认操作已过期，本次未提交。请重新说出完整请求，并在提示后再次确认"
            )
        profile_state = await self.list_profiles(session_id)
        if profile_state.get("status") != "ok":
            return profile_state
        if profile_state.get("active_profile") != pending.profile:
            raise XiaozhiAdapterError(
                "active profile changed after preparation; prepare the command again"
            )
        return await self._client.request(
            "dispatch",
            session_id=session_id,
            input=pending.prompt,
            confirmed=True,
        )

    async def cancel_dispatch(self, session_id: str) -> Mapping[str, object]:
        async with self._state_lock:
            self._drop_expired_prepared_locked(time.monotonic())
            pending = self._pending.get(session_id)
            if pending is not None and pending.state != "prepared":
                raise XiaozhiAdapterError(
                    f"pending Hermes operation is {pending.state}; it cannot be cancelled"
                )
            cancelled = self._pending.pop(session_id, None) is not None
        return {
            "schema_version": "1.0",
            "status": "ok",
            "operation": "cancel_dispatch",
            "session_id": session_id,
            "cancelled": cancelled,
        }

    async def pending_for_device(self, *, device_id: str) -> Mapping[str, object]:
        """Return only the control state needed to preserve the confirmation gate."""

        if not isinstance(device_id, str) or not device_id.strip():
            raise XiaozhiAdapterError("device_id is required")
        async with self._state_lock:
            self._drop_expired_prepared_locked(time.monotonic())
            match = self._pending_for_device_locked(device_id)
            if match is None:
                return {
                    "schema_version": "1.0",
                    "status": "ok",
                    "operation": "pending_for_device",
                    "device_id": device_id,
                    "pending": False,
                }
            pending_session_id, pending = match
            remaining = max(
                0, int(_PENDING_TTL_SECONDS - (time.monotonic() - pending.prepared_at))
            )
            return {
                "schema_version": "1.0",
                "status": "ok",
                "operation": "pending_for_device",
                "device_id": device_id,
                "pending": True,
                "pending_operation": pending.operation,
                "pending_state": pending.state,
                "pending_session_id": pending_session_id,
                "action_id": pending.operation_id,
                "host": pending.host,
                "profile": pending.profile,
                "stored_session_id": pending.stored_session_id,
                "binding_revision": pending.binding_revision,
                "expires_in_seconds": remaining,
            }

    async def cancel_prepared_for_device(
        self, *, device_id: str
    ) -> Mapping[str, object]:
        if not isinstance(device_id, str) or not device_id.strip():
            raise XiaozhiAdapterError("device_id is required")
        async with self._state_lock:
            self._drop_expired_prepared_locked(time.monotonic())
            matches = [
                (pending_session_id, pending)
                for pending_session_id, pending in self._pending.items()
                if pending.device_id == device_id
            ]
            non_prepared = [
                pending.state for _, pending in matches if pending.state != "prepared"
            ]
            if non_prepared:
                raise XiaozhiAdapterError(
                    "device has an Agent operation in state "
                    f"{non_prepared[0]}; reconcile it before switching"
                )
            for pending_session_id, _ in matches:
                self._pending.pop(pending_session_id, None)
        return {
            "schema_version": "1.0",
            "status": "ok",
            "operation": "cancel_prepared_for_device",
            "device_id": device_id,
            "cancelled_count": len(matches),
        }

    async def close(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()


def adapter_from_environment(
    environment: Mapping[str, str] | None = None,
) -> XiaozhiHermesAdapter:
    settings = BridgeProcessSettings.from_environment(environment)
    return XiaozhiHermesAdapter(
        BridgeProcessClient(settings, environment=environment),
    )


__all__ = [
    "BridgeProcessClient",
    "BridgeProcessSettings",
    "PendingDispatch",
    "XiaozhiAdapterError",
    "XiaozhiHermesAdapter",
    "adapter_from_environment",
]
