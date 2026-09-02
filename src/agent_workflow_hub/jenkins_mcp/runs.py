from __future__ import annotations

import secrets
from dataclasses import dataclass
from threading import RLock
from typing import Mapping, Protocol

from .client import JenkinsBuild, JenkinsClientError, JenkinsQueueItem
from .concurrency import ConcurrencyLimiter, OperationLease
from .models import OperationRequest, PolicyDecision, WritePermit, request_fingerprint
from .service import OperationPolicyService


class RunOperationError(RuntimeError):
    pass


class _RunClient(Protocol):
    def _trigger_build(self, item_path: str, parameters: dict[str, str], permit: WritePermit) -> int: ...

    def get_queue_item(self, queue_id: int) -> JenkinsQueueItem: ...

    def get_build(self, item_path: str, number: int) -> JenkinsBuild: ...

    def _cancel_build(self, item_path: str, number: int, permit: WritePermit) -> None: ...


@dataclass(frozen=True)
class TriggerBuildRequest:
    controller: str
    item_path: str
    parameters: Mapping[str, str]


@dataclass(frozen=True)
class CancelBuildRequest:
    controller: str
    item_path: str
    build_number: int


@dataclass(frozen=True)
class BuildSubmission:
    operation_id: str
    state: str
    queue_id: int | None


@dataclass(frozen=True)
class BuildStatus:
    operation_id: str
    state: str
    queue_id: int | None
    build_number: int | None
    result: str | None


@dataclass(frozen=True)
class CancelSubmission:
    operation_id: str
    state: str


@dataclass
class _ActiveBuildOperation:
    request: OperationRequest
    lease: OperationLease
    queue_id: int | None
    state: str
    build_number: int | None = None


@dataclass
class _ActiveCancellation:
    request: OperationRequest
    lease: OperationLease
    build_number: int
    state: str


