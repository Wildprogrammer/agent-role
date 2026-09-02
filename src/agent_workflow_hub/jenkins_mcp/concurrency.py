from __future__ import annotations

from collections import Counter
from threading import Lock

from .models import OperationRequest, PolicyDecision, request_fingerprint


class ConcurrencyLimitError(RuntimeError):
    pass


class ConcurrencyLimiter:
    """Process-local leases used by Jenkins build execution drivers."""

    def __init__(self) -> None:
        self._active: Counter[tuple[str, str]] = Counter()
        self._lock = Lock()

    def acquire(self, decision: PolicyDecision, request: OperationRequest) -> OperationLease:
        if not decision.allowed or request.action not in {
            "trigger_build",
            "cancel_build",
        }:
            raise ConcurrencyLimitError(
                "a concurrency lease requires a policy-approved build action"
            )
        if decision.rule_name is None:
            # No explicit policy: Jenkins account permissions are the default
            # boundary, so no process-local concurrency limit applies.
            if (
                decision.max_concurrent is not None
                or decision.controller != request.controller
                or decision.action != request.action
                or decision.request_fingerprint != request_fingerprint(request)
            ):
                raise ConcurrencyLimitError(
                    "a concurrency lease requires a policy-approved build action"
                )
            return OperationLease(self, (request.controller, request.action))
        if (
            decision.max_concurrent is None
            or decision.controller != request.controller
            or decision.action != request.action
            or decision.request_fingerprint != request_fingerprint(request)
        ):
            raise ConcurrencyLimitError(
                "a concurrency lease requires a policy-approved build action"
            )

        key = (decision.rule_name, request.controller)
        with self._lock:
            if self._active[key] >= decision.max_concurrent:
                raise ConcurrencyLimitError("Jenkins build concurrency limit has been reached")
            self._active[key] += 1
        return OperationLease(self, key)

    def _release(self, key: tuple[str, str]) -> None:
        with self._lock:
            if self._active[key] <= 1:
                self._active.pop(key, None)
            else:
                self._active[key] -= 1


class OperationLease:
    def __init__(self, limiter: ConcurrencyLimiter, key: tuple[str, str]) -> None:
        self._limiter = limiter
        self._key = key
        self._released = False
        self._lock = Lock()

    def __enter__(self) -> OperationLease:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._limiter._release(self._key)
