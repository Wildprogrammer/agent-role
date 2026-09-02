"""The closed, stdio-only MySQL MCP surface.

Only this module registers MCP tools.  Database configuration, policies and
credentials are constructed once for a runtime; callers cannot supply a target,
connection string, password, arbitrary SQL, or a confirmation ticket.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from os import environ as process_environ
from pathlib import Path
from typing import Any, Protocol

from mcp.server.fastmcp import FastMCP

from agent_workflow_hub.confirmation import (
    ConfirmationError,
    OperationSummary,
    SessionConfirmationStore,
    canonical_request_fingerprint,
)

from .client import MySqlClientError, parse_connection_string, read_connection
from .config import ConfigError, load_config, resolve_credentials
from .executor import (
    MySqlExecutionService,
    is_read_only_batch,
    static_policy_operations,
)
from .migrations import MigrationError, MigrationPlan, MySqlMigrationService
from .models import (
    MYSQL_POLICY_ACTIONS,
    MySqlConnectionOverride,
    MySqlCredentials,
    MySqlErrorClassification,
    MySqlExecutionError,
    MySqlExecutionResult,
    MySqlPolicy,
    MySqlStatementResult,
    MySqlTarget,
    PolicyOutcome,
)
from .policy import PolicyError, PolicyService, load_policy
from .reads import MySqlReadError, MySqlReadService
from .writes import (
    DeleteRequest,
    InsertRequest,
    MySqlWriteError,
    MySqlWriteService,
    TransactionRequest,
    UpdateRequest,
)


_WRITE_TOOL_NAMES = frozenset(
    {
        "mysql_insert",
        "mysql_update",
        "mysql_delete",
        "mysql_execute_transaction",
        "mysql_execute_sql",
        "mysql_apply_migration",
    }
)


class MySqlMcpRuntimeError(RuntimeError):
    """A sanitized runtime error suitable for the narrow MCP boundary."""


class MySqlMcpBackend(Protocol):
    def get_capabilities(self) -> dict[str, object]: ...

    def list_schemas(self) -> dict[str, object]: ...

    def list_tables(self, schema: str) -> dict[str, object]: ...

    def describe_table(self, schema: str, table: str) -> dict[str, object]: ...

    def read_query(
        self,
        sql: str,
        *,
        parameters: list[object],
        limit: int | None,
        offset: int,
    ) -> dict[str, object]: ...

    def explain_query(self, sql: str, *, parameters: list[object]) -> dict[str, object]: ...

    def insert(
        self, *, schema: str, table: str, values: dict[str, object], expected_max_rows: int,
        confirmation_id: str | None = None,
    ) -> dict[str, object]: ...

    def update(
        self,
        *,
        schema: str,
        table: str,
        values: dict[str, object],
        where: dict[str, object],
        expected_max_rows: int, confirmation_id: str | None = None,
    ) -> dict[str, object]: ...

    def delete(
        self, *, schema: str, table: str, where: dict[str, object], expected_max_rows: int,
        confirmation_id: str | None = None,
    ) -> dict[str, object]: ...

    def execute_transaction(self, *, operations: list[dict[str, object]], confirmation_id: str | None = None) -> dict[str, object]: ...

    def plan_migration(self, migration_file: str) -> dict[str, object]: ...

    def apply_migration(self, migration_id: str, confirmation_id: str | None = None) -> dict[str, object]: ...

    def schema_snapshot(self, schema: str, table: str) -> dict[str, object]: ...

    def execute_sql(
        self,
        sql: str,
        *,
        params: list[object] | None = None,
        connection_string: str | None = None,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        username: str | None = None,
        password: str | None = None,
        max_result_rows: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]: ...


class _MySqlFastMCP(FastMCP):
    """Invalidate supplied replay IDs even when argument validation fails first."""

    def __init__(self, *, confirmation_invalidator: Callable[[str], None], **kwargs: object) -> None:
        self._confirmation_invalidator = confirmation_invalidator
        super().__init__(**kwargs)  # type: ignore[arg-type]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
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
class _ConfirmedDml:
    action: str
    request: InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest
    preflight: object
    exact_request: Mapping[str, object]


@dataclass(frozen=True)
class _ConfirmedMigration:
    migration_id: str
    plan: MigrationPlan
    exact_request: Mapping[str, object]


class MySqlMcpRuntime:
    """One configuration-bound set of MySQL services behind the MCP surface."""

    def __init__(
        self,
        target: MySqlTarget,
        credentials: MySqlCredentials,
        policy: MySqlPolicy | None = None,
        *,
        connection_factory: Callable[
            [MySqlTarget, MySqlCredentials], AbstractContextManager[object]
        ] = read_connection,
        confirmation_store: SessionConfirmationStore[
            _ConfirmedDml | _ConfirmedMigration
        ] | None = None,
    ) -> None:
        if not isinstance(target, MySqlTarget) or not isinstance(
            credentials, MySqlCredentials
        ):
            raise TypeError("runtime requires validated MySQL configuration")
        if policy is not None and not isinstance(policy, MySqlPolicy):
            raise TypeError("runtime requires validated MySQL configuration")
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._target = target
        self._credentials = credentials
        self._policy = policy
        self._executor = MySqlExecutionService(
            target=target,
            credentials=credentials,
            connection_factory=connection_factory,
            default_max_result_rows=target.max_result_rows,
        )
        if policy is None:
            self._policy_service = None
            self._reads = MySqlReadService(
                target=target,
                credentials=credentials,
                policy=None,
                policy_service=None,
                connection_factory=connection_factory,
            )
            self._writes = MySqlWriteService(
                target=target,
                credentials=credentials,
                policy=None,
                policy_service=None,
                connection_factory=connection_factory,
            )
            self._migrations = MySqlMigrationService(
                target=target,
                credentials=credentials,
                policy=None,
                policy_service=None,
                connection_factory=connection_factory,
            )
        else:
            self._policy_service = PolicyService(policy, target=target)
            self._reads = MySqlReadService(
                target=target,
                credentials=credentials,
                policy=policy,
                policy_service=self._policy_service,
                connection_factory=connection_factory,
            )
            self._writes = MySqlWriteService(
                target=target,
                credentials=credentials,
                policy=policy,
                policy_service=self._policy_service,
                connection_factory=connection_factory,
            )
            self._migrations = MySqlMigrationService(
                target=target,
                credentials=credentials,
                policy=policy,
                policy_service=self._policy_service,
                connection_factory=connection_factory,
            )
        self._confirmations = confirmation_store or SessionConfirmationStore()

    @classmethod
    def from_config_file(
        cls,
        ini_path: Path,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> MySqlMcpRuntime:
        try:
            target = load_config(ini_path)
            runtime_environment = dict(
                process_environ if environment is None else environment
            )
            credentials = resolve_credentials(target, environ=runtime_environment)
            policy = (
                load_policy(target.policy_path)
                if target.policy_path is not None
                else None
            )
        except (ConfigError, PolicyError):
            raise MySqlMcpRuntimeError("invalid_config_or_policy") from None
        return cls(target, credentials, policy)

    def invalidate_confirmation(self, confirmation_id: str) -> None:
        """Terminate a replay ID rejected at the FastMCP argument boundary."""

        self._confirmations.invalidate(confirmation_id)

    def get_capabilities(self) -> dict[str, object]:
        if self._policy is None:
            actions: list[str] = sorted(MYSQL_POLICY_ACTIONS)
        else:
            actions = sorted(
                {
                    action
                    for rule in self._policy.rules
                    if self._target.name in rule.targets
                    and self._target.environment in rule.environments
                    for action in rule.actions
                }
            )
        if self._target.is_read_only:
            actions = [
                action for action in actions if action in {"metadata", "read", "explain"}
            ]
        return {
            "target": self._target.name,
            "environment": self._target.environment,
            "read_only": self._target.is_read_only,
            "actions": actions,
            "policy": "none" if self._policy is None else "configured",
        }

    def list_schemas(self) -> dict[str, object]:
        return self._reads.list_schemas()

    def list_tables(self, schema: str) -> dict[str, object]:
        return self._reads.list_tables(schema)

    def describe_table(self, schema: str, table: str) -> dict[str, object]:
        return self._reads.describe_table(schema, table)

    def read_query(
        self,
        sql: str,
        *,
        parameters: list[object],
        limit: int | None,
        offset: int,
    ) -> dict[str, object]:
        return self._reads.read_query(
            sql,
            parameters=_list_input(parameters, "parameters"),
            limit=limit,
            offset=offset,
        )

    def explain_query(self, sql: str, *, parameters: list[object]) -> dict[str, object]:
        return self._reads.explain_query(
            sql,
            parameters=_list_input(parameters, "parameters"),
        )

    def execute_sql(
        self,
        sql: str,
        *,
        params: list[object] | tuple[object, ...] | None = None,
        connection_string: str | None = None,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        username: str | None = None,
        password: str | None = None,
        max_result_rows: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        if self._target.is_read_only and not is_read_only_batch(sql):
            raise MySqlMcpRuntimeError(
                "configured read-only environment rejects write operations"
            )
        context_sha256 = self._context_fingerprint()
        raw_confirmation_required = self._target.require_confirmation
        if self._policy is not None:
            operations = static_policy_operations(sql, self._target)
            if operations is None:
                raise MySqlExecutionError(
                    MySqlErrorClassification.POLICY_NOT_APPLICABLE,
                    "policy cannot express CALL/DDL/transaction/multi-table statements; disable the optional policy or use typed tools",
                )
            for operation in operations:
                decision = self._policy_service.authorize(operation)
                if decision.outcome is PolicyOutcome.DENY:
                    raise MySqlMcpRuntimeError("not authorized by policy")
                if decision.outcome is PolicyOutcome.NEEDS_USER_CONFIRMATION:
                    raw_confirmation_required = True
        override = _connection_override(
            connection_string=connection_string,
            host=host,
            port=port,
            database=database,
            username=username,
            password=password,
            max_result_rows=max_result_rows,
        )
        exact = _raw_execution_request(
            self._target,
            sql,
            params,
            override,
            context_sha256,
        )
        if confirmation_id is None:
            if raw_confirmation_required:
                return self._issue_confirmation(
                    exact,
                    _raw_execution_summary(
                        self._target,
                        sql,
                        params,
                        override,
                        context_sha256,
                    ),
                    None,
                )
            return _execution_result_mapping(
                self._executor.execute(
                    sql,
                    params=params,
                    max_result_rows=max_result_rows,
                    connection_override=override,
                )
            )
        if not raw_confirmation_required:
            raise MySqlMcpRuntimeError("invalid confirmation_id")
        try:
            self._confirmations.consume(
                confirmation_id,
                request=exact,
                context_fingerprint=context_sha256,
            )
            return _execution_result_mapping(
                self._executor.execute(
                    sql,
                    params=params,
                    max_result_rows=max_result_rows,
                    connection_override=override,
                )
            )
        except Exception as exc:
            if isinstance(confirmation_id, str) and confirmation_id:
                self._confirmations.invalidate(confirmation_id)
            if isinstance(exc, MySqlMcpRuntimeError):
                raise
            raise MySqlMcpRuntimeError(str(exc)) from None

    def insert(
        self, *, schema: str, table: str, values: dict[str, object], expected_max_rows: int,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return self._guarded_dml(
            confirmation_id,
            "insert",
            lambda: InsertRequest(
                schema=schema,
                table=table,
                values=_mapping_input(values, "values"),
                expected_max_rows=expected_max_rows,
            )
        )

    def update(
        self,
        *,
        schema: str,
        table: str,
        values: dict[str, object],
        where: dict[str, object],
        expected_max_rows: int,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return self._guarded_dml(
            confirmation_id,
            "update",
            lambda: UpdateRequest(
                schema=schema,
                table=table,
                values=_mapping_input(values, "values"),
                where=_mapping_input(where, "where"),
                expected_max_rows=expected_max_rows,
            )
        )

    def delete(
        self, *, schema: str, table: str, where: dict[str, object], expected_max_rows: int,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return self._guarded_dml(
            confirmation_id,
            "delete",
            lambda: DeleteRequest(
                schema=schema,
                table=table,
                where=_mapping_input(where, "where"),
                expected_max_rows=expected_max_rows,
            )
        )

    def execute_transaction(
        self, *, operations: list[dict[str, object]], confirmation_id: str | None = None
    ) -> dict[str, object]:
        def request() -> TransactionRequest:
            try:
                return TransactionRequest(
                    tuple(_transaction_operation(operation) for operation in _list_input(operations, "operations"))
                )
            except (TypeError, ValueError, MySqlMcpRuntimeError):
                raise MySqlMcpRuntimeError("invalid_typed_transaction") from None
        return self._guarded_dml(confirmation_id, "transaction", request)

    def plan_migration(self, migration_file: str) -> dict[str, object]:
        try:
            plan = self._migrations.plan_migration(migration_file)
        except MigrationError as exc:
            raise MySqlMcpRuntimeError(str(exc)) from None
        result = asdict(plan)
        self._migrations.discard_plan(plan)
        return result

    def apply_migration(
        self, migration_id: str, confirmation_id: str | None = None
    ) -> dict[str, object]:
        try:
            self._reject_read_only_target()
            if not isinstance(migration_id, str) or not migration_id:
                raise MySqlMcpRuntimeError("invalid_migration_id")
            if confirmation_id is None:
                plan = self._migrations.plan_migration(f"{migration_id}.sql")
                context_sha256 = self._context_fingerprint()
                exact = _migration_request(
                    self._target,
                    plan.migration_id,
                    plan.source_sha256,
                    context_sha256,
                )
                if not plan.requires_confirmation:
                    return self._migrations.apply_migration(plan)
                return self._issue_confirmation(
                    exact,
                    _migration_summary(self._target, plan, context_sha256),
                    _ConfirmedMigration(migration_id, plan, exact),
                )
            source_sha256 = self._migrations.source_digest(migration_id)
            context_sha256 = self._context_fingerprint()
            exact = _migration_request(
                self._target,
                migration_id,
                source_sha256,
                context_sha256,
            )
            consumed = self._confirmations.consume(
                confirmation_id,
                request=exact,
                context_fingerprint=context_sha256,
            )
            private = consumed.private_payload
            if not isinstance(private, _ConfirmedMigration):
                raise ConfirmationError("invalid MySQL migration confirmation payload")
            if not private.plan.requires_confirmation:
                raise MySqlMcpRuntimeError("invalid confirmation_id")
            self._reject_read_only_target()
            return self._migrations.apply_migration(private.plan)
        except Exception as exc:
            if isinstance(confirmation_id, str) and confirmation_id:
                self._confirmations.invalidate(confirmation_id)
            if isinstance(exc, MySqlMcpRuntimeError):
                raise
            raise MySqlMcpRuntimeError(str(exc)) from None

    def schema_snapshot(self, schema: str, table: str) -> dict[str, object]:
        description = self.describe_table(schema, table)
        serialized = json.dumps(
            description,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return {
            "schema": schema,
            "table": table,
            "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            "columns": description["columns"],
        }

    def _guarded_dml(
        self,
        confirmation_id: str | None,
        action: str,
        build: Callable[[], InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest],
    ) -> dict[str, object]:
        try:
            self._reject_read_only_target()
            request = build()
            context_sha256 = self._context_fingerprint()
            exact = _dml_request(self._target, action, request, context_sha256)
            confirmation_required = self._target.require_confirmation
            if self._policy is not None:
                outcome = self._writes.authorize(request)
                if outcome is PolicyOutcome.NEEDS_USER_CONFIRMATION:
                    confirmation_required = True
            if confirmation_id is None:
                if not confirmation_required:
                    return self._writes.execute_request(request)
                preflight = (
                    self._writes.preview(request)
                    if self._policy is not None
                    else None
                )
                return self._issue_confirmation(
                    exact,
                    _dml_summary(self._target, action, request, context_sha256),
                    _ConfirmedDml(action, request, preflight, exact),
                )
            if not confirmation_required:
                raise MySqlMcpRuntimeError("invalid confirmation_id")
            if self._policy is not None:
                self._writes.recheck(request)
            consumed = self._confirmations.consume(
                confirmation_id,
                request=exact,
                context_fingerprint=context_sha256,
            )
            private = consumed.private_payload
            if not isinstance(private, _ConfirmedDml) or private.action != action:
                raise ConfirmationError("invalid MySQL write confirmation payload")
            self._reject_read_only_target()
            if private.preflight is not None:
                return self._writes.execute(private.preflight)  # type: ignore[arg-type]
            return self._writes.execute_request(request)
        except Exception as exc:
            if isinstance(confirmation_id, str) and confirmation_id:
                self._confirmations.invalidate(confirmation_id)
            if isinstance(exc, MySqlMcpRuntimeError):
                raise
            raise MySqlMcpRuntimeError(str(exc)) from None

    def _issue_confirmation(
        self,
        exact: Mapping[str, object],
        summary: OperationSummary,
        private: _ConfirmedDml | _ConfirmedMigration,
    ) -> dict[str, object]:
        challenge = self._confirmations.prepare(
            request=exact,
            context_fingerprint=self._context_fingerprint(),
            summary=summary,
            private_payload=private,
        )
        return {
            "status": "needs_user_confirmation",
            "confirmation_id": challenge.confirmation_id,
            "request_fingerprint": challenge.request_fingerprint,
            "summary": challenge.summary.to_mapping(),
        }

    def _context_fingerprint(self) -> str:
        if self._policy_service is not None:
            return self._policy_service.context_fingerprint()
        return canonical_request_fingerprint(
            {
                "target": self._target.name,
                "environment": self._target.environment,
                "host": self._target.host,
                "port": self._target.port,
                "database": self._target.database,
                "username": self._target.username,
                "read_only_environments": sorted(
                    self._target.read_only_environments
                ),
                "require_confirmation": self._target.require_confirmation,
                "max_result_rows": self._target.max_result_rows,
            }
        )

    def _reject_read_only_target(self) -> None:
        if self._target.is_read_only:
            raise MySqlMcpRuntimeError("configured read-only environment rejects write operations")


def create_mysql_mcp_server(backend: MySqlMcpBackend) -> FastMCP:
    """Expose exactly the bounded MySQL operation surface over MCP stdio."""

    invalidator = getattr(backend, "invalidate_confirmation", None)
    server = _MySqlFastMCP(
        name="mysql-operations",
        instructions=(
            "Use only the fixed typed MySQL tools. Connection details, raw DML/DDL, "
            "and credentials are never accepted as tool inputs. Every write first returns "
            "a current-session confirmation challenge and executes only on one replay."
        ),
        confirmation_invalidator=(invalidator if callable(invalidator) else lambda _: None),
    )

    @server.tool()
    def mysql_get_capabilities() -> dict[str, object]:
        return _tool_result(backend.get_capabilities)

    @server.tool()
    def mysql_list_schemas() -> dict[str, object]:
        return _tool_result(backend.list_schemas)

    @server.tool()
    def mysql_list_tables(schema: str) -> dict[str, object]:
        return _tool_result(lambda: backend.list_tables(schema))

    @server.tool()
    def mysql_describe_table(schema: str, table: str) -> dict[str, object]:
        return _tool_result(lambda: backend.describe_table(schema, table))

    @server.tool()
    def mysql_read_query(
        sql: str,
        parameters: list[object] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.read_query(
                sql,
                parameters=[] if parameters is None else parameters,
                limit=limit,
                offset=offset,
            )
        )

    @server.tool()
    def mysql_explain_query(
        sql: str, parameters: list[object] | None = None
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.explain_query(
                sql,
                parameters=[] if parameters is None else parameters,
            )
        )

    @server.tool()
    def mysql_insert(
        schema: str,
        table: str,
        values: dict[str, object],
        expected_max_rows: int = 1,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.insert(
                schema=schema,
                table=table,
                values=values,
                expected_max_rows=expected_max_rows,
                confirmation_id=confirmation_id,
            )
        )

    @server.tool()
    def mysql_update(
        schema: str,
        table: str,
        values: dict[str, object],
        where: dict[str, object],
        expected_max_rows: int = 1,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.update(
                schema=schema,
                table=table,
                values=values,
                where=where,
                expected_max_rows=expected_max_rows,
                confirmation_id=confirmation_id,
            )
        )

    @server.tool()
    def mysql_delete(
        schema: str,
        table: str,
        where: dict[str, object],
        expected_max_rows: int = 1,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.delete(
                schema=schema,
                table=table,
                where=where,
                expected_max_rows=expected_max_rows,
                confirmation_id=confirmation_id,
            )
        )

    @server.tool()
    def mysql_execute_transaction(
        operations: list[dict[str, object]],
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(lambda: backend.execute_transaction(operations=operations, confirmation_id=confirmation_id))

    @server.tool()
    def mysql_execute_sql(
        sql: str,
        params: list[object] | None = None,
        connection_string: str | None = None,
        host: str | None = None,
        port: int | None = None,
        database: str | None = None,
        username: str | None = None,
        password: str | None = None,
        max_result_rows: int | None = None,
        confirmation_id: str | None = None,
    ) -> dict[str, object]:
        return _tool_result(
            lambda: backend.execute_sql(
                sql,
                params=params,
                connection_string=connection_string,
                host=host,
                port=port,
                database=database,
                username=username,
                password=password,
                max_result_rows=max_result_rows,
                confirmation_id=confirmation_id,
            )
        )

    @server.tool()
    def mysql_plan_migration(migration_file: str) -> dict[str, object]:
        return _tool_result(lambda: backend.plan_migration(migration_file))

    @server.tool()
    def mysql_apply_migration(
        migration_id: str, confirmation_id: str | None = None
    ) -> dict[str, object]:
        return _tool_result(lambda: backend.apply_migration(migration_id, confirmation_id=confirmation_id))

    @server.tool()
    def mysql_schema_snapshot(schema: str, table: str) -> dict[str, object]:
        return _tool_result(lambda: backend.schema_snapshot(schema, table))

    _close_tool_inputs(server)
    return server


def run_stdio_server(server: FastMCP) -> None:
    server.run(transport="stdio")


def run_mysql_mcp(ini_path: Path) -> None:
    """Run the independent MySQL MCP from a user-managed absolute INI file."""

    if not isinstance(ini_path, Path) or not ini_path.is_absolute():
        raise MySqlMcpRuntimeError("invalid_config_path")
    runtime = MySqlMcpRuntime.from_config_file(Path(os.path.abspath(ini_path)))
    run_stdio_server(create_mysql_mcp_server(runtime))


def _tool_result(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
    try:
        result = operation()
        if result.get("status") == "needs_user_confirmation":
            if set(result) != {"status", "confirmation_id", "request_fingerprint", "summary"}:
                raise MySqlMcpRuntimeError("invalid MySQL confirmation result")
            return result
        return {"status": "ok", "data": result}
    except MySqlExecutionError as exc:
        return {
            "status": "error",
            "classification": exc.classification.value,
            "message": exc.message,
        }
    except Exception as exc:
        status, message = _mapped_error(exc)
        return {"status": status, "message": message}


def _mapped_error(exc: Exception) -> tuple[str, str]:
    message = str(exc)
    if isinstance(exc, ConfirmationError):
        return (
            "denied_or_failed",
            "MySQL confirmation is invalid, expired, already used, or no longer matches",
        )
    if "read-only" in message or "not authorized" in message:
        return ("denied", "MySQL operation is outside the configured policy scope")
    if isinstance(exc, (ConfigError, PolicyError)) or message == "invalid_config_or_policy":
        return ("configuration_error", "MySQL MCP configuration or policy is invalid")
    if isinstance(exc, (MySqlReadError, MySqlWriteError, MigrationError, MySqlMcpRuntimeError)):
        return (
            "denied_or_failed",
            "MySQL operation was denied or did not complete with safe evidence",
        )
    return ("denied_or_failed", "MySQL operation was denied or did not complete with safe evidence")


def _confirmation_failure_result() -> dict[str, object]:
    status, message = _mapped_error(ConfirmationError("invalid confirmation"))
    return {"status": status, "message": message}


def _close_tool_inputs(server: FastMCP) -> None:
    """Fail closed if the pinned MCP runtime cannot reject undeclared fields."""

    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None)
    if not isinstance(tools, dict):
        raise MySqlMcpRuntimeError("unsupported_mcp_runtime")
    for tool in tools.values():
        parameters = getattr(tool, "parameters", None)
        metadata = getattr(tool, "fn_metadata", None)
        argument_model = getattr(metadata, "arg_model", None)
        if not isinstance(parameters, dict) or argument_model is None:
            raise MySqlMcpRuntimeError("unsupported_mcp_runtime")
        parameters["additionalProperties"] = False
        model_config = getattr(argument_model, "model_config", None)
        model_rebuild = getattr(argument_model, "model_rebuild", None)
        if not isinstance(model_config, dict) or not callable(model_rebuild):
            raise MySqlMcpRuntimeError("unsupported_mcp_runtime")
        model_config["extra"] = "forbid"
        model_config["hide_input_in_errors"] = True
        model_rebuild(force=True)


def _dml_request(
    target: MySqlTarget,
    action: str,
    request: InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest,
    policy_sha256: str,
) -> dict[str, object]:
    def one(item: InsertRequest | UpdateRequest | DeleteRequest) -> dict[str, object]:
        result: dict[str, object] = {
            "action": "insert" if isinstance(item, InsertRequest) else "update" if isinstance(item, UpdateRequest) else "delete",
            "schema": item.schema,
            "table": item.table,
            "expected_max_rows": item.expected_max_rows,
        }
        if isinstance(item, InsertRequest):
            result["values"] = dict(item.values)
        elif isinstance(item, UpdateRequest):
            result["values"] = dict(item.values)
            result["where"] = dict(item.where)
        else:
            result["where"] = dict(item.where)
        return result
    operations = [one(item) for item in request.operations] if isinstance(request, TransactionRequest) else [one(request)]
    return {
        "target": target.name,
        "environment": target.environment,
        "action": action,
        "policy_sha256": policy_sha256,
        "operations": operations,
    }


def _migration_request(
    target: MySqlTarget,
    migration_id: str,
    source_sha256: str,
    policy_sha256: str,
) -> dict[str, object]:
    return {
        "target": target.name,
        "environment": target.environment,
        "action": "apply_migration",
        "migration_id": migration_id,
        "source_sha256": source_sha256,
        "policy_sha256": policy_sha256,
    }


def _value_digest(value: object) -> str:
    return canonical_request_fingerprint({"value": value})


def _redacted_mapping(values: Mapping[str, object]) -> dict[str, str]:
    return {key: _value_digest(values[key]) for key in sorted(values)}


def _dml_summary(
    target: MySqlTarget,
    action: str,
    request: InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest,
    policy_sha256: str,
) -> OperationSummary:
    def one(item: InsertRequest | UpdateRequest | DeleteRequest) -> dict[str, object]:
        summary: dict[str, object] = {
            "action": "insert" if isinstance(item, InsertRequest) else "update" if isinstance(item, UpdateRequest) else "delete",
            "schema": item.schema, "table": item.table,
            "expected_max_rows": item.expected_max_rows,
        }
        if isinstance(item, InsertRequest):
            summary["values"] = _redacted_mapping(item.values)
        elif isinstance(item, UpdateRequest):
            summary["values"] = _redacted_mapping(item.values)
            summary["where"] = _redacted_mapping(item.where)
        else:
            summary["where"] = _redacted_mapping(item.where)
        return summary
    operations = [one(item) for item in request.operations] if isinstance(request, TransactionRequest) else [one(request)]
    exact = _dml_request(target, action, request, policy_sha256)
    return OperationSummary(
        target=target.name,
        environment=target.environment,
        action=action,
        object_ref="transaction" if isinstance(request, TransactionRequest) else f"{operations[0]['schema']}.{operations[0]['table']}",
        parameters={"operations": operations, "policy_sha256": policy_sha256},
        risk="typed_mysql_write",
        rollback_or_reconcile="transaction rollback before commit; reconcile outcome_unknown",
        request_fingerprint=canonical_request_fingerprint(exact),
    )


def _migration_summary(
    target: MySqlTarget,
    plan: MigrationPlan,
    policy_sha256: str,
) -> OperationSummary:
    exact = _migration_request(
        target,
        plan.migration_id,
        plan.source_sha256,
        policy_sha256,
    )
    return OperationSummary(
        target=target.name,
        environment=target.environment,
        action="apply_migration",
        object_ref=plan.migration_id,
        parameters={
            "migration_id": plan.migration_id,
            "source_sha256": plan.source_sha256,
            "schema_before_sha256": plan.schema_before_sha256,
            "statement_count": len(plan.statements),
            "policy": "current-policy",
        },
        risk=plan.risk,
        rollback_or_reconcile="DDL may implicitly commit; reconcile through the migration ledger",
        request_fingerprint=canonical_request_fingerprint(exact),
    )


def _mapping_input(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise MySqlMcpRuntimeError(f"invalid_{label}")
    return dict(value)


def _list_input(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise MySqlMcpRuntimeError(f"invalid_{label}")
    return list(value)


def _transaction_operation(
    raw: object,
) -> InsertRequest | UpdateRequest | DeleteRequest:
    operation = _mapping_input(raw, "transaction_operation")
    kind = operation.pop("operation", None)
    if kind == "insert" and set(operation) == {
        "schema",
        "table",
        "values",
        "expected_max_rows",
    }:
        return InsertRequest(**operation)  # type: ignore[arg-type]
    if kind == "update" and set(operation) == {
        "schema",
        "table",
        "values",
        "where",
        "expected_max_rows",
    }:
        return UpdateRequest(**operation)  # type: ignore[arg-type]
    if kind == "delete" and set(operation) == {
        "schema",
        "table",
        "where",
        "expected_max_rows",
    }:
        return DeleteRequest(**operation)  # type: ignore[arg-type]
    raise MySqlMcpRuntimeError("invalid_typed_transaction")


__all__ = [
    "MySqlMcpRuntime",
    "MySqlMcpRuntimeError",
    "create_mysql_mcp_server",
    "run_mysql_mcp",
    "run_stdio_server",
]


def _connection_override(
    *,
    connection_string: str | None,
    host: str | None,
    port: int | None,
    database: str | None,
    username: str | None,
    password: str | None,
    max_result_rows: int | None,
) -> MySqlConnectionOverride | None:
    if all(
        value is None
        for value in (
            connection_string,
            host,
            port,
            database,
            username,
            password,
            max_result_rows,
        )
    ):
        return None
    return MySqlConnectionOverride(
        connection_string=connection_string,
        host=host,
        port=port,
        database=database,
        username=username,
        password=password,
        max_result_rows=max_result_rows,
    )


def _raw_execution_request(
    target: MySqlTarget,
    sql: str,
    params: list[object] | tuple[object, ...] | None,
    override: MySqlConnectionOverride | None,
    context_sha256: str,
) -> dict[str, object]:
    effective_username = (
        None
        if override is None and target.username is None
        else (
            override.username
            if override is not None and override.username is not None
            else target.username
        )
    )
    return {
        "target": target.name,
        "environment": target.environment,
        "action": "mysql_execute_sql",
        "sql": sql,
        "params": None if params is None else list(params),
        "connection_string": _credential_digest(
            None if override is None else override.connection_string
        ),
        "host": (
            target.host
            if override is None or override.host is None
            else override.host
        ),
        "port": (
            target.port
            if override is None or override.port is None
            else override.port
        ),
        "database": (
            target.database
            if override is None or override.database is None
            else override.database
        ),
        "username": _credential_digest(effective_username),
        "password": _credential_digest(
            None if override is None else override.password
        ),
        "max_result_rows": (
            target.max_result_rows
            if override is None or override.max_result_rows is None
            else override.max_result_rows
        ),
        "context_sha256": context_sha256,
    }


def _raw_execution_summary(
    target: MySqlTarget,
    sql: str,
    params: list[object] | tuple[object, ...] | None,
    override: MySqlConnectionOverride | None,
    context_sha256: str,
) -> OperationSummary:
    exact = _raw_execution_request(
        target,
        sql,
        params,
        override,
        context_sha256,
    )
    display_host, display_port, display_database = (
        _raw_execution_display_connection(target, override)
    )
    parameters = {
        "sql_digest": _value_digest(sql),
        "params_digest": (
            None
            if params is None
            else [_value_digest(value) for value in params]
        ),
        "host": display_host,
        "port": display_port,
        "database": display_database,
        "max_result_rows": (
            target.max_result_rows
            if override is None or override.max_result_rows is None
            else override.max_result_rows
        ),
    }
    return OperationSummary(
        target=target.name,
        environment=target.environment,
        action="mysql_execute_sql",
        object_ref="raw_mysql_statement",
        parameters=parameters,
        risk="raw_mysql_write",
        rollback_or_reconcile="outcome_unknown → read-only reconcile",
        request_fingerprint=canonical_request_fingerprint(exact),
    )


def _raw_execution_display_connection(
    target: MySqlTarget,
    override: MySqlConnectionOverride | None,
) -> tuple[str, int | None, str | None]:
    if override is not None and override.connection_string is not None:
        try:
            parsed = parse_connection_string(override.connection_string)
            return parsed.host, parsed.port, parsed.database
        except MySqlClientError:
            pass
    host = (
        target.host
        if override is None or override.host is None
        else override.host
    )
    port = (
        target.port
        if override is None or override.port is None
        else override.port
    )
    database = (
        target.database
        if override is None or override.database is None
        else override.database
    )
    return host, port, database


def _credential_digest(value: str | None) -> str | None:
    return None if value is None else _value_digest(value)


def _execution_result_mapping(
    result: MySqlExecutionResult,
) -> dict[str, object]:
    return {
        "statements": [
            _statement_result_mapping(statement)
            for statement in result.statements
        ]
    }


def _statement_result_mapping(
    statement: MySqlStatementResult,
) -> dict[str, object]:
    mapped: dict[str, object] = {
        "index": statement.index,
        "status": statement.status.value,
    }
    if statement.classification is not None:
        mapped["classification"] = statement.classification.value
    if statement.result_sets:
        mapped["result_sets"] = [
            {
                "columns": list(result_set.columns),
                "rows": list(result_set.rows),
                "row_count": result_set.row_count,
                "truncated": result_set.truncated,
            }
            for result_set in statement.result_sets
        ]
    if statement.affected_rows is not None:
        mapped["affected_rows"] = statement.affected_rows
    if statement.error is not None:
        mapped["error"] = statement.error
    return mapped
