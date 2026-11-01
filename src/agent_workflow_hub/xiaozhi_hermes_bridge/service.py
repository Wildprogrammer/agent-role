"""In-memory profile routing and bounded Hermes Runs API transport."""

from __future__ import annotations

import time
from math import isfinite
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import quote

import httpx

from .contracts import BridgeConfig, BridgeRequest, ProfileRoute


_TERMINAL_STATUSES = frozenset(
    ("completed", "failed", "cancelled", "interrupted", "waiting_for_approval")
)
_NONTERMINAL_STATUSES = frozenset(("queued", "pending", "running"))
_MAX_SESSIONS = 1_024


class BridgeRejected(ValueError):
    """Raised for a request rejected before an external call is made."""


class HermesTransportError(RuntimeError):
    """Raised when the Hermes Runs API cannot produce a bounded result."""

    def __init__(
        self,
        message: str,
        *,
        outcome_unknown: bool = False,
        run_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown
        self.run_id = run_id


@dataclass(frozen=True, slots=True)
class HermesRunResult:
    status: str
    run_id: str
    output: object | None


class RunsTransport(Protocol):
    def run(self, route: ProfileRoute, *, input: str, session_id: str) -> HermesRunResult:
        """Submit and poll one Hermes run without approving it."""


class _HttpClient(Protocol):
    def post(self, url: str, **kwargs: object) -> object: ...

    def get(self, url: str, **kwargs: object) -> object: ...


class HermesRunsTransport:
    """Small Hermes Runs API client with loopback-only routes from the contract."""

    def __init__(
        self,
        config: BridgeConfig,
        *,
        environment: Mapping[str, str],
        client: _HttpClient | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._environment = environment
        self._client = client or httpx.Client(
            timeout=config.request_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self._sleeper = sleeper

    @staticmethod
    def _mapping(response: object, *, phase: str) -> Mapping[str, object]:
        try:
            cast(httpx.Response, response).raise_for_status()
            value = cast(httpx.Response, response).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HermesTransportError(f"Hermes {phase} failed: {type(exc).__name__}") from None
        if not isinstance(value, Mapping):
            raise HermesTransportError(f"Hermes {phase} returned a non-object response")
        HermesRunsTransport._validate_finite_json(value, phase=phase)
        return cast(Mapping[str, object], value)

    @staticmethod
    def _validate_finite_json(value: object, *, phase: str, depth: int = 0) -> None:
        if depth > 64:
            raise HermesTransportError(f"Hermes {phase} response is too deeply nested")
        if value is None or type(value) in (bool, int, str):
            return
        if type(value) is float:
            if isfinite(cast(float, value)):
                return
            raise HermesTransportError(f"Hermes {phase} response contains non-finite JSON")
        if type(value) is list:
            for item in cast(list[object], value):
                HermesRunsTransport._validate_finite_json(
                    item, phase=phase, depth=depth + 1
                )
            return
        if isinstance(value, Mapping) and all(type(key) is str for key in value):
            for item in value.values():
                HermesRunsTransport._validate_finite_json(
                    item, phase=phase, depth=depth + 1
                )
            return
        raise HermesTransportError(f"Hermes {phase} response is not finite JSON")

    def run(self, route: ProfileRoute, *, input: str, session_id: str) -> HermesRunResult:
        api_key = self._environment.get(route.api_key_env, "")
        if not api_key:
            raise HermesTransportError(
                f"Hermes API key environment variable is missing: {route.api_key_env}"
            )
        headers = {"Authorization": f"Bearer {api_key}"}
        runs_url = f"{route.api_server_base_url}/v1/runs"
        try:
            submitted = self._client.post(
                runs_url,
                headers=headers,
                json={"input": input, "session_id": session_id},
            )
        except httpx.HTTPError as exc:
            raise HermesTransportError(
                f"Hermes submit outcome is unknown: {type(exc).__name__}",
                outcome_unknown=True,
            ) from None
        try:
            submit_data = self._mapping(submitted, phase="submit")
        except HermesTransportError as exc:
            raise HermesTransportError(
                str(exc), outcome_unknown=True
            ) from None
        run_id = submit_data.get("run_id")
        if type(run_id) is not str or not cast(str, run_id).strip():
            raise HermesTransportError(
                "Hermes submit response has no run_id", outcome_unknown=True
            )
        encoded_run_id = quote(cast(str, run_id), safe="")

        for poll_index in range(self._config.max_polls):
            if poll_index:
                self._sleeper(float(self._config.poll_interval_seconds))
            try:
                response = self._client.get(
                    f"{runs_url}/{encoded_run_id}",
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise HermesTransportError(
                    f"Hermes poll outcome is unknown: {type(exc).__name__}",
                    outcome_unknown=True,
                    run_id=cast(str, run_id),
                ) from None
            try:
                data = self._mapping(response, phase="poll")
            except HermesTransportError as exc:
                raise HermesTransportError(
                    str(exc),
                    outcome_unknown=True,
                    run_id=cast(str, run_id),
                ) from None
            status = data.get("status")
            if type(status) is not str:
                raise HermesTransportError(
                    "Hermes poll response has no status",
                    outcome_unknown=True,
                    run_id=cast(str, run_id),
                )
            if status in _TERMINAL_STATUSES:
                return HermesRunResult(
                    status=cast(str, status),
                    run_id=cast(str, run_id),
                    output=data.get("output"),
                )
            if status not in _NONTERMINAL_STATUSES:
                raise HermesTransportError(
                    f"Hermes returned unsupported run status: {status}",
                    outcome_unknown=True,
                    run_id=cast(str, run_id),
                )
        raise HermesTransportError(
            "Hermes run did not finish within the polling bound",
            outcome_unknown=True,
            run_id=cast(str, run_id),
        )


class BridgeService:
    """Keep the active profile in process memory, isolated by session id."""

    def __init__(
        self,
        config: BridgeConfig,
        transport: RunsTransport,
        *,
        max_sessions: int = _MAX_SESSIONS,
    ) -> None:
        self._config = config
        self._transport = transport
        self._active_profiles: dict[str, str] = {}
        self._max_sessions = max_sessions

    def active_profile(self, session_id: str) -> str:
        return self._active_profiles.get(session_id, self._config.default_profile)

    def handle(self, request: BridgeRequest) -> dict[str, object]:
        if request.caller_id != self._config.trusted_caller_id:
            raise BridgeRejected("caller_id does not match the trusted stdio parent")
        active = self.active_profile(request.session_id)
        common: dict[str, object] = {
            "schema_version": "1.0",
            "operation": request.operation,
            "session_id": request.session_id,
        }
        if request.operation == "list_profiles":
            return {
                **common,
                "status": "ok",
                "active_profile": active,
                "profiles": [item.name for item in self._config.profiles],
            }
        if request.operation == "switch_profile":
            assert request.profile is not None
            if self._config.profile(request.profile) is None:
                raise BridgeRejected("profile is not allowlisted")
            if (
                request.session_id not in self._active_profiles
                and len(self._active_profiles) >= self._max_sessions
            ):
                raise BridgeRejected("session limit reached")
            self._active_profiles[request.session_id] = request.profile
            return {
                **common,
                "status": "ok",
                "active_profile": request.profile,
            }

        assert request.input is not None
        if not self._config.dispatch_enabled:
            raise BridgeRejected("dispatch is disabled by configuration")
        if self._config.require_confirmation and not request.confirmed:
            raise BridgeRejected("dispatch requires an explicit confirmed=true request")
        if len(request.input) > self._config.max_input_chars:
            raise BridgeRejected("input exceeds max_input_chars")
        route = self._config.profile(active)
        if route is None:
            raise BridgeRejected("active profile is not allowlisted")
        result = self._transport.run(
            route,
            input=request.input,
            session_id=request.session_id,
        )
        return {
            **common,
            "status": result.status,
            "active_profile": active,
            "run_id": result.run_id,
            "output": result.output,
        }


__all__ = [
    "BridgeRejected",
    "BridgeService",
    "HermesRunResult",
    "HermesRunsTransport",
    "HermesTransportError",
    "RunsTransport",
]
