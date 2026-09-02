from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from os import environ as process_environ
from pathlib import Path
from threading import RLock
from typing import Any, Protocol
from types import MappingProxyType

from mcp.server.fastmcp import FastMCP

from agent_workflow_hub.confirmation import (
    ConfirmationError,
    OperationSummary,
    SessionConfirmationStore,
    canonical_request_fingerprint,
)

from .changes import (
    ChangeConflictError,
    ChangeRequest,
    ChangeValidationError,
    JenkinsChangeService,
    prepare_update_operation,
)
from .client import JenkinsClient, JenkinsClientError
from .concurrency import ConcurrencyLimiter
from .config import ConfigError, load_config, load_policy, resolve_credentials
from .items import CreateItemRequest, ItemOperationError, JenkinsItemService, prepare_create_operation
from .models import JenkinsConfig, OperationRequest, PolicyDecision, WritePermit, request_fingerprint
from .plugins import JenkinsPluginReadService, PluginCapabilityError
from .runs import (
    CancelBuildRequest,
    JenkinsRunService,
    RunOperationError,
    TriggerBuildRequest,
    prepare_cancel_operation,
    prepare_trigger_operation,
)
from .service import OperationPolicyService


class JenkinsMcpRuntimeError(RuntimeError):
    pass


class JenkinsWriteDeniedError(RuntimeError):
    pass


_CONTROLLER_READ_TARGET = "scope.controller"
_ROOT_READ_TARGET = "scope.root"
_NODES_READ_TARGET = "scope.nodes"
_WRITE_TOOL_NAMES = frozenset(
    {
        "jenkins_create_item",
        "jenkins_update_item",
        "jenkins_trigger_build",
        "jenkins_cancel_build",
        "jenkins_abandon_unknown",
    }
)


class _JenkinsFastMCP(FastMCP):
    """Invalidate replay IDs even when FastMCP rejects args before the tool wrapper."""

    def __init__(
        self,
        *,
        name: str,
        instructions: str,
        confirmation_invalidator: Callable[[str], None],
    ) -> None:
        self._confirmation_invalidator = confirmation_invalidator
        super().__init__(name=name, instructions=instructions)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except Exception:
            supplied = arguments.get("confirmation_id") if name in _WRITE_TOOL_NAMES else None
            if supplied is None:
                raise
            if isinstance(supplied, str) and supplied:
                self._confirmation_invalidator(supplied)
            tool = self._tool_manager.get_tool(name)
            if tool is None:
                raise
            return tool.fn_metadata.convert_result(_confirmation_failure_result())


@dataclass(frozen=True)
class _AbandonExecution:
    operation_id: str
    original_request_fingerprint: str


@dataclass(frozen=True)
class _PreparedWrite:
    action: str
    operation: OperationRequest
    exact_request: Mapping[str, object]
    execution: CreateItemRequest | ChangeRequest | TriggerBuildRequest | CancelBuildRequest | _AbandonExecution


class JenkinsMcpBackend(Protocol):
    def controller_info(self, controller: str) -> dict[str, object]: ...

    def list_nodes(self, controller: str) -> dict[str, object]: ...

    def list_items(self, controller: str) -> dict[str, object]: ...

    def get_item(self, controller: str, item_path: str) -> dict[str, object]: ...

    def create_item(self, **kwargs: object) -> dict[str, object]: ...

    def config_snapshot(self, **kwargs: object) -> dict[str, object]: ...

    def config_preview(self, **kwargs: object) -> dict[str, object]: ...

    def update_item(self, **kwargs: object) -> dict[str, object]: ...

    def trigger_build(self, **kwargs: object) -> dict[str, object]: ...

    def observe_build(self, operation_id: str) -> dict[str, object]: ...

    def cancel_build(self, **kwargs: object) -> dict[str, object]: ...

    def observe_cancellation(self, operation_id: str) -> dict[str, object]: ...

    def abandon_unknown(
        self, operation_id: str, confirmation_id: str | None = None
    ) -> dict[str, object]: ...

    def progressive_log(self, **kwargs: object) -> dict[str, object]: ...

    def pipeline_runs(self, controller: str, item_path: str) -> dict[str, object]: ...

    def junit_summary(self, controller: str, item_path: str, build_number: int) -> dict[str, object]: ...

    def multibranch_children(self, controller: str, item_path: str) -> dict[str, object]: ...


