"""Typed, policy-bounded MySQL DML with one-shot preflight evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import re
import time
from types import MappingProxyType
import weakref

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


class MySqlWriteError(RuntimeError):
    """A sanitized write failure that never includes values or SQL text."""


_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_WriteRequest = "InsertRequest | UpdateRequest | DeleteRequest"
_PREFLIGHT_TTL_SECONDS = 300.0
_MAX_OUTSTANDING_PREFLIGHTS = 128


@dataclass(frozen=True)
class InsertRequest:
    """One fixed-shape INSERT; values are bound parameters, never SQL text."""

    schema: str
    table: str
    values: Mapping[str, object] = field(repr=False)
    expected_max_rows: int = 1

    def __post_init__(self) -> None:
        _validate_identifier(self.schema, "schema")
        _validate_identifier(self.table, "table")
        object.__setattr__(self, "values", _snapshot_values(self.values, "values"))
        _validate_expected_max_rows(self.expected_max_rows)


@dataclass(frozen=True)
class UpdateRequest:
    """One fixed-shape UPDATE with equality-only, parameterized predicates."""

    schema: str
    table: str
    values: Mapping[str, object] = field(repr=False)
    where: Mapping[str, object] = field(repr=False)
    expected_max_rows: int = 1

    def __post_init__(self) -> None:
        _validate_identifier(self.schema, "schema")
        _validate_identifier(self.table, "table")
        object.__setattr__(self, "values", _snapshot_values(self.values, "values"))
        object.__setattr__(self, "where", _snapshot_values(self.where, "where"))
        _validate_expected_max_rows(self.expected_max_rows)


@dataclass(frozen=True)
class DeleteRequest:
    """One fixed-shape DELETE with equality-only, parameterized predicates."""

    schema: str
    table: str
    where: Mapping[str, object] = field(repr=False)
    expected_max_rows: int = 1

    def __post_init__(self) -> None:
        _validate_identifier(self.schema, "schema")
        _validate_identifier(self.table, "table")
        object.__setattr__(self, "where", _snapshot_values(self.where, "where"))
        _validate_expected_max_rows(self.expected_max_rows)


@dataclass(frozen=True)
class TransactionRequest:
    """An all-or-nothing list of typed DML requests; raw SQL is not accepted."""

    operations: tuple[InsertRequest | UpdateRequest | DeleteRequest, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.operations, tuple) or not self.operations:
            raise TypeError("transaction operations must be a nonempty tuple")
        if not all(isinstance(operation, _request_types()) for operation in self.operations):
            raise TypeError("transaction operations must be typed DML requests")


@dataclass(frozen=True)
class WritePreflight:
    """An opaque, one-shot execution capability issued by one write service."""

    _request: InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest = field(
        repr=False
    )


@dataclass(frozen=True)
class _PreparedWrite:
    request: InsertRequest | UpdateRequest | DeleteRequest
    action: str
    maximum_rows: int
    rule: MySqlPolicyRule | None = None
    decision: MySqlPolicyDecision | None = None


class MySqlWriteService:
    """Execute only policy-approved typed DML inside one bounded transaction."""

    def __init__(
        self,
        *,
        target: MySqlTarget,
        credentials: MySqlCredentials,
        policy: MySqlPolicy | None,
        policy_service: PolicyService | None,
        connection_factory: Callable[
            [MySqlTarget, MySqlCredentials], AbstractContextManager[object]
        ] = read_connection,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(target, MySqlTarget) or not isinstance(
            credentials, MySqlCredentials
        ):
            raise TypeError("target, credentials, and policy must be validated MySQL configuration")
        if (policy is None) != (policy_service is None):
            raise TypeError("policy and policy_service must be provided together")
        if policy is not None:
            if not isinstance(policy, MySqlPolicy) or not isinstance(
                policy_service, PolicyService
            ):
                raise TypeError("policy_service must be a PolicyService")
            # A decision must be derived from the exact policy whose rule is later
            # used for row caps and identifiers; matching rule names are not enough.
            if (
                getattr(policy_service, "_policy", None) is not policy
                or getattr(policy_service, "_target", None) is not target
            ):
                raise TypeError(
                    "policy_service must bind the same trusted policy and target"
                )
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._target = target
        self._credentials = credentials
        self._policy = policy
        self._policy_service = policy_service
        self._connection_factory = connection_factory
        self._clock = clock or time.monotonic
        self._issued_preflights: dict[
            int, tuple[weakref.ReferenceType[WritePreflight], float]
        ] = {}

    def preview(
        self,
        request: InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest,
    ) -> WritePreflight:
        """Prevalidate a complete typed write set without opening a connection."""

        self._reject_read_only_target()
        self._prepare_request_set(request)
        now = self._now()
        self._purge_expired_preflights(now)
        if len(self._issued_preflights) >= _MAX_OUTSTANDING_PREFLIGHTS:
            raise MySqlWriteError("too many outstanding write preflights")
        evidence = WritePreflight(request)
        self._issued_preflights[id(evidence)] = (weakref.ref(evidence), now)
        return evidence

    def recheck(
        self,
        request: InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest,
    ) -> None:
        """Re-authorize one replay without issuing new execution evidence."""

        self._reject_read_only_target()
        self._prepare_request_set(request)

    def execute(self, preflight: WritePreflight) -> dict[str, object]:
        """Consume one service-issued preflight and perform exactly one transaction."""

        self._reject_read_only_target()
        if not isinstance(preflight, WritePreflight):
            raise MySqlWriteError("trusted write preflight evidence is required")
        issued = self._issued_preflights.pop(id(preflight), None)
        if issued is None or issued[0]() is not preflight:
            raise MySqlWriteError("trusted write preflight evidence is required")
        if self._now() - issued[1] > _PREFLIGHT_TTL_SECONDS:
            raise MySqlWriteError("trusted write preflight evidence has expired")

        prepared = self._prepare_request_set(preflight._request)
        return self._run_transaction(prepared)

    def execute_request(
        self,
        request: InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest,
    ) -> dict[str, object]:
        """Execute one typed write set directly under account or policy scope."""

        self._reject_read_only_target()
        prepared = self._prepare_request_set(request)
        return self._run_transaction(prepared)

    def authorize(
        self,
        request: InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest,
    ) -> PolicyOutcome:
        """Return the aggregated policy outcome; DENY raises before any client."""

        self._reject_read_only_target()
        prepared = self._prepare_request_set(request)
        if any(
            item.decision is not None
            and item.decision.outcome is PolicyOutcome.NEEDS_USER_CONFIRMATION
            for item in prepared
        ):
            return PolicyOutcome.NEEDS_USER_CONFIRMATION
        return PolicyOutcome.ALLOW

    def _run_transaction(
        self,
        prepared: tuple[_PreparedWrite, ...],
    ) -> dict[str, object]:
        commit_attempted = False
        try:
            with self._connection_factory(self._target, self._credentials) as connection:
                try:
                    results = tuple(
                        self._execute_one(connection, operation) for operation in prepared
                    )
                except _OutcomeUnknown:
                    _rollback(connection)
                    return _unknown_outcome()
                except MySqlWriteError:
                    _rollback(connection)
                    raise
                except Exception as exc:
                    _rollback(connection)
                    if _is_connection_interruption(exc):
                        return _unknown_outcome()
                    raise MySqlWriteError("MySQL write execution failed") from None

                try:
                    commit_attempted = True
                    connection.commit()  # type: ignore[attr-defined]
                except Exception:
                    # A server commit may have happened while the response was lost.
                    _rollback(connection)
                    return _unknown_outcome()
        except MySqlWriteError:
            if commit_attempted:
                return _unknown_outcome()
            raise
        except Exception:
            if commit_attempted:
                return _unknown_outcome()
            raise MySqlWriteError("could not establish MySQL write transaction") from None

        return {
            "outcome": "committed",
            "row_count": sum(result[0] for result in results),
            "readback": [row for _, rows in results for row in rows],
        }

    def _prepare_request_set(
        self,
        request: InsertRequest | UpdateRequest | DeleteRequest | TransactionRequest | object,
    ) -> tuple[_PreparedWrite, ...]:
        if isinstance(request, TransactionRequest):
            requests = request.operations
        elif isinstance(request, _request_types()):
            requests = (request,)
        else:
            raise MySqlWriteError("writes require typed requests, not raw DML SQL")
        return tuple(self._prepare_one(item) for item in requests)

    def _prepare_one(
        self,
        request: InsertRequest | UpdateRequest | DeleteRequest,
    ) -> _PreparedWrite:
        action = _action_for(request)
        if self._policy is None:
            return _PreparedWrite(
                request=request,
                action=action,
                maximum_rows=request.expected_max_rows,
            )
        values = _write_values(request)
        where = _where_values(request)
        operation = MySqlOperation(
            target=self._target.name,
            environment=self._target.environment,
            action=action,
            schemas=frozenset({request.schema}),
            tables=frozenset({request.table}),
            columns=frozenset((*values, *where)),
            estimated_rows=request.expected_max_rows,
            migration_id=None,
            where_columns=frozenset(where),
        )
        if self._expected_rows_exceed_all_compatible_rule_caps(operation):
            raise MySqlWriteError("write expected maximum exceeds policy")
        decision = self._policy_service.authorize(operation)
        if decision.outcome not in {
            PolicyOutcome.ALLOW,
            PolicyOutcome.NEEDS_USER_CONFIRMATION,
        } or decision.rule_name is None:
            if action in {"update", "delete"}:
                raise MySqlWriteError(
                    "write is not authorized by policy: primary key or WHERE protection may be missing"
                )
            raise MySqlWriteError(
                "write is not authorized by policy or expected maximum exceeds policy"
            )
        rule = next(
            (candidate for candidate in self._policy.rules if candidate.name == decision.rule_name),
            None,
        )
        if rule is None or rule.max_dml_rows is None:
            raise MySqlWriteError("MySQL write policy decision is inconsistent")
        if request.expected_max_rows > rule.max_dml_rows:
            raise MySqlWriteError("write expected maximum exceeds policy")
        return _PreparedWrite(
            request=request,
            rule=rule,
            decision=decision,
            action=action,
            maximum_rows=min(request.expected_max_rows, rule.max_dml_rows),
        )

    def _expected_rows_exceed_all_compatible_rule_caps(
        self,
        operation: MySqlOperation,
    ) -> bool:
        compatible_caps = [
            rule.max_dml_rows
            for rule in self._policy.rules
            if (
                operation.target in rule.targets
                and operation.environment in rule.environments
                and operation.action in rule.actions
                and operation.schemas == rule.schemas
                and operation.tables == rule.tables
                and operation.columns <= rule.columns
                and operation.where_columns <= rule.columns
                and rule.max_dml_rows is not None
            )
        ]
        return bool(compatible_caps) and operation.estimated_rows is not None and (
            operation.estimated_rows > max(compatible_caps)
        )

    def _execute_one(
        self,
        connection: object,
        prepared: _PreparedWrite,
    ) -> tuple[int, list[dict[str, object]]]:
        request = prepared.request
        lock_where = _lock_predicate_values(request, prepared.rule)
        count_sql, count_parameters = _count_for_update_sql(request, lock_where)
        cursor = connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute(count_sql, count_parameters)
            locked_count = _count_from_rows(cursor.fetchall())
            if locked_count > prepared.maximum_rows:
                raise MySqlWriteError("locked target count exceeds expected maximum")

            dml_sql, dml_parameters = _dml_sql(request)
            cursor.execute(dml_sql, dml_parameters)
            row_count = _cursor_row_count(cursor)
            if row_count > prepared.maximum_rows:
                raise MySqlWriteError("affected-row count exceeds expected maximum")

            readback_columns = _readback_columns(request)
            readback_sql, readback_parameters = _readback_sql(
                request,
                lock_where,
                readback_columns,
            )
            cursor.execute(readback_sql, readback_parameters)
            readback = _controlled_rows(
                cursor.fetchall(),
                readback_columns,
                maximum_rows=prepared.maximum_rows,
            )
            if row_count and prepared.action != "delete" and not readback:
                raise MySqlWriteError("controlled readback did not verify the write")
            return row_count, readback
        except MySqlWriteError:
            raise
        except Exception as exc:
            if _is_connection_interruption(exc):
                raise _OutcomeUnknown from None
            raise
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    def _reject_read_only_target(self) -> None:
        # This guard must remain before policy, confirmation, client, SQL, or evidence work.
        if self._target.is_read_only:
            raise MySqlWriteError("configured read-only environment rejects write operations")

    def _now(self) -> float:
        value = self._clock()
        if not isinstance(value, (float, int)) or isinstance(value, bool):
            raise MySqlWriteError("write preflight clock is invalid")
        return float(value)

    def _purge_expired_preflights(self, now: float) -> None:
        expired = [
            key
            for key, (evidence, issued_at) in self._issued_preflights.items()
            if evidence() is None or now - issued_at > _PREFLIGHT_TTL_SECONDS
        ]
        for key in expired:
            del self._issued_preflights[key]


class _OutcomeUnknown(Exception):
    """A connection interruption makes server-side commit state unknowable."""


def _request_types() -> tuple[type[InsertRequest], type[UpdateRequest], type[DeleteRequest]]:
    return InsertRequest, UpdateRequest, DeleteRequest


def _validate_identifier(value: object, label: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} must be a conservative SQL identifier")


def _snapshot_values(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise TypeError(f"{label} must be a nonempty mapping")
    copied = dict(value)
    for key in copied:
        _validate_identifier(key, label)
    return MappingProxyType(copied)


def _validate_expected_max_rows(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("expected_max_rows must be a positive integer")


def _action_for(request: InsertRequest | UpdateRequest | DeleteRequest) -> str:
    if isinstance(request, InsertRequest):
        return "insert"
    if isinstance(request, UpdateRequest):
        return "update"
    return "delete"


def _write_values(request: InsertRequest | UpdateRequest | DeleteRequest) -> Mapping[str, object]:
    if isinstance(request, DeleteRequest):
        return MappingProxyType({})
    return request.values


def _where_values(request: InsertRequest | UpdateRequest | DeleteRequest) -> Mapping[str, object]:
    if isinstance(request, InsertRequest):
        return MappingProxyType({})
    return request.where


def _lock_predicate_values(
    request: InsertRequest | UpdateRequest | DeleteRequest,
    rule: MySqlPolicyRule | None,
) -> Mapping[str, object]:
    if not isinstance(request, InsertRequest):
        return request.where
    if (
        rule is not None
        and
        rule.primary_key_columns is not None
        and rule.primary_key_columns <= request.values.keys()
    ):
        return MappingProxyType(
            {column: request.values[column] for column in rule.primary_key_columns}
        )
    return request.values


def _count_for_update_sql(
    request: InsertRequest | UpdateRequest | DeleteRequest,
    where: Mapping[str, object],
) -> tuple[str, tuple[object, ...]]:
    predicate, parameters = _predicate(where)
    return (
        f"SELECT COUNT(*) AS count FROM {_table_name(request)} WHERE {predicate} FOR UPDATE",
        parameters,
    )


def _dml_sql(
    request: InsertRequest | UpdateRequest | DeleteRequest,
) -> tuple[str, tuple[object, ...]]:
    if isinstance(request, InsertRequest):
        values = _ordered_items(request.values)
        columns = ", ".join(_quoted_identifier(key) for key, _ in values)
        markers = ", ".join("%s" for _ in values)
        return (
            f"INSERT INTO {_table_name(request)} ({columns}) VALUES ({markers})",
            tuple(value for _, value in values),
        )
    predicate, where_parameters = _predicate(request.where)
    if isinstance(request, UpdateRequest):
        values = _ordered_items(request.values)
        assignments = ", ".join(f"{_quoted_identifier(key)} = %s" for key, _ in values)
        return (
            f"UPDATE {_table_name(request)} SET {assignments} WHERE {predicate}",
            tuple(value for _, value in values) + where_parameters,
        )
    return f"DELETE FROM {_table_name(request)} WHERE {predicate}", where_parameters


def _readback_columns(
    request: InsertRequest | UpdateRequest | DeleteRequest,
) -> tuple[str, ...]:
    if isinstance(request, InsertRequest):
        return tuple(key for key, _ in _ordered_items(request.values))
    if isinstance(request, UpdateRequest):
        return tuple(sorted(set(request.values) | set(request.where)))
    return tuple(key for key, _ in _ordered_items(request.where))


def _readback_sql(
    request: InsertRequest | UpdateRequest | DeleteRequest,
    where: Mapping[str, object],
    columns: tuple[str, ...],
) -> tuple[str, tuple[object, ...]]:
    selected_columns = ", ".join(_quoted_identifier(column) for column in columns)
    predicate, parameters = _predicate(where)
    return (
        f"SELECT {selected_columns} FROM {_table_name(request)} WHERE {predicate}",
        parameters,
    )


def _table_name(request: InsertRequest | UpdateRequest | DeleteRequest) -> str:
    return f"{_quoted_identifier(request.schema)}.{_quoted_identifier(request.table)}"


def _quoted_identifier(value: str) -> str:
    _validate_identifier(value, "identifier")
    return f"`{value}`"


def _predicate(where: Mapping[str, object]) -> tuple[str, tuple[object, ...]]:
    items = _ordered_items(where)
    if not items:
        raise MySqlWriteError("update and delete require a WHERE predicate")
    return (
        " AND ".join(f"{_quoted_identifier(key)} = %s" for key, _ in items),
        tuple(value for _, value in items),
    )


def _ordered_items(values: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
    return tuple((key, values[key]) for key in sorted(values))


def _count_from_rows(rows: object) -> int:
    if (
        not isinstance(rows, (list, tuple))
        or len(rows) != 1
        or not isinstance(rows[0], Mapping)
    ):
        raise MySqlWriteError("MySQL write count result has an invalid shape")
    count = rows[0].get("count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise MySqlWriteError("MySQL write count result has an invalid shape")
    return count


def _cursor_row_count(cursor: object) -> int:
    row_count = getattr(cursor, "rowcount", None)
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
        raise MySqlWriteError("MySQL affected-row count is invalid")
    return row_count


def _controlled_rows(
    rows: object,
    columns: tuple[str, ...],
    *,
    maximum_rows: int,
) -> list[dict[str, object]]:
    if not isinstance(rows, (list, tuple)) or not all(
        isinstance(row, Mapping) for row in rows
    ):
        raise MySqlWriteError("MySQL write readback has an invalid shape")
    if len(rows) > maximum_rows:
        raise MySqlWriteError("MySQL write readback exceeds the expected maximum")
    allowed = frozenset(columns)
    return [{key: value for key, value in row.items() if key in allowed} for row in rows]


def _rollback(connection: object) -> None:
    try:
        connection.rollback()  # type: ignore[attr-defined]
    except Exception:
        pass


def _is_connection_interruption(exc: Exception) -> bool:
    return isinstance(exc, (ConnectionError, OSError, TimeoutError))


def _unknown_outcome() -> dict[str, object]:
    return {"outcome": "outcome_unknown", "row_count": None, "readback": []}
