"""Guarded MySQL DDL migration planning.

Migration SQL is never accepted directly from a caller.  A caller can name only a
registered file in the configured migrations directory; this module reads it with
the same guarded-file rules as configuration, parses it as MySQL, and keeps only
policy-approved DDL facts for later application.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import weakref

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .client import read_connection
from .config import _UnsafePathError, _guarded_regular_file
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


class MigrationError(RuntimeError):
    """A sanitized migration planning or application failure."""


@dataclass(frozen=True)
class MigrationPlan:
    """Immutable, value-free evidence for one guarded migration source."""

    migration_id: str
    source_sha256: str
    schema_before_sha256: str
    statements: tuple[str, ...]
    risk: str
    requires_confirmation: bool
    fingerprint: str


@dataclass(frozen=True)
class _DdlStatement:
    category: str
    schema: str
    table: str
    columns: frozenset[str]
    statement: str


_MIGRATION_FILE = re.compile(r"^(?P<number>[0-9]{4})_(?P<description>[a-z][a-z0-9_]*)\.sql$")
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_SCHEMA_SNAPSHOT_SQL = (
    "SELECT TABLE_NAME AS table_name, COLUMN_NAME AS column_name, "
    "COLUMN_TYPE AS column_type, IS_NULLABLE AS is_nullable, "
    "COLUMN_KEY AS column_key FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
    "ORDER BY TABLE_NAME, ORDINAL_POSITION"
)


class MySqlMigrationService:
    """Create non-executing plans from registered, policy-scoped DDL files."""

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
            if (
                getattr(policy_service, "_policy", None) is not policy
                or getattr(policy_service, "_target", None) is not target
            ):
                raise TypeError(
                    "policy_service must bind the same trusted policy and target"
                )
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        self._target = target
        self._credentials = credentials
        self._policy = policy
        self._policy_service = policy_service
        self._connection_factory = connection_factory
        self._issued_plans: dict[int, weakref.ReferenceType[MigrationPlan]] = {}
        self._outcome_unknown_migration_ids: set[str] = set()

    def plan_migration(self, migration_file: str) -> MigrationPlan:
        """Read and plan one registered migration without issuing any write SQL.

        Planning is intentionally available on read-only targets.  It evaluates the
        exact same policy scope against a read-only clone of the trusted target, but
        never treats that evaluation as authorization to apply the plan.
        """

        migration_id, numeric_id, source = self._migration_source(migration_file)
        self._reject_duplicate_numeric_id(numeric_id, source)
        source_bytes = _read_source(source)
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        statements = _parse_ddl(source_bytes, target=self._target)
        self._require_one_scope(statements)

        if self._policy is None:
            ddl_decisions: tuple[MySqlPolicyDecision, ...] = ()
            migration_decision: MySqlPolicyDecision | None = None
        else:
            ddl_decisions = tuple(
                self._authorize_ddl(statement) for statement in statements
            )
            migration_decision = self._authorize_migration(migration_id, statements)
        schema_before_sha256 = self._schema_before_sha256(statements)
        decisions = (
            (*ddl_decisions, migration_decision)
            if migration_decision is not None
            else ddl_decisions
        )
        plan = MigrationPlan(
            migration_id=migration_id,
            source_sha256=source_sha256,
            schema_before_sha256=schema_before_sha256,
            statements=tuple(statement.statement for statement in statements),
            risk="structural_change_no_rollback",
            requires_confirmation=self._target.require_confirmation
            or any(
                decision.outcome is PolicyOutcome.NEEDS_USER_CONFIRMATION
                for decision in decisions
            ),
            fingerprint=_plan_fingerprint(
                migration_id=migration_id,
                source_sha256=source_sha256,
                schema_before_sha256=schema_before_sha256,
                statements=statements,
                decisions=decisions,
            ),
        )
        self._issued_plans[id(plan)] = weakref.ref(plan)
        return plan

    def source_digest(self, migration_id: str) -> str:
        """Return the guarded source SHA-256 for one registered migration file.

        The MCP replay path uses this to recompute a confirmation request without
        opening a database connection; schema drift is rechecked under the write
        lock before any DDL is sent.
        """

        migration_file = f"{_validate_migration_id(migration_id)}.sql"
        _, numeric_id, source = self._migration_source(migration_file)
        self._reject_duplicate_numeric_id(numeric_id, source)
        return hashlib.sha256(_read_source(source)).hexdigest()

    def apply_migration(self, plan: MigrationPlan) -> dict[str, str]:
        """Apply exactly one service-issued plan through the policy-bound ledger.

        MySQL DDL may implicitly commit.  Once the first migration DDL is sent, a
        driver or network failure is therefore not retried and is reported as an
        unknown outcome for callers to reconcile through the read-only ledger API.
        """

        # This guard intentionally precedes plan evidence, policy, confirmation,
        # source revalidation, client construction, and every SQL statement.
        self._reject_read_only_target()
        self._consume_issued_plan(plan)
        self._reject_outcome_unknown_retry(plan)
        statements = self._revalidate_plan_source(plan)
        if self._policy is not None:
            tuple(self._authorize_ddl(statement) for statement in statements)
            self._authorize_migration(plan.migration_id, statements)
        try:
            outcome = self._apply_with_ledger(plan, statements)
            if outcome["outcome"] == "outcome_unknown":
                self._outcome_unknown_migration_ids.add(plan.migration_id)
            return outcome
        except _LedgerUnavailable:
            raise MigrationError("migration ledger is unavailable; plan only") from None
        except MigrationError:
            raise
        except Exception:
            # _apply_with_ledger converts any error after migration DDL starts to an
            # unknown outcome.  Remaining errors occurred before a migration DDL
            # statement and are safe to report as a sanitized setup failure.
            raise MigrationError("could not establish guarded migration application") from None

    def reconcile_migration(self, migration_id: str) -> dict[str, str]:
        """Read the fixed ledger only; this method never creates or changes it."""

        _validate_migration_id(migration_id)
        ledger = self._ledger_name()
        try:
            with self._connection_factory(self._target, self._credentials) as connection:
                cursor = connection.cursor()  # type: ignore[attr-defined]
                try:
                    cursor.execute(
                        f"SELECT source_sha256 FROM {ledger} WHERE migration_id = %s",
                        (migration_id,),
                    )
                    source_sha256 = _ledger_source_sha256(cursor.fetchall())
                finally:
                    close = getattr(cursor, "close", None)
                    if callable(close):
                        close()
        except MigrationError:
            raise
        except Exception:
            raise MigrationError("could not read migration ledger") from None
        if source_sha256 is None:
            return {"outcome": "not_applied", "migration_id": migration_id}
        # A ledger entry with a valid source hash is the only evidence that can
        # resolve a locally remembered interrupted apply.  A missing entry keeps
        # the migration blocked so a caller cannot mistake an inconclusive read
        # for permission to retry.
        self._outcome_unknown_migration_ids.discard(migration_id)
        return {
            "outcome": "applied",
            "migration_id": migration_id,
            "source_sha256": source_sha256,
        }

    def _migration_source(self, migration_file: str) -> tuple[str, str, Path]:
        match = _MIGRATION_FILE.fullmatch(migration_file) if isinstance(migration_file, str) else None
        if match is None or Path(migration_file).name != migration_file:
            raise MigrationError("migration file must use the NNNN_description.sql form")
        migrations_dir = self._migrations_directory()
        source = migrations_dir / migration_file
        try:
            source.relative_to(migrations_dir)
        except ValueError:
            raise MigrationError("migration file must stay inside the configured directory") from None
        _require_regular_unlinked_file(source)
        return migration_file[:-4], match.group("number"), source

    def _migrations_directory(self) -> Path:
        configured = self._target.migrations_dir
        if not isinstance(configured, Path) or not configured.is_absolute():
            raise MigrationError("configured migrations directory is unavailable")
        migrations_dir = Path(os.path.abspath(configured))
        try:
            directory_stat = os.lstat(migrations_dir)
        except OSError:
            raise MigrationError("configured migrations directory is unavailable") from None
        if _is_link(directory_stat) or not stat.S_ISDIR(directory_stat.st_mode):
            raise MigrationError("configured migrations directory must not use links")
        _require_unlinked_ancestors(migrations_dir)
        return migrations_dir

    def _reject_duplicate_numeric_id(self, numeric_id: str, requested: Path) -> None:
        migrations_dir = requested.parent
        try:
            candidates = tuple(migrations_dir.iterdir())
        except OSError:
            raise MigrationError("configured migrations directory is unavailable") from None
        for candidate in candidates:
            match = _MIGRATION_FILE.fullmatch(candidate.name)
            if match is None or match.group("number") != numeric_id:
                continue
            _require_regular_unlinked_file(candidate)
            if candidate != requested:
                # A migration number is its immutable sequence identifier.  Even an
                # identical duplicate would make future ledger reconciliation
                # ambiguous, so fail closed before parsing or accessing MySQL.
                raise MigrationError("duplicate migration ID in configured directory")

    def _authorize_ddl(self, statement: _DdlStatement) -> MySqlPolicyDecision:
        columns = statement.columns or self._policy_columns_for(statement, action="ddl")
        operation = MySqlOperation(
            target=self._target.name,
            environment=self._target.environment,
            action="ddl",
            schemas=frozenset({statement.schema}),
            tables=frozenset({statement.table}),
            columns=columns,
            estimated_rows=None,
            migration_id=None,
            ddl_category=statement.category,
        )
        decision = self._authorize_for_plan(operation)
        if decision.outcome is PolicyOutcome.DENY:
            raise MigrationError("migration DDL is not authorized by policy")
        return decision

    def _authorize_migration(
        self,
        migration_id: str,
        statements: tuple[_DdlStatement, ...],
    ) -> MySqlPolicyDecision:
        first = statements[0]
        columns = frozenset().union(*(statement.columns for statement in statements))
        if not columns:
            columns = self._policy_columns_for(first, action="apply_migration")
        operation = MySqlOperation(
            target=self._target.name,
            environment=self._target.environment,
            action="apply_migration",
            schemas=frozenset({first.schema}),
            tables=frozenset({first.table}),
            columns=columns,
            estimated_rows=None,
            migration_id=migration_id,
            migration_directory=self._migrations_directory().name,
            ledger_table=self._configured_ledger_table(),
        )
        decision = self._authorize_for_plan(operation)
        if decision.outcome is PolicyOutcome.DENY:
            raise MigrationError("migration file is not authorized by policy")
        return decision

    def _configured_ledger_table(self) -> str:
        ledger_table = self._target.migration_ledger_table
        if not isinstance(ledger_table, str) or _IDENTIFIER.fullmatch(ledger_table) is None:
            raise MigrationError("configured migration ledger table is unavailable")
        return ledger_table

    def _policy_columns_for(self, statement: _DdlStatement, *, action: str) -> frozenset[str]:
        candidates = [
            rule.columns
            for rule in self._policy.rules
            if (
                self._target.name in rule.targets
                and self._target.environment in rule.environments
                and action in rule.actions
                and rule.schemas == frozenset({statement.schema})
                and rule.tables == frozenset({statement.table})
                and (action != "ddl" or statement.category in (rule.ddl_categories or frozenset()))
            )
        ]
        if not candidates:
            raise MigrationError("migration DDL is not authorized by policy")
        return candidates[0]

    def _authorize_for_plan(self, operation: MySqlOperation) -> MySqlPolicyDecision:
        if not self._target.is_read_only:
            return self._policy_service.authorize(operation)

        # PolicyService correctly denies write *application* on a restricted target.
        # A plan is nevertheless a read-only inspection, so check its policy scope
        # against an otherwise identical target whose read-only gate is disabled.
        # apply_migration (implemented separately) rechecks the real target before
        # any policy, confirmation, client, or SQL work.
        plan_target = replace(self._target, read_only_environments=frozenset())
        clock = getattr(self._policy_service, "_clock", None)
        return PolicyService(self._policy, target=plan_target, clock=clock).authorize(operation)

    def _consume_issued_plan(self, plan: MigrationPlan) -> None:
        if not isinstance(plan, MigrationPlan):
            raise MigrationError("trusted migration plan evidence is required")
        issued = self._issued_plans.pop(id(plan), None)
        if issued is None or issued() is not plan:
            raise MigrationError("trusted migration plan evidence is required")

    def discard_plan(self, plan: MigrationPlan) -> None:
        """Retire a plan used only to revalidate a confirmation replay."""

        self._consume_issued_plan(plan)

    def _reject_outcome_unknown_retry(self, plan: MigrationPlan) -> None:
        if plan.migration_id in self._outcome_unknown_migration_ids:
            raise MigrationError(
                "migration has an unknown outcome and must be reconciled before any retry"
            )

    def _revalidate_plan_source(self, plan: MigrationPlan) -> tuple[_DdlStatement, ...]:
        migration_file = f"{plan.migration_id}.sql"
        migration_id, numeric_id, source = self._migration_source(migration_file)
        if migration_id != plan.migration_id:
            raise MigrationError("trusted migration plan evidence is invalid")
        self._reject_duplicate_numeric_id(numeric_id, source)
        source_bytes = _read_source(source)
        if hashlib.sha256(source_bytes).hexdigest() != plan.source_sha256:
            raise MigrationError("migration source changed after planning")
        statements = _parse_ddl(source_bytes, target=self._target)
        self._require_one_scope(statements)
        if tuple(statement.statement for statement in statements) != plan.statements:
            raise MigrationError("migration source changed after planning")
        return statements

    def _apply_with_ledger(
        self,
        plan: MigrationPlan,
        statements: tuple[_DdlStatement, ...],
    ) -> dict[str, str]:
        ledger = self._ledger_name()
        lock_name = self._migration_lock_name(plan)
        ddl_started = False
        try:
            with self._connection_factory(self._target, self._credentials) as connection:
                cursor = connection.cursor()  # type: ignore[attr-defined]
                lock_acquired = False
                try:
                    self._acquire_migration_lock(cursor, lock_name)
                    lock_acquired = True
                    if (
                        self._schema_before_sha256_from_cursor(cursor, statements)
                        != plan.schema_before_sha256
                    ):
                        raise MigrationError("migration schema changed after planning")
                    self._ensure_ledger(cursor, ledger)
                    recorded_sha256 = self._lookup_ledger(cursor, ledger, plan.migration_id)
                    if recorded_sha256 is not None:
                        if recorded_sha256 != plan.source_sha256:
                            raise MigrationError("migration ledger source hash conflicts with this ID")
                        return {
                            "outcome": "already_applied",
                            "migration_id": plan.migration_id,
                            "fingerprint": plan.fingerprint,
                        }
                    for statement in statements:
                        ddl_started = True
                        cursor.execute(statement.statement, ())
                    cursor.execute(
                        f"INSERT INTO {ledger} "
                        "(migration_id, source_sha256, applied_at_utc, applied_by) "
                        "VALUES (%s, %s, UTC_TIMESTAMP(6), %s)",
                        (plan.migration_id, plan.source_sha256, "mysql-mcp"),
                    )
                    connection.commit()  # type: ignore[attr-defined]
                except _LedgerUnavailable:
                    raise
                except Exception:
                    if ddl_started:
                        return _unknown_outcome(plan)
                    raise
                finally:
                    if lock_acquired:
                        self._release_migration_lock(cursor, lock_name)
                    close = getattr(cursor, "close", None)
                    if callable(close):
                        close()
        except _LedgerUnavailable:
            raise
        except Exception:
            if ddl_started:
                return _unknown_outcome(plan)
            raise
        return {
            "outcome": "applied",
            "migration_id": plan.migration_id,
            "fingerprint": plan.fingerprint,
        }

    def _ensure_ledger(self, cursor: object, ledger: str) -> None:
        try:
            cursor.execute(  # type: ignore[attr-defined]
                f"CREATE TABLE IF NOT EXISTS {ledger} "
                "(migration_id VARCHAR(128) PRIMARY KEY, source_sha256 CHAR(64) NOT NULL, "
                "applied_at_utc DATETIME(6) NOT NULL, applied_by VARCHAR(128) NOT NULL)",
                (),
            )
        except Exception:
            raise _LedgerUnavailable from None

    def _acquire_migration_lock(self, cursor: object, lock_name: str) -> None:
        try:
            cursor.execute(  # type: ignore[attr-defined]
                "SELECT GET_LOCK(%s, 0) AS acquired",
                (lock_name,),
            )
            rows = cursor.fetchall()  # type: ignore[attr-defined]
        except Exception:
            raise MigrationError("migration advisory lock is unavailable") from None
        if (
            not isinstance(rows, (list, tuple))
            or len(rows) != 1
            or not isinstance(rows[0], Mapping)
            or not isinstance(rows[0].get("acquired"), int)
            or isinstance(rows[0].get("acquired"), bool)
        ):
            raise MigrationError("migration advisory lock has an invalid response")
        if rows[0]["acquired"] != 1:
            raise MigrationError("migration advisory lock is unavailable")

    def _release_migration_lock(self, cursor: object, lock_name: str) -> None:
        try:
            cursor.execute(  # type: ignore[attr-defined]
                "SELECT RELEASE_LOCK(%s) AS released",
                (lock_name,),
            )
        except Exception:
            # Closing the connection also releases an advisory lock.  Do not mask a
            # migration outcome with a cleanup-only transport failure.
            pass

    def _lookup_ledger(self, cursor: object, ledger: str, migration_id: str) -> str | None:
        try:
            cursor.execute(  # type: ignore[attr-defined]
                f"SELECT source_sha256 FROM {ledger} WHERE migration_id = %s FOR UPDATE",
                (migration_id,),
            )
            return _ledger_source_sha256(cursor.fetchall())  # type: ignore[attr-defined]
        except MigrationError:
            raise
        except Exception:
            raise _LedgerUnavailable from None

    def _ledger_name(self) -> str:
        return f"{_quoted_identifier(self._target.database)}.{_quoted_identifier(self._configured_ledger_table())}"

    def _migration_lock_name(self, plan: MigrationPlan) -> str:
        scope = "\x1f".join(
            (
                self._target.name,
                self._target.database,
                self._configured_ledger_table(),
                plan.migration_id,
            )
        )
        return "awh:" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:60]

    def _reject_read_only_target(self) -> None:
        if self._target.is_read_only:
            raise MigrationError("configured read-only environment rejects migration application")

    def _schema_before_sha256(self, statements: tuple[_DdlStatement, ...]) -> str:
        try:
            with self._connection_factory(self._target, self._credentials) as connection:
                cursor = connection.cursor()  # type: ignore[attr-defined]
                try:
                    return self._schema_before_sha256_from_cursor(cursor, statements)
                finally:
                    close = getattr(cursor, "close", None)
                    if callable(close):
                        close()
        except MigrationError:
            raise
        except Exception:
            raise MigrationError("could not read migration schema snapshot") from None

    @staticmethod
    def _schema_before_sha256_from_cursor(
        cursor: object,
        statements: tuple[_DdlStatement, ...],
    ) -> str:
        statement = statements[0]
        try:
            cursor.execute(  # type: ignore[attr-defined]
                _SCHEMA_SNAPSHOT_SQL,
                (statement.schema, statement.table),
            )
            rows = cursor.fetchall()  # type: ignore[attr-defined]
        except Exception:
            raise MigrationError("could not read migration schema snapshot") from None
        return _schema_snapshot_sha256(rows)

    @staticmethod
    def _require_one_scope(statements: tuple[_DdlStatement, ...]) -> None:
        if len({(statement.schema, statement.table) for statement in statements}) != 1:
            raise MigrationError("migration statements must have one explicit policy scope")


def _read_source(path: Path) -> bytes:
    try:
        with _guarded_regular_file(path, binary=True) as handle:
            source = handle.read()
    except (_UnsafePathError, OSError):
        raise MigrationError("migration source must be an existing regular unlinked file") from None
    if not isinstance(source, bytes) or not source:
        raise MigrationError("migration source must be a nonempty UTF-8 file")
    return source


def _parse_ddl(source: bytes, *, target: MySqlTarget) -> tuple[_DdlStatement, ...]:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        raise MigrationError("migration source must be a nonempty UTF-8 file") from None
    if "/*!" in text or "/*+" in text:
        raise MigrationError("migration DDL must not contain executable comments or hints")
    try:
        parsed = sqlglot.parse(text, read="mysql")
    except SqlglotError:
        raise MigrationError("migration source is not valid MySQL DDL") from None
    if not parsed or any(statement is None for statement in parsed):
        raise MigrationError("migration source must contain DDL statements")
    return tuple(_ddl_statement(statement, target=target) for statement in parsed)


def _ddl_statement(statement: exp.Expression, *, target: MySqlTarget) -> _DdlStatement:
    if any(isinstance(node, (exp.DML, exp.Command, exp.Select, exp.Into)) for node in statement.walk()):
        raise MigrationError("migration source contains non-DDL or non-allowed DDL")
    category: str
    table: exp.Table
    if isinstance(statement, exp.Create):
        kind = str(statement.args.get("kind", "")).upper()
        if kind == "TABLE" and isinstance(statement.this, exp.Schema) and isinstance(statement.this.this, exp.Table):
            category = "create"
            table = statement.this.this
        elif kind == "INDEX" and isinstance(statement.this, exp.Index):
            index_table = statement.this.args.get("table")
            if not isinstance(index_table, exp.Table):
                raise MigrationError("migration source contains non-allowed DDL")
            category = "index"
            table = index_table
        else:
            raise MigrationError("migration source contains non-allowed DDL")
    elif isinstance(statement, exp.Alter) and str(statement.args.get("kind", "")).upper() == "TABLE" and isinstance(statement.this, exp.Table):
        category = "alter"
        table = statement.this
    elif isinstance(statement, exp.Drop) and str(statement.args.get("kind", "")).upper() == "TABLE" and isinstance(statement.this, exp.Table):
        category = "drop"
        table = statement.this
    else:
        raise MigrationError("migration source contains non-allowed DDL")
    table_references = tuple(statement.find_all(exp.Table))
    if len(table_references) != 1 or table_references[0] is not table:
        raise MigrationError("migration DDL must have one explicit table target")
    schema, table_name = _ddl_scope(table, target=target)
    column_names = tuple(
        column.name
        for column in (*statement.find_all(exp.ColumnDef), *statement.find_all(exp.Column))
        if column.name
    )
    if any(_IDENTIFIER.fullmatch(column_name) is None for column_name in column_names):
        raise MigrationError("migration DDL contains an untrusted column identifier")
    columns = frozenset(column_names)
    return _DdlStatement(
        category=category,
        schema=schema,
        table=table_name,
        columns=columns,
        statement=statement.sql(dialect="mysql").rstrip(";"),
    )


def _ddl_scope(table: exp.Table, *, target: MySqlTarget) -> tuple[str, str]:
    schema = table.db or target.database
    table_name = table.name
    if (
        not isinstance(schema, str)
        or not isinstance(table_name, str)
        or schema != target.database
        or _IDENTIFIER.fullmatch(schema) is None
        or _IDENTIFIER.fullmatch(table_name) is None
    ):
        raise MigrationError("migration DDL must target one configured schema and table")
    return schema, table_name


def _schema_snapshot_sha256(rows: object) -> str:
    if not isinstance(rows, (list, tuple)) or not all(
        isinstance(row, Mapping) for row in rows
    ):
        raise MigrationError("migration schema snapshot has an invalid shape")
    facts: list[tuple[str | None, str | None, str | None, str | None, str | None]] = []
    keys = ("table_name", "column_name", "column_type", "is_nullable", "column_key")
    for row in rows:
        fact: list[str | None] = []
        for key in keys:
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                raise MigrationError("migration schema snapshot has an invalid shape")
            fact.append(value)
        facts.append(tuple(fact))  # type: ignore[arg-type]
    serialized = json.dumps(sorted(facts), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _plan_fingerprint(
    *,
    migration_id: str,
    source_sha256: str,
    schema_before_sha256: str,
    statements: tuple[_DdlStatement, ...],
    decisions: tuple[MySqlPolicyDecision, ...],
) -> str:
    payload = {
        "migration_id": migration_id,
        "source_sha256": source_sha256,
        "schema_before_sha256": schema_before_sha256,
        "statements": [
            {"category": statement.category, "schema": statement.schema, "table": statement.table}
            for statement in statements
        ],
        "policy_fingerprints": [decision.operation_fingerprint for decision in decisions],
    }
    serialized = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _require_regular_unlinked_file(path: Path) -> None:
    try:
        file_stat = os.lstat(path)
    except OSError:
        raise MigrationError("migration source must be an existing regular unlinked file") from None
    if _is_link(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise MigrationError("migration source must be an existing regular unlinked file")


def _require_unlinked_ancestors(path: Path) -> None:
    current = path
    try:
        while True:
            if _is_link(os.lstat(current)):
                raise _UnsafePathError
            parent = current.parent
            if parent == current:
                return
            current = parent
    except (OSError, _UnsafePathError):
        raise MigrationError("configured migrations directory must not use links") from None


def _is_link(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(attributes & _REPARSE_POINT)


class _LedgerUnavailable(Exception):
    """The fixed ledger cannot safely be used, so application must not proceed."""


def _unknown_outcome(plan: MigrationPlan) -> dict[str, str]:
    return {
        "outcome": "outcome_unknown",
        "migration_id": plan.migration_id,
        "fingerprint": plan.fingerprint,
    }


def _validate_migration_id(value: object) -> str:
    if not isinstance(value, str) or _MIGRATION_FILE.fullmatch(f"{value}.sql") is None:
        raise MigrationError("migration ID must use the NNNN_description form")
    return value


def _quoted_identifier(value: object) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise MigrationError("configured migration identifier is unavailable")
    return f"`{value}`"


def _ledger_source_sha256(rows: object) -> str | None:
    if (
        not isinstance(rows, (list, tuple))
        or len(rows) > 1
        or not all(isinstance(row, Mapping) for row in rows)
    ):
        raise MigrationError("migration ledger has an invalid shape")
    if not rows:
        return None
    source_sha256 = rows[0].get("source_sha256")
    if (
        not isinstance(source_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
    ):
        raise MigrationError("migration ledger has an invalid source hash")
    return source_sha256