class JenkinsRunService:
    """Policy-controlled build submission that never retries an uncertain POST."""

    def __init__(
        self,
        client: _RunClient,
        policy: OperationPolicyService,
        concurrency: ConcurrencyLimiter,
    ) -> None:
        self._client = client
        self._policy = policy
        self._concurrency = concurrency
        self._active: dict[str, _ActiveBuildOperation] = {}
        self._cancellations: dict[str, _ActiveCancellation] = {}
        self._operations_lock = RLock()

    def trigger(
        self,
        request: TriggerBuildRequest,
        *,
        decision: PolicyDecision,
        permit: WritePermit,
    ) -> BuildSubmission:
        parameters, operation = prepare_trigger_operation(request)
        if (
            not decision.allowed
            or decision.request_fingerprint != request_fingerprint(operation)
            or self._policy.inspect_write_permit(permit) != operation
        ):
            raise RunOperationError("trigger_build denied: write authorization is invalid")
        lease = self._concurrency.acquire(decision, operation)
        operation_id = secrets.token_urlsafe(18)
        try:
            queue_id = self._client._trigger_build(request.item_path, parameters, permit)
        except JenkinsClientError as exc:
            if exc.kind in {"timeout", "transport", "outcome_unknown"}:
                with self._operations_lock:
                    self._active[operation_id] = _ActiveBuildOperation(
                        request=operation,
                        lease=lease,
                        queue_id=None,
                        state="outcome_unknown",
                    )
                return BuildSubmission(operation_id=operation_id, state="outcome_unknown", queue_id=None)
            lease.release()
            raise RunOperationError("Jenkins build submission failed") from None
        if not isinstance(queue_id, int) or isinstance(queue_id, bool) or queue_id < 1:
            lease.release()
            raise RunOperationError("Jenkins did not return a valid queue identity")
        with self._operations_lock:
            self._active[operation_id] = _ActiveBuildOperation(
                request=operation,
                lease=lease,
                queue_id=queue_id,
                state="queued",
            )
        return BuildSubmission(operation_id=operation_id, state="queued", queue_id=queue_id)

    def cancel(
        self,
        request: CancelBuildRequest,
        *,
        decision: PolicyDecision,
        permit: WritePermit,
    ) -> CancelSubmission:
        build_number, operation = prepare_cancel_operation(request)
        if (
            not decision.allowed
            or decision.request_fingerprint != request_fingerprint(operation)
            or self._policy.inspect_write_permit(permit) != operation
        ):
            raise RunOperationError("cancel_build denied: write authorization is invalid")
        lease = self._concurrency.acquire(decision, operation)
        operation_id = secrets.token_urlsafe(18)
        try:
            self._client._cancel_build(request.item_path, build_number, permit)
        except JenkinsClientError as exc:
            if exc.kind in {"timeout", "transport", "outcome_unknown"}:
                with self._operations_lock:
                    self._cancellations[operation_id] = _ActiveCancellation(
                        request=operation,
                        lease=lease,
                        build_number=build_number,
                        state="outcome_unknown",
                    )
                return CancelSubmission(operation_id=operation_id, state="outcome_unknown")
            lease.release()
            raise RunOperationError("Jenkins build cancellation failed") from None
        with self._operations_lock:
            self._cancellations[operation_id] = _ActiveCancellation(
                request=operation,
                lease=lease,
                build_number=build_number,
                state="cancel_requested",
            )
        return CancelSubmission(operation_id=operation_id, state="cancel_requested")

    def observe(self, operation_id: str) -> BuildStatus:
        # Keep the lookup, remote observation and lease release atomic per service instance.
        with self._operations_lock:
            return self._observe_locked(operation_id)

    def owns_operation(self, operation_id: str) -> bool:
        """Return whether this runtime owns the opaque operation without observing Jenkins."""
        with self._operations_lock:
            return operation_id in self._active or operation_id in self._cancellations

    def operation_request(self, operation_id: str) -> OperationRequest:
        """Return an operation's policy-bound target before exposing a remote read result."""
        with self._operations_lock:
            active = self._active.get(operation_id)
            if active is not None:
                return active.request
            cancellation = self._cancellations.get(operation_id)
            if cancellation is not None:
                return cancellation.request
        raise RunOperationError("unknown Jenkins operation")

    def unknown_outcome_request(self, operation_id: str) -> OperationRequest:
        """Return only operations whose uncertain remote outcome may be abandoned."""
        with self._operations_lock:
            active = self._active.get(operation_id)
            if active is not None and active.state == "outcome_unknown":
                return active.request
            cancellation = self._cancellations.get(operation_id)
            if cancellation is not None and cancellation.state == "outcome_unknown":
                return cancellation.request
        raise RunOperationError("operation does not have an unknown outcome")

    def _observe_locked(self, operation_id: str) -> BuildStatus:
        active = self._active.get(operation_id)
        if active is None:
            raise RunOperationError("unknown build operation")
        if active.state == "outcome_unknown" and active.queue_id is None:
            return BuildStatus(operation_id, "outcome_unknown", None, None, None)
        try:
            if active.build_number is None:
                if active.queue_id is None:
                    raise RunOperationError("queued build is missing its queue identity")
                queue = self._client.get_queue_item(active.queue_id)
                if queue.cancelled:
                    return self._finish(operation_id, active, "cancelled", None, None)
                if queue.executable_number is None:
                    return BuildStatus(operation_id, "queued", active.queue_id, None, None)
                active.build_number = queue.executable_number
            build = self._client.get_build(active.request.item_path, active.build_number)
        except JenkinsClientError:
            active.state = "outcome_unknown"
            return BuildStatus(
                operation_id,
                "outcome_unknown",
                active.queue_id,
                active.build_number,
                None,
            )
        if build.building:
            active.state = "running"
            return BuildStatus(operation_id, "running", active.queue_id, build.number, None)
        if build.result is None:
            active.state = "outcome_unknown"
            return BuildStatus(operation_id, "outcome_unknown", active.queue_id, build.number, None)
        return self._finish(operation_id, active, "completed", build.number, build.result)

    def observe_cancellation(self, operation_id: str) -> BuildStatus:
        # See observe(): cancellation state shares the same operation table lifetime.
        with self._operations_lock:
            return self._observe_cancellation_locked(operation_id)

    def _observe_cancellation_locked(self, operation_id: str) -> BuildStatus:
        cancellation = self._cancellations.get(operation_id)
        if cancellation is None:
            raise RunOperationError("unknown build cancellation")
        try:
            build = self._client.get_build(cancellation.request.item_path, cancellation.build_number)
        except JenkinsClientError:
            cancellation.state = "outcome_unknown"
            return BuildStatus(
                operation_id,
                "outcome_unknown",
                None,
                cancellation.build_number,
                None,
            )
        if build.building:
            state = "outcome_unknown" if cancellation.state == "outcome_unknown" else "cancel_requested"
            return BuildStatus(operation_id, state, None, build.number, None)
        if build.result is None:
            cancellation.state = "outcome_unknown"
            return BuildStatus(operation_id, "outcome_unknown", None, build.number, None)
        cancellation.lease.release()
        del self._cancellations[operation_id]
        state = "cancelled" if build.result == "ABORTED" else "completed_before_cancel"
        return BuildStatus(operation_id, state, None, build.number, build.result)

    def abandon_unknown(
        self,
        operation_id: str,
        *,
        expected_request_fingerprint: str,
    ) -> BuildStatus:
        with self._operations_lock:
            return self._abandon_unknown_locked(
                operation_id,
                expected_request_fingerprint=expected_request_fingerprint,
            )

    def _abandon_unknown_locked(
        self,
        operation_id: str,
        *,
        expected_request_fingerprint: str,
    ) -> BuildStatus:
        active = self._active.get(operation_id)
        cancellation = self._cancellations.get(operation_id)
        if active is None and cancellation is None:
            raise RunOperationError("unknown build operation")
        state = active.state if active is not None else cancellation.state
        if state != "outcome_unknown":
            raise RunOperationError("only an unknown build outcome can be abandoned")
        request = active.request if active is not None else cancellation.request
        if request_fingerprint(request) != expected_request_fingerprint:
            raise RunOperationError("unknown outcome operation changed before abandonment")
        if active is not None:
            active.lease.release()
            del self._active[operation_id]
            return BuildStatus(
                operation_id,
                "abandoned_after_confirmation",
                active.queue_id,
                active.build_number,
                None,
            )
        assert cancellation is not None
        cancellation.lease.release()
        del self._cancellations[operation_id]
        return BuildStatus(
            operation_id,
            "abandoned_after_confirmation",
            None,
            cancellation.build_number,
            None,
        )

    def _finish(
        self,
        operation_id: str,
        active: _ActiveBuildOperation,
        state: str,
        build_number: int | None,
        result: str | None,
    ) -> BuildStatus:
        active.lease.release()
        del self._active[operation_id]
        return BuildStatus(operation_id, state, active.queue_id, build_number, result)


def _normalize_parameters(parameters: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(parameters, Mapping):
        raise RunOperationError("build parameters must be a mapping of strings")
    result: dict[str, str] = {}
    for name, value in parameters.items():
        if not isinstance(name, str) or not name or not isinstance(value, str):
            raise RunOperationError("build parameters must be a mapping of strings")
        result[name] = value
    return result


def prepare_trigger_operation(
    request: TriggerBuildRequest,
) -> tuple[dict[str, str], OperationRequest]:
    parameters = _normalize_parameters(request.parameters)
    return parameters, OperationRequest(
        controller=request.controller,
        action="trigger_build",
        item_path=request.item_path,
        parameters=parameters,
    )


def prepare_cancel_operation(
    request: CancelBuildRequest,
) -> tuple[int, OperationRequest]:
    build_number = _positive_build_number(request.build_number)
    return build_number, OperationRequest(
        controller=request.controller,
        action="cancel_build",
        item_path=request.item_path,
        target_build_number=build_number,
    )


def _positive_build_number(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise RunOperationError("build number must be a positive integer")
    return value
