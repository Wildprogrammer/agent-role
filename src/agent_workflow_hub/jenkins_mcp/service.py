from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import RLock

from agent_workflow_hub.confirmation import canonical_request_fingerprint

from .models import (
    ControllerConfig,
    JenkinsConfig,
    OperationRequest,
    Policy,
    PolicyDecision,
    WritePermit,
    request_fingerprint,
)
from .policy import _evaluate_policy


_WRITE_PERMIT_TTL = timedelta(minutes=5)


class OperationPolicyService:
    """Policy evaluator and private one-shot Jenkins write-permit authority."""

    def __init__(self, config: JenkinsConfig, policy: Policy | None) -> None:
        self._config = config
        self._policy = policy
        self._clock = _utc_now
        self._write_permits: dict[str, tuple[WritePermit, OperationRequest]] = {}
        self._write_permit_lock = RLock()

    @classmethod
    def _for_test(
        cls,
        config: JenkinsConfig,
        policy: Policy,
        *,
        clock: Callable[[], datetime],
    ) -> OperationPolicyService:
        service = cls(config, policy)
        service._clock = clock
        return service

    def evaluate(self, request: OperationRequest) -> PolicyDecision:
        return self.check_eligibility(request)

    def check_eligibility(self, request: OperationRequest) -> PolicyDecision:
        if self._policy is None:
            return PolicyDecision(
                True,
                False,
                "no explicit policy configured; "
                "Jenkins account permissions are the default boundary",
                controller=request.controller,
                action=request.action,
                request_fingerprint=request_fingerprint(request),
            )
        return _evaluate_policy(
            self._policy,
            request,
            config=self._config,
            now=self._now(),
        )

    def controls_controller(self, controller: ControllerConfig) -> bool:
        """Return whether this runtime was built for this exact controller config."""
        return self._config.controllers.get(controller.name) == controller

    def context_fingerprint(self, controller_name: str) -> str:
        """Bind a challenge to current controller security settings and loaded policy."""
        controller = self._config.controllers.get(controller_name)
        if controller is None:
            raise ValueError("requested Jenkins controller is not configured")
        return canonical_request_fingerprint(
            {
                "controller": {
                    "name": controller.name,
                    "url": controller.url,
                    "environment": controller.environment,
                    "username_env": controller.username_env,
                    "token_env": controller.token_env,
                    "allow_insecure_http": controller.allow_insecure_http,
                    "require_crumb": controller.require_crumb,
                    "ca_bundle": None if controller.ca_bundle is None else str(controller.ca_bundle),
                    "username_sha256": _secret_digest(controller.username),
                    "token_sha256": _secret_digest(controller.token),
                },
                "policy": (
                    []
                    if self._policy is None
                    else [
                        {
                            "name": rule.name,
                            "action": rule.action,
                            "controllers": sorted(rule.controllers),
                            "environments": sorted(rule.environments),
                            "path_prefixes": list(rule.path_prefixes),
                            "item_types": None if rule.item_types is None else sorted(rule.item_types),
                            "templates": None if rule.templates is None else sorted(rule.templates),
                            "allowed_fields": (
                                None if rule.allowed_fields is None else sorted(rule.allowed_fields)
                            ),
                            "parameters": (
                                None
                                if rule.parameters is None
                                else {
                                    name: sorted(values)
                                    for name, values in sorted(rule.parameters.items())
                                }
                            ),
                            "expires_at": (
                                None
                                if rule.expires_at is None
                                else rule.expires_at.astimezone(UTC).isoformat()
                            ),
                            "max_concurrent": rule.max_concurrent,
                            "read_scopes": (
                                None if rule.read_scopes is None else sorted(rule.read_scopes)
                            ),
                        }
                        for rule in self._policy.rules
                    ]
                ),
            }
        )

    def _issue_write_permit(
        self,
        request: OperationRequest,
        *,
        payload_digest: str | None = None,
        base_config_digest: str | None = None,
    ) -> WritePermit:
        """Issue one opaque permit after the runtime consumed a session challenge."""
        if request.action == "update_item" and (
            not _is_sha256_digest(payload_digest)
            or not _is_sha256_digest(base_config_digest)
            or request.change_digest != payload_digest
            or request.base_config_digest != base_config_digest
        ):
            raise ValueError("update payload does not match the confirmed change")
        if request.action not in {
            "create_item",
            "update_item",
            "trigger_build",
            "cancel_build",
        }:
            raise ValueError("unsupported Jenkins write permit action")
        item_type = request.item_type or "__jenkins_run__"
        template = request.template or "run-v1"
        with self._write_permit_lock:
            now = self._now()
            decision = self.check_eligibility(request)
            if not decision.allowed:
                raise ValueError("Jenkins write is no longer policy eligible")
            self._purge_expired_permits(now)
            permit_id = self._new_permit_id()
            permit = WritePermit(
                permit_id=permit_id,
                controller=request.controller,
                action=request.action,
                item_path=request.item_path,
                item_type=item_type,
                template=template,
                expires_at=now + _WRITE_PERMIT_TTL,
                payload_digest=payload_digest,
                base_config_digest=base_config_digest,
            )
            self._write_permits[permit.permit_id] = (permit, request)
            return permit

    def _new_permit_id(self) -> str:
        for _ in range(32):
            permit_id = secrets.token_urlsafe(24)
            if permit_id not in self._write_permits:
                return permit_id
        raise ValueError("unable to allocate Jenkins write permit")

    def inspect_write_permit(self, permit: WritePermit) -> OperationRequest | None:
        """Validate a permit without consuming it, for local payload preflight only."""
        with self._write_permit_lock:
            return self._validated_permit_operation(permit)

    def verify_and_consume_write_permit(self, permit: WritePermit) -> bool:
        """Consume the exact issued permit object once at the client POST boundary."""
        with self._write_permit_lock:
            operation = self._validated_permit_operation(permit)
            if operation is None:
                return False
            del self._write_permits[permit.permit_id]
            return True

    def clear_write_permits(self) -> None:
        with self._write_permit_lock:
            self._write_permits.clear()

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("policy service clock must include a timezone")
        return now.astimezone(UTC)

    def _purge_expired_permits(self, now: datetime) -> None:
        for permit_id in tuple(self._write_permits):
            if now >= self._write_permits[permit_id][0].expires_at:
                del self._write_permits[permit_id]

    def _validated_permit_operation(self, permit: WritePermit) -> OperationRequest | None:
        if not isinstance(permit, WritePermit):
            return None
        issued = self._write_permits.get(permit.permit_id)
        if issued is None or issued[0] is not permit:
            return None
        now = self._now()
        if now >= permit.expires_at:
            del self._write_permits[permit.permit_id]
            return None
        eligibility = self.check_eligibility(issued[1])
        if not eligibility.allowed:
            del self._write_permits[permit.permit_id]
            return None
        return issued[1]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _is_sha256_digest(value: str | None) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _secret_digest(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
