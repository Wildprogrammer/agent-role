"""Policy-bounded MySQL metadata and data read operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
import re

from .client import read_connection
from .models import (
    MySqlCredentials,
    MySqlOperation,
    MySqlPolicy,
    MySqlPolicyDecision,
    MySqlPolicyRule,
    MySqlTarget,
    PolicyOutcome,
)
from .policy import PolicyService
from .sql_guard import QueryAnalysis, SqlGuardError, analyze_read_query


class MySqlReadError(RuntimeError):
    """A sanitized read failure that never includes SQL values or row contents."""


_DEFAULT_RETURN_LIMIT = 100
_LIMIT_KEYWORD = re.compile(r"\bLIMIT\b", re.IGNORECASE)
_COMMENT_TOKEN = re.compile(r"--|#|/\*")
_EXPLAIN_RESULT_COLUMNS = (
    "id",
    "select_type",
    "table",
    "type",
    "possible_keys",
    "key",
    "key_len",
    "ref",
    "rows",
    "filtered",
)


@dataclass(frozen=True)
class _AuthorizedScope:
    rule: MySqlPolicyRule
    decision: MySqlPolicyDecision


class MySqlReadService:
    """Expose the fixed MySQL read surface after SQL and policy authorization."""

    def __init__(
        self,
        *,
        target: MySqlTarget,
        credentials: MySqlCredentials,
        policy: MySqlPolicy | None,
        policy_service: PolicyService | None,
        connection_factory: Callable[[MySqlTarget, MySqlCredentials], AbstractContextManager[object]] = read_connection,
    ) -> None:
        if not isinstance(target, MySqlTarget) or not isinstance(
            credentials, MySqlCredentials
        ):
            raise TypeError("target, credentials, and policy must be validated MySQL configuration")
        if (policy is None) != (policy_service is None):
            raise TypeError("policy and policy_service must be provided together")
        if policy is not None and (
            not isinstance(policy, MySqlPolicy)
            or not isinstance(policy_service, PolicyService)
        ):
            raise TypeError("policy_service must be a PolicyService")
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._target = target
        self._credentials = credentials
        self._policy = policy
        self._policy_service = policy_service
        self._connection_factory = connection_factory

    def list_schemas(self) -> dict[str, list[str]]:
        """List schemas visible to the DB account, or policy-scoped schemas."""

        if self._policy is None:
            rows = self._fetch_all(
                "SELECT SCHEMA_NAME AS name FROM information_schema.SCHEMATA "
                "ORDER BY SCHEMA_NAME",
                (),
            )
            return {
                "schemas": [
                    row["name"] for row in rows if isinstance(row.get("name"), str)
                ]
            }
        schemas: list[str] = []
        for scope in self._metadata_scopes():
            schema = next(iter(scope.rule.schemas))
            rows = self._fetch_all(
                "SELECT SCHEMA_NAME AS name FROM information_schema.SCHEMATA "
                "WHERE SCHEMA_NAME = %s ORDER BY SCHEMA_NAME",
                (schema,),
            )
            if any(row.get("name") == schema for row in rows) and schema not in schemas:
                schemas.append(schema)
        return {"schemas": schemas}

    def list_tables(self, schema: str) -> dict[str, list[str]]:
        """List base tables visible to the DB account or covered by policy."""

        if self._policy is None:
            rows = self._fetch_all(
                "SELECT TABLE_NAME AS name FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = %s ORDER BY TABLE_NAME",
                (schema, "BASE TABLE"),
            )
            return {
                "tables": [
                    row["name"] for row in rows if isinstance(row.get("name"), str)
                ]
            }
        tables: list[str] = []
        for scope in self._metadata_scopes(schema=schema):
            table = next(iter(scope.rule.tables))
            rows = self._fetch_all(
                "SELECT TABLE_NAME AS name FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND TABLE_TYPE = %s "
                "ORDER BY TABLE_NAME",
                (schema, table, "BASE TABLE"),
            )
            if any(row.get("name") == table for row in rows) and table not in tables:
                tables.append(table)
        return {"tables": tables}

    def describe_table(self, schema: str, table: str) -> dict[str, list[dict[str, object]]]:
        """Describe columns visible to the DB account or covered by policy."""

        if self._policy is None:
            rows = self._fetch_all(
                "SELECT COLUMN_NAME AS name, DATA_TYPE AS data_type, "
                "IS_NULLABLE AS is_nullable, COLUMN_KEY AS column_key "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s "
                "AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                (schema, table),
            )
            return {
                "columns": [
                    {
                        "name": row["name"],
                        "data_type": row.get("data_type"),
                        "is_nullable": row.get("is_nullable"),
                        "column_key": row.get("column_key"),
                    }
                    for row in rows
                    if isinstance(row.get("name"), str)
                ]
            }
        columns: list[dict[str, object]] = []
        for scope in self._metadata_scopes(schema=schema, table=table):
            for column in sorted(scope.rule.columns):
                rows = self._fetch_all(
                    "SELECT COLUMN_NAME AS name, DATA_TYPE AS data_type, "
                    "IS_NULLABLE AS is_nullable, COLUMN_KEY AS column_key "
                    "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = %s "
                    "AND TABLE_NAME = %s AND COLUMN_NAME = %s "
                    "ORDER BY ORDINAL_POSITION",
                    (schema, table, column),
                )
                for row in rows:
                    if row.get("name") == column:
                        columns.append(
                            {
                                "name": column,
                                "data_type": row.get("data_type"),
                                "is_nullable": row.get("is_nullable"),
                                "column_key": row.get("column_key"),
                            }
                        )
        return {"columns": columns}

    def read_query(
        self,
        sql: str,
        *,
        parameters: Sequence[object] = (),
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, object]:
        """Run one SQL-guarded SELECT with parameter-only values and a hard limit."""

        analysis = self._select_analysis(sql, parameters)
        if _COMMENT_TOKEN.search(sql):
            raise MySqlReadError("read queries must not contain SQL comments")
        if _LIMIT_KEYWORD.search(sql):
            raise MySqlReadError("read queries must use the service LIMIT parameter")
        if self._policy is None:
            maximum = self._target.max_result_rows
        else:
            scope = self._authorize_query(analysis, action="read", estimated_rows=0)
            maximum = _rule_max_rows(scope.rule)
        requested_limit = _validated_limit(limit, default=_DEFAULT_RETURN_LIMIT)
        safe_offset = _validated_offset(offset)
        effective_limit = min(requested_limit, maximum)
        if self._policy is not None:
            self._authorize_query(
                analysis,
                action="read",
                estimated_rows=effective_limit,
            )
        query = f"{_without_trailing_semicolon(sql)} LIMIT %s OFFSET %s"
        rows = self._fetch_all(query, tuple(parameters) + (effective_limit, safe_offset))
        if self._policy is None:
            safe_rows = rows
        else:
            safe_rows = _redact_unlisted_fields(rows, frozenset(analysis.columns))
        visible_rows = safe_rows[:effective_limit]
        return {
            "columns": sorted(analysis.columns),
            "rows": visible_rows,
            "row_count": len(visible_rows),
            "truncated": len(safe_rows) >= effective_limit,
        }

    def explain_query(
        self,
        sql: str,
        *,
        parameters: Sequence[object] = (),
    ) -> dict[str, object]:
        """Return a fixed, value-safe EXPLAIN projection for one guarded SELECT."""

        analysis = self._select_analysis(sql, parameters)
        if self._policy is None:
            maximum = self._target.max_result_rows
        else:
            scope = self._authorize_query(
                analysis, action="explain", estimated_rows=1
            )
            maximum = _rule_max_rows(scope.rule)
        rows = self._fetch_all(f"EXPLAIN {_without_trailing_semicolon(sql)}", tuple(parameters))
        visible_rows = [
            {column: row[column] for column in _EXPLAIN_RESULT_COLUMNS if column in row}
            for row in rows[:maximum]
        ]
        returned_columns = [
            column
            for column in _EXPLAIN_RESULT_COLUMNS
            if any(column in row for row in visible_rows)
        ]
        return {
            "columns": returned_columns,
            "rows": visible_rows,
            "row_count": len(visible_rows),
            "truncated": len(rows) > maximum,
        }

    def _metadata_scopes(
        self,
        *,
        schema: str | None = None,
        table: str | None = None,
    ) -> tuple[_AuthorizedScope, ...]:
        scopes: list[_AuthorizedScope] = []
        for rule in self._policy.rules:
            rule_schema = next(iter(rule.schemas))
            rule_table = next(iter(rule.tables))
            if (
                "metadata" not in rule.actions
                or self._target.name not in rule.targets
                or self._target.environment not in rule.environments
                or (schema is not None and schema != rule_schema)
                or (table is not None and table != rule_table)
            ):
                continue
            operation = MySqlOperation(
                target=self._target.name,
                environment=self._target.environment,
                action="metadata",
                schemas=rule.schemas,
                tables=rule.tables,
                columns=rule.columns,
                estimated_rows=1,
                migration_id=None,
            )
            decision = self._policy_service.authorize(operation)
            if decision.outcome is PolicyOutcome.ALLOW:
                scopes.append(_AuthorizedScope(rule, decision))
        return tuple(scopes)

    def _select_analysis(
        self,
        sql: str,
        parameters: Sequence[object],
    ) -> QueryAnalysis:
        try:
            analysis = analyze_read_query(sql, parameters=parameters)
        except SqlGuardError:
            raise MySqlReadError("SQL is not an allowed parameterized read query") from None
        if analysis.statement_type != "select":
            raise MySqlReadError("this read operation requires one SELECT statement")
        return analysis

    def _authorize_query(
        self,
        analysis: QueryAnalysis,
        *,
        action: str,
        estimated_rows: int,
    ) -> _AuthorizedScope:
        schemas = analysis.schemas or frozenset({self._target.database})
        if len(schemas) != 1 or len(analysis.tables) != 1 or not analysis.columns:
            raise MySqlReadError("read query must have one explicit policy scope")
        operation = MySqlOperation(
            target=self._target.name,
            environment=self._target.environment,
            action=action,
            schemas=schemas,
            tables=analysis.tables,
            columns=analysis.columns,
            estimated_rows=estimated_rows,
            migration_id=None,
        )
        decision = self._policy_service.authorize(operation)
        if decision.outcome is not PolicyOutcome.ALLOW or decision.rule_name is None:
            raise MySqlReadError("MySQL read operation is not authorized by policy")
        rule = next((rule for rule in self._policy.rules if rule.name == decision.rule_name), None)
        if rule is None:
            raise MySqlReadError("MySQL read policy decision is inconsistent")
        return _AuthorizedScope(rule, decision)

    def _fetch_all(
        self,
        sql: str,
        parameters: tuple[object, ...],
    ) -> list[dict[str, object]]:
        try:
            with self._connection_factory(self._target, self._credentials) as connection:
                cursor = connection.cursor()  # type: ignore[attr-defined]
                try:
                    cursor.execute(sql, parameters)
                    rows = cursor.fetchall()
                finally:
                    close = getattr(cursor, "close", None)
                    if callable(close):
                        close()
        except MySqlReadError:
            raise
        except Exception:
            raise MySqlReadError("MySQL read execution failed") from None
        if not isinstance(rows, (list, tuple)) or not all(
            isinstance(row, Mapping) for row in rows
        ):
            raise MySqlReadError("MySQL read result has an invalid shape")
        return [dict(row) for row in rows]


def _rule_max_rows(rule: MySqlPolicyRule) -> int:
    if rule.max_return_rows is None:
        raise MySqlReadError("MySQL read policy is incomplete")
    return rule.max_return_rows


def _validated_limit(value: int | None, *, default: int) -> int:
    limit = default if value is None else value
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise MySqlReadError("read limit must be a positive integer")
    return limit


def _validated_offset(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MySqlReadError("read offset must be a nonnegative integer")
    return value


def _without_trailing_semicolon(sql: str) -> str:
    trimmed = sql.strip()
    return trimmed[:-1].rstrip() if trimmed.endswith(";") else trimmed


def _redact_unlisted_fields(
    rows: list[dict[str, object]],
    allowed_columns: frozenset[str],
) -> list[dict[str, object]]:
    """Omit fields outside the SQL-and-policy explicit permitted field set."""

    return [
        {key: value for key, value in row.items() if key in allowed_columns}
        for row in rows
    ]