class JenkinsMcpRuntime:
    """Long-lived, policy-bound services behind the narrow MCP tool surface."""

    def __init__(
        self,
        config: JenkinsConfig,
        policy: OperationPolicyService,
        *,
        environment: Mapping[str, str] | None = None,
        client_factory: Callable[..., JenkinsClient] = JenkinsClient,
        confirmation_store: SessionConfirmationStore[_PreparedWrite] | None = None,
    ) -> None:
        self._config = config
        self._policy = policy
        self._environment = dict(process_environ if environment is None else environment)
        self._client_factory = client_factory
        self._clients: dict[str, JenkinsClient] = {}
        self._runs: dict[str, JenkinsRunService] = {}
        self._concurrency = ConcurrencyLimiter()
        self._runtime_lock = RLock()
        self._closed = False
        self._confirmations = confirmation_store or SessionConfirmationStore()

    @classmethod
    def from_config_file(
        cls,
        ini_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
        confirmation_store: SessionConfirmationStore[_PreparedWrite] | None = None,
    ) -> JenkinsMcpRuntime:
        config = load_config(ini_path)
        policy = OperationPolicyService(config, load_policy(config))
        return cls(
            config,
            policy,
            environment=environment,
            confirmation_store=confirmation_store,
        )

    def close(self) -> None:
        with self._runtime_lock:
            if self._closed:
                return
            self._closed = True
            clients = tuple(self._clients.values())
            self._clients.clear()
            self._runs.clear()
            self._confirmations.clear()
            self._policy.clear_write_permits()
        for client in clients:
            client.close()

    def invalidate_confirmation(self, confirmation_id: str) -> None:
        """Invalidate a supplied replay ID rejected at the MCP schema boundary."""
        self._confirmations.invalidate(confirmation_id)

    def controller_info(self, controller: str) -> dict[str, object]:
        self._authorize_read(controller, _CONTROLLER_READ_TARGET, scope="controller")
        info = self._client(controller).get_controller_info()
        return {"controller": controller, "version": info.version, "mode": info.mode}

    def list_nodes(self, controller: str) -> dict[str, object]:
        self._authorize_read(controller, _NODES_READ_TARGET, scope="nodes")
        return {"nodes": [_metadata(node) for node in self._client(controller).list_nodes()]}

    def list_items(self, controller: str) -> dict[str, object]:
        self._authorize_read(controller, _ROOT_READ_TARGET, scope="root_list")
        return {"items": [_metadata(item) for item in self._client(controller).list_items()]}

    def get_item(self, controller: str, item_path: str) -> dict[str, object]:
        self._authorize_read(controller, item_path)
        return _metadata(self._client(controller).get_item(item_path))

    def create_item(
        self,
        *,
        controller: str,
        item_path: str,
        item_type: str,
        template: str,
        parameters: Mapping[str, str] | None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return self._guarded_write(
            confirmation_id,
            lambda: self._prepare_create(
                controller=controller,
                item_path=item_path,
                item_type=item_type,
                template=template,
                parameters=parameters,
            ),
        )

    def config_snapshot(
        self, *, controller: str, item_path: str, item_type: str, template: str
    ) -> dict[str, object]:
        self._authorize_read(controller, item_path)
        return _metadata(
            JenkinsChangeService(self._client(controller), self._policy).snapshot(
                item_path=item_path, item_type=item_type, template=template
            )
        )

    def config_preview(
        self,
        *,
        controller: str,
        item_path: str,
        item_type: str,
        template: str,
        expected_config_digest: str,
        fields: Mapping[str, object],
        template_parameters: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        self._authorize_read(controller, item_path)
        return _metadata(
            JenkinsChangeService(self._client(controller), self._policy).preview(
                ChangeRequest(
                    controller=controller,
                    item_path=item_path,
                    item_type=item_type,
                    template=template,
                    expected_config_digest=expected_config_digest,
                    fields=_mapping(fields, "fields"),
                    template_parameters=_string_mapping(
                        template_parameters, "template_parameters"
                    ),
                )
            )
        )

    def update_item(
        self,
        *,
        controller: str,
        item_path: str,
        item_type: str,
        template: str,
        expected_config_digest: str,
        expected_payload_digest: str,
        fields: Mapping[str, object],
        template_parameters: Mapping[str, str] | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return self._guarded_write(
            confirmation_id,
            lambda: self._prepare_update(
                controller=controller,
                item_path=item_path,
                item_type=item_type,
                template=template,
                expected_config_digest=expected_config_digest,
                expected_payload_digest=expected_payload_digest,
                fields=fields,
                template_parameters=template_parameters,
            ),
        )

    def trigger_build(
        self,
        *,
        controller: str,
        item_path: str,
        parameters: Mapping[str, str] | None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return self._guarded_write(
            confirmation_id,
            lambda: self._prepare_trigger(
                controller=controller,
                item_path=item_path,
                parameters=parameters,
            ),
        )

    def observe_build(self, operation_id: str) -> dict[str, object]:
        service = self._run_for_operation(operation_id)
        operation = service.operation_request(operation_id)
        self._authorize_read(operation.controller, operation.item_path)
        return _metadata(service.observe(operation_id))

    def cancel_build(
        self,
        *,
        controller: str,
        item_path: str,
        build_number: int,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return self._guarded_write(
            confirmation_id,
            lambda: self._prepare_cancel(
                controller=controller,
                item_path=item_path,
                build_number=build_number,
            ),
        )

    def observe_cancellation(self, operation_id: str) -> dict[str, object]:
        service = self._run_for_operation(operation_id)
        operation = service.operation_request(operation_id)
        self._authorize_read(operation.controller, operation.item_path)
        return _metadata(service.observe_cancellation(operation_id))

    def abandon_unknown(
        self,
        operation_id: str,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return self._guarded_write(
            confirmation_id,
            lambda: self._prepare_abandon(operation_id),
        )

    def progressive_log(
        self, *, controller: str, item_path: str, build_number: int, start: int
    ) -> dict[str, object]:
        self._authorize_read(controller, item_path)
        return _metadata(self._client(controller).get_progressive_log(item_path, build_number, start=start))

    def pipeline_runs(self, controller: str, item_path: str) -> dict[str, object]:
        self._authorize_read(controller, item_path)
        return {
            "runs": [
                _metadata(run)
                for run in JenkinsPluginReadService(self._client(controller)).pipeline_runs(item_path)
            ]
        }

    def junit_summary(self, controller: str, item_path: str, build_number: int) -> dict[str, object]:
        self._authorize_read(controller, item_path)
        return _metadata(
            JenkinsPluginReadService(self._client(controller)).junit_summary(item_path, build_number)
        )

    def multibranch_children(self, controller: str, item_path: str) -> dict[str, object]:
        self._authorize_read(controller, item_path)
        return {
            "items": [
                _metadata(item)
                for item in JenkinsPluginReadService(self._client(controller)).multibranch_children(item_path)
            ]
        }

    def _client(self, controller: str) -> JenkinsClient:
        with self._runtime_lock:
            if self._closed:
                raise JenkinsMcpRuntimeError("Jenkins MCP runtime is closed")
            if not isinstance(controller, str) or controller not in self._config.controllers:
                raise JenkinsMcpRuntimeError("requested Jenkins controller is not configured")
            client = self._clients.get(controller)
            if client is None:
                controller_config = self._config.controllers[controller]
                credentials = resolve_credentials(controller_config, environ=self._environment)
                client = self._client_factory(
                    controller_config,
                    credentials,
                    write_authority=self._policy,
                )
                self._clients[controller] = client
            return client

    def _authorize_read(self, controller: str, item_path: str, *, scope: str = "item") -> None:
        decision = self._policy.check_eligibility(
            OperationRequest(
                controller=controller,
                action="read",
                item_path=item_path,
                read_scope=scope,
            )
        )
        if not decision.allowed:
            raise JenkinsMcpRuntimeError("Jenkins read is outside the configured policy scope")

    def _run(self, controller: str) -> JenkinsRunService:
        with self._runtime_lock:
            client = self._client(controller)
            service = self._runs.get(controller)
            if service is None:
                service = JenkinsRunService(client, self._policy, self._concurrency)
                self._runs[controller] = service
            return service

    def _run_for_operation(self, operation_id: str) -> JenkinsRunService:
        if not isinstance(operation_id, str) or not operation_id:
            raise JenkinsMcpRuntimeError("operation_id must be a nonblank string")
        with self._runtime_lock:
            services = tuple(self._runs.values())
        for service in services:
            if service.owns_operation(operation_id):
                return service
        raise JenkinsMcpRuntimeError("unknown Jenkins operation")

    def _guarded_write(
        self,
        confirmation_id: str | None,
        prepare: Callable[[], _PreparedWrite],
    ) -> dict[str, object]:
        try:
            if confirmation_id is not None and (
                not isinstance(confirmation_id, str) or not confirmation_id
            ):
                raise JenkinsWriteDeniedError("confirmation_id must be a nonblank string")
            return self._dispatch_write(prepare(), confirmation_id)
        except Exception:
            if isinstance(confirmation_id, str) and confirmation_id:
                self._confirmations.invalidate(confirmation_id)
            raise

    def _dispatch_write(
        self,
        prepared: _PreparedWrite,
        confirmation_id: str | None,
    ) -> dict[str, object]:
        self._ensure_open()
        controller = self._write_controller(prepared.operation.controller)
        self._reject_production_http_write(controller)
        decision = self._policy.check_eligibility(prepared.operation)
        if not decision.allowed:
            raise JenkinsWriteDeniedError("Jenkins write is outside the configured policy scope")
        if not controller.confirm_writes:
            if prepared.action == "abandon_unknown":
                return self._execute_abandon(prepared)
            permit = self._policy._issue_write_permit(
                prepared.operation,
                payload_digest=(
                    prepared.operation.change_digest
                    if prepared.action == "update_item"
                    else None
                ),
                base_config_digest=(
                    prepared.operation.base_config_digest
                    if prepared.action == "update_item"
                    else None
                ),
            )
            return self._execute_remote_write(prepared, decision, permit)
        context_fingerprint = self._policy.context_fingerprint(controller.name)
        if confirmation_id is None:
            fingerprint = canonical_request_fingerprint(prepared.exact_request)
            summary = _operation_summary(prepared, controller.environment, fingerprint)
            challenge = self._confirmations.prepare(
                request=prepared.exact_request,
                context_fingerprint=context_fingerprint,
                summary=summary,
                private_payload=prepared,
            )
            return {
                "status": "needs_user_confirmation",
                "confirmation_id": challenge.confirmation_id,
                "request_fingerprint": challenge.request_fingerprint,
                "summary": challenge.summary.to_mapping(),
            }
        consumed = self._confirmations.consume(
            confirmation_id,
            request=prepared.exact_request,
            context_fingerprint=context_fingerprint,
        )
        original = consumed.private_payload
        if not isinstance(original, _PreparedWrite) or original.action != prepared.action:
            raise ConfirmationError("invalid Jenkins confirmation payload")
        replay_decision = self._policy.check_eligibility(original.operation)
        if not replay_decision.allowed:
            raise JenkinsWriteDeniedError("Jenkins write is no longer policy eligible")
        if original.action == "abandon_unknown":
            return self._execute_abandon(original)
        permit = self._policy._issue_write_permit(
            original.operation,
            payload_digest=(
                original.operation.change_digest if original.action == "update_item" else None
            ),
            base_config_digest=(
                original.operation.base_config_digest if original.action == "update_item" else None
            ),
        )
        return self._execute_remote_write(original, replay_decision, permit)

    def _prepare_create(
        self,
        *,
        controller: str,
        item_path: str,
        item_type: str,
        template: str,
        parameters: Mapping[str, str] | None,
    ) -> _PreparedWrite:
        normalized = _string_mapping(parameters, "parameters") or {}
        request = CreateItemRequest(
            controller=controller,
            item_path=item_path,
            item_type=item_type,
            template=template,
            parameters=_frozen_mapping(normalized),
        )
        operation = _snapshot_operation(prepare_create_operation(request))
        return _prepared_write(
            "create_item",
            operation,
            {
                "controller": controller,
                "item_path": operation.item_path,
                "item_type": item_type,
                "template": template,
                "parameters": normalized,
            },
            request,
        )

    def _prepare_update(
        self,
        *,
        controller: str,
        item_path: str,
        item_type: str,
        template: str,
        expected_config_digest: str,
        expected_payload_digest: str,
        fields: Mapping[str, object],
        template_parameters: Mapping[str, str] | None,
    ) -> _PreparedWrite:
        normalized_fields = _mapping(fields, "fields")
        normalized_template_parameters = _string_mapping(
            template_parameters, "template_parameters"
        )
        request = ChangeRequest(
            controller=controller,
            item_path=item_path,
            item_type=item_type,
            template=template,
            expected_config_digest=expected_config_digest,
            expected_payload_digest=expected_payload_digest,
            fields=_frozen_mapping(normalized_fields),
            template_parameters=(
                None
                if normalized_template_parameters is None
                else _frozen_mapping(normalized_template_parameters)
            ),
        )
        _, operation = prepare_update_operation(request)
        exact_request: dict[str, object] = {
            "controller": controller,
            "item_path": item_path,
            "item_type": item_type,
            "template": template,
            "expected_config_digest": expected_config_digest,
            "expected_payload_digest": expected_payload_digest,
            "fields": normalized_fields,
        }
        if normalized_template_parameters is not None:
            exact_request["template_parameters"] = normalized_template_parameters
        return _prepared_write(
            "update_item",
            _snapshot_operation(operation),
            exact_request,
            request,
        )

    def _prepare_trigger(
        self,
        *,
        controller: str,
        item_path: str,
        parameters: Mapping[str, str] | None,
    ) -> _PreparedWrite:
        normalized = _string_mapping(parameters, "parameters") or {}
        request = TriggerBuildRequest(
            controller=controller,
            item_path=item_path,
            parameters=_frozen_mapping(normalized),
        )
        _, operation = prepare_trigger_operation(request)
        return _prepared_write(
            "trigger_build",
            _snapshot_operation(operation),
            {"controller": controller, "item_path": item_path, "parameters": normalized},
            request,
        )

    def _prepare_cancel(
        self,
        *,
        controller: str,
        item_path: str,
        build_number: int,
    ) -> _PreparedWrite:
        request = CancelBuildRequest(
            controller=controller,
            item_path=item_path,
            build_number=build_number,
        )
        _, operation = prepare_cancel_operation(request)
        return _prepared_write(
            "cancel_build",
            _snapshot_operation(operation),
            {
                "controller": controller,
                "item_path": item_path,
                "build_number": build_number,
            },
            request,
        )

    def _prepare_abandon(self, operation_id: str) -> _PreparedWrite:
        if not isinstance(operation_id, str) or not operation_id:
            raise JenkinsMcpRuntimeError("operation_id must be a nonblank string")
        operation = _snapshot_operation(
            self._run_for_operation(operation_id).unknown_outcome_request(operation_id)
        )
        original_fingerprint = request_fingerprint(operation)
        execution = _AbandonExecution(operation_id, original_fingerprint)
        return _prepared_write(
            "abandon_unknown",
            operation,
            {
                "operation_id": operation_id,
                "original_request_fingerprint": original_fingerprint,
            },
            execution,
        )

    def _execute_remote_write(
        self,
        prepared: _PreparedWrite,
        decision: PolicyDecision,
        permit: WritePermit,
    ) -> dict[str, object]:
        execution = prepared.execution
        if isinstance(execution, CreateItemRequest):
            return _metadata(
                JenkinsItemService(self._client(prepared.operation.controller), self._policy).create(
                    execution,
                    permit=permit,
                )
            )
        if isinstance(execution, ChangeRequest):
            return _metadata(
                JenkinsChangeService(self._client(prepared.operation.controller), self._policy).update(
                    execution,
                    permit=permit,
                )
            )
        if isinstance(execution, TriggerBuildRequest):
            return _metadata(
                self._run(prepared.operation.controller).trigger(
                    execution,
                    decision=decision,
                    permit=permit,
                )
            )
        if isinstance(execution, CancelBuildRequest):
            return _metadata(
                self._run(prepared.operation.controller).cancel(
                    execution,
                    decision=decision,
                    permit=permit,
                )
            )
        raise JenkinsMcpRuntimeError("unsupported Jenkins write payload")

    def _execute_abandon(self, prepared: _PreparedWrite) -> dict[str, object]:
        execution = prepared.execution
        if not isinstance(execution, _AbandonExecution):
            raise ConfirmationError("invalid Jenkins abandonment confirmation")
        service = self._run_for_operation(execution.operation_id)
        current = service.unknown_outcome_request(execution.operation_id)
        if request_fingerprint(current) != execution.original_request_fingerprint:
            raise ConfirmationError("unknown outcome record changed")
        return _metadata(
            service.abandon_unknown(
                execution.operation_id,
                expected_request_fingerprint=execution.original_request_fingerprint,
            )
        )

    def _ensure_open(self) -> None:
        with self._runtime_lock:
            if self._closed:
                raise JenkinsMcpRuntimeError("Jenkins MCP runtime is closed")

    def _write_controller(self, controller: str):
        if not isinstance(controller, str) or controller not in self._config.controllers:
            raise JenkinsMcpRuntimeError("requested Jenkins controller is not configured")
        return self._config.controllers[controller]

    @staticmethod
    def _reject_production_http_write(controller: object) -> None:
        if (
            getattr(controller, "environment", None) == "production"
            and str(getattr(controller, "url", "")).casefold().startswith("http://")
        ):
            raise JenkinsWriteDeniedError("production Jenkins writes require HTTPS")


def create_jenkins_mcp_server(backend: JenkinsMcpBackend) -> FastMCP:
    """Expose the fixed Jenkins operation set; no raw HTTP or XML tool exists."""
    invalidator = getattr(backend, "invalidate_confirmation", None)
    server = _JenkinsFastMCP(
        name="jenkins-operations",
        instructions=(
            "Use only the typed Jenkins tools. Writes execute directly by "
            "default only when a controller explicitly sets confirm_writes=false "
            "under the Jenkins account permissions; otherwise a controller "
            "first returns a current-session confirmation challenge and "
            "executes when replayed once with its confirmation_id."
        ),
        confirmation_invalidator=(invalidator if callable(invalidator) else lambda _: None),
    )

    @server.tool()
    def jenkins_controller_info(controller: str) -> dict[str, object]:
        return _tool_result(lambda: backend.controller_info(controller))

    @server.tool()
    def jenkins_list_nodes(controller: str) -> dict[str, object]:
        return _tool_result(lambda: backend.list_nodes(controller))

    @server.tool()
    def jenkins_list_items(controller: str) -> dict[str, object]:
        return _tool_result(lambda: backend.list_items(controller))

    @server.tool()
    def jenkins_get_item(controller: str, item_path: str) -> dict[str, object]:
        return _tool_result(lambda: backend.get_item(controller, item_path))

    @server.tool()
    def jenkins_create_item(
        controller: str,
        item_path: str,
        item_type: str,
        template: str,
        parameters: dict[str, str] | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.create_item(
                controller=controller,
                item_path=item_path,
                item_type=item_type,
                template=template,
                parameters=parameters,
                confirmation_id=confirmation_id,
            )
        )

    @server.tool()
    def jenkins_config_snapshot(
        controller: str, item_path: str, item_type: str, template: str
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.config_snapshot(
                controller=controller, item_path=item_path, item_type=item_type, template=template
            )
        )

    @server.tool()
    def jenkins_config_preview(
        controller: str,
        item_path: str,
        item_type: str,
        template: str,
        expected_config_digest: str,
        fields: dict[str, object],
        template_parameters: dict[str, str] | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.config_preview(
                controller=controller,
                item_path=item_path,
                item_type=item_type,
                template=template,
                expected_config_digest=expected_config_digest,
                fields=fields,
                template_parameters=template_parameters,
            )
        )

    @server.tool()
    def jenkins_update_item(
        controller: str,
        item_path: str,
        item_type: str,
        template: str,
        expected_config_digest: str,
        expected_payload_digest: str,
        fields: dict[str, object],
        template_parameters: dict[str, str] | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.update_item(
                controller=controller,
                item_path=item_path,
                item_type=item_type,
                template=template,
                expected_config_digest=expected_config_digest,
                expected_payload_digest=expected_payload_digest,
                fields=fields,
                template_parameters=template_parameters,
                confirmation_id=confirmation_id,
            )
        )

    @server.tool()
    def jenkins_trigger_build(
        controller: str,
        item_path: str,
        parameters: dict[str, str] | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.trigger_build(
                controller=controller,
                item_path=item_path,
                parameters=parameters,
                confirmation_id=confirmation_id,
            )
        )

    @server.tool()
    def jenkins_observe_build(operation_id: str) -> dict[str, object]:
        return _tool_result(lambda: backend.observe_build(operation_id))

    @server.tool()
    def jenkins_cancel_build(
        controller: str,
        item_path: str,
        build_number: int,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.cancel_build(
                controller=controller,
                item_path=item_path,
                build_number=build_number,
                confirmation_id=confirmation_id,
            )
        )

    @server.tool()
    def jenkins_observe_cancellation(operation_id: str) -> dict[str, object]:
        return _tool_result(lambda: backend.observe_cancellation(operation_id))

    @server.tool()
    def jenkins_abandon_unknown(
        operation_id: str,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.abandon_unknown(
                operation_id,
                confirmation_id=confirmation_id,
            )
        )

    @server.tool()
    def jenkins_get_progressive_log(
        controller: str, item_path: str, build_number: int, start: int = 0
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.progressive_log(
                controller=controller, item_path=item_path, build_number=build_number, start=start
            )
        )

    @server.tool()
    def jenkins_pipeline_runs(controller: str, item_path: str) -> dict[str, object]:
        return _tool_result(lambda: backend.pipeline_runs(controller, item_path))

    @server.tool()
    def jenkins_junit_summary(
        controller: str, item_path: str, build_number: int
    ) -> dict[str, object]:
        return _tool_result(lambda: backend.junit_summary(controller, item_path, build_number))

    @server.tool()
    def jenkins_multibranch_children(controller: str, item_path: str) -> dict[str, object]:
        return _tool_result(lambda: backend.multibranch_children(controller, item_path))

    return server


def run_stdio_server(server: FastMCP) -> None:
    server.run(transport="stdio")


def run_jenkins_mcp(ini_path: Path) -> None:
    runtime = JenkinsMcpRuntime.from_config_file(ini_path)
    try:
        run_stdio_server(create_jenkins_mcp_server(runtime))
    finally:
        runtime.close()


def _tool_result(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
    try:
        result = operation()
        if result.get("status") == "needs_user_confirmation":
            if set(result) != {
                "status",
                "confirmation_id",
                "request_fingerprint",
                "summary",
            }:
                raise JenkinsMcpRuntimeError("invalid Jenkins confirmation result")
            return result
        return {"status": "ok", "data": result}
    except Exception as exc:
        status, message = _mapped_error(exc)
        return {"status": status, "message": message}


def _mapped_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ConfirmationError):
        return (
            "denied_or_failed",
            "Jenkins confirmation is invalid, expired, already used, or no longer matches",
        )
    if isinstance(exc, ChangeConflictError):
        return "conflict", "Jenkins configuration changed; take a new snapshot and preview"
    if isinstance(exc, PluginCapabilityError):
        return "unsupported", "required Jenkins plugin capability is unavailable"
    if isinstance(exc, (ConfigError, JenkinsMcpRuntimeError)):
        return "configuration_error", "Jenkins MCP configuration or controller selection is invalid"
    if isinstance(
        exc,
        (
            ChangeValidationError,
            ItemOperationError,
            RunOperationError,
            JenkinsClientError,
            JenkinsWriteDeniedError,
        ),
    ):
        return "denied_or_failed", "Jenkins operation was denied or did not complete with safe evidence"
    return "failed", "Jenkins MCP operation failed"


def _confirmation_failure_result() -> dict[str, object]:
    status, message = _mapped_error(ConfirmationError("invalid confirmation"))
    return {"status": status, "message": message}


def _metadata(value: object) -> dict[str, object]:
    if not is_dataclass(value):
        raise JenkinsMcpRuntimeError("Jenkins service returned an unsupported result")
    result = asdict(value)
    if not isinstance(result, dict):
        raise JenkinsMcpRuntimeError("Jenkins service returned an unsupported result")
    return result


def _prepared_write(
    action: str,
    operation: OperationRequest,
    exact_request: Mapping[str, object],
    execution: CreateItemRequest | ChangeRequest | TriggerBuildRequest | CancelBuildRequest | _AbandonExecution,
) -> _PreparedWrite:
    return _PreparedWrite(
        action=action,
        operation=_snapshot_operation(operation),
        exact_request=_frozen_mapping(exact_request),
        execution=execution,
    )


def _operation_summary(
    prepared: _PreparedWrite,
    environment: str,
    fingerprint: str,
) -> OperationSummary:
    operation = prepared.operation
    exact = prepared.exact_request
    if prepared.action == "abandon_unknown":
        parameters: Mapping[str, object] = {
            "operation_id": exact["operation_id"],
            "original_request_fingerprint": exact["original_request_fingerprint"],
        }
        object_ref = str(exact["operation_id"])
        risk = "Releases local concurrency tracking despite an unresolved remote outcome."
        reconcile = "Verify the Jenkins build manually before submitting any replacement write."
    else:
        parameters = {
            key: _redact_confirmation_value(value, key=key)
            for key, value in exact.items()
            if key not in {"controller", "item_path"}
        }
        object_ref = operation.item_path
        risk = "Executes one policy-scoped Jenkins write against the selected controller."
        reconcile = "Use Jenkins readback and reconcile manually; uncertain writes are never retried."
    return OperationSummary(
        target=operation.controller,
        environment=environment,
        action=prepared.action,
        object_ref=object_ref,
        parameters=parameters,
        risk=risk,
        rollback_or_reconcile=reconcile,
        request_fingerprint=fingerprint,
    )


def _snapshot_operation(operation: OperationRequest) -> OperationRequest:
    """Detach a host confirmation challenge from any caller-owned mutable mappings."""
    return OperationRequest(
        controller=operation.controller,
        action=operation.action,
        item_path=operation.item_path,
        item_type=operation.item_type,
        template=operation.template,
        fields=frozenset(operation.fields),
        parameters=_frozen_mapping(operation.parameters),
        change_digest=operation.change_digest,
        target_build_number=operation.target_build_number,
        base_config_digest=operation.base_config_digest,
        read_scope=operation.read_scope,
        confirmation_details=_frozen_mapping(operation.confirmation_details),
    )


def _frozen_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({str(key): _frozen_value(item) for key, item in value.items()})


def _frozen_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _frozen_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_frozen_value(item) for item in value)
    return value


def _redact_confirmation_value(value: object, *, key: str = "") -> object:
    if _sensitive_confirmation_key(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(child_key): _redact_confirmation_value(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_confirmation_value(item) for item in value]
    return value


def _sensitive_confirmation_key(key: str) -> bool:
    normalized = key.casefold()
    return any(
        marker in normalized
        for marker in (
            "authorization",
            "token",
            "password",
            "secret",
            "credential",
            "cookie",
            "crumb",
            "key",
        )
    )


def _mapping(value: Mapping[str, object], field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise JenkinsMcpRuntimeError(f"{field} must be an object")
    return dict(value)


def _string_mapping(value: Mapping[str, str] | None, field: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise JenkinsMcpRuntimeError(f"{field} must be a string mapping")
    return dict(value)
