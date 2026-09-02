from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from pathlib import Path


@dataclass(frozen=True)
class MySqlTarget:
    """Immutable, non-secret target configuration for one MySQL environment."""

    name: str
    environment: str
    host: str
    port: int
    database: str | None = None
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    username_env: str | None = field(default=None, repr=False)
    password_env: str | None = field(default=None, repr=False)
    tls_verify: bool = False
    ca_bundle: Path | None = None
    connect_timeout_seconds: int = 10
    read_only_environments: frozenset[str] = field(default_factory=frozenset)
    source_path: Path | None = field(default=None, repr=False)
    policy_path: Path | None = None
    migrations_dir: Path | None = None
    migration_ledger_table: str | None = None
    allow_insecure_tls: bool = False
    max_result_rows: int = 100
    require_confirmation: bool = False
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.read_only_environments, frozenset):
            object.__setattr__(
                self,
                "read_only_environments",
                frozenset(self.read_only_environments),
            )

    @property
    def is_read_only(self) -> bool:
        """Whether this target is restricted by an exact environment-name match."""

        return self.environment in self.read_only_environments


@dataclass(frozen=True)
class MySqlCredentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


MYSQL_POLICY_ACTIONS = frozenset(
    {
        "metadata",
        "read",
        "explain",
        "insert",
        "update",
        "delete",
        "transaction",
        "ddl",
        "apply_migration",
    }
)
MYSQL_WRITE_ACTIONS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "transaction",
        "ddl",
        "apply_migration",
    }
)
MYSQL_DML_ACTIONS = frozenset({"insert", "update", "delete", "transaction"})
MYSQL_DDL_CATEGORIES = frozenset({"create", "alter", "drop", "index"})


@dataclass(frozen=True)
class MySqlOperation:
    """A value-free, non-executing preflight claim for one MySQL operation.

    Policy decisions never open a client or execute SQL. Later SQL-guard and write
    layers must bind this claimed shape to parsed statements and database evidence.
    """

    target: str
    environment: str
    action: str
    schemas: frozenset[str]
    tables: frozenset[str]
    columns: frozenset[str]
    estimated_rows: int | None
    migration_id: str | None
    where_columns: frozenset[str] = field(default_factory=frozenset)
    ddl_category: str | None = None
    migration_directory: str | None = None
    ledger_table: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schemas", _snapshot_string_set(self.schemas, "schemas"))
        object.__setattr__(self, "tables", _snapshot_string_set(self.tables, "tables"))
        object.__setattr__(self, "columns", _snapshot_string_set(self.columns, "columns"))
        object.__setattr__(
            self,
            "where_columns",
            _snapshot_string_set(self.where_columns, "where_columns"),
        )


@dataclass(frozen=True)
class MySqlPolicyRule:
    """One explicit, least-privilege authorization rule."""

    name: str
    targets: frozenset[str]
    environments: frozenset[str]
    actions: frozenset[str]
    schemas: frozenset[str]
    tables: frozenset[str]
    columns: frozenset[str]
    max_return_rows: int | None = None
    max_dml_rows: int | None = None
    requires_where: bool | None = None
    requires_primary_key: bool | None = None
    primary_key_columns: frozenset[str] | None = None
    predicate_requirement: str | None = None
    ddl_categories: frozenset[str] | None = None
    migration_directory: str | None = None
    migration_ids: frozenset[str] | None = None
    expires_at: datetime | None = None
    migration_ledger_tables: frozenset[str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _nonblank_string(self.name, "name"))
        for field_name in (
            "targets",
            "environments",
            "actions",
            "schemas",
            "tables",
        ):
            object.__setattr__(
                self,
                field_name,
                _snapshot_string_set(getattr(self, field_name), field_name, nonempty=True),
            )
        object.__setattr__(
            self,
            "columns",
            _snapshot_string_set(self.columns, "columns", nonempty=True),
        )
        if len(self.schemas) != 1 or len(self.tables) != 1:
            raise ValueError(
                "MySQL policy rules require one explicit schema-to-table mapping"
            )
        if not self.actions <= MYSQL_POLICY_ACTIONS:
            raise ValueError("MySQL policy rules contain an unsupported action")
        _validate_optional_positive_integer(self.max_return_rows, "max_return_rows")
        _validate_optional_positive_integer(self.max_dml_rows, "max_dml_rows")
        _validate_optional_bool(self.requires_where, "requires_where")
        _validate_optional_bool(self.requires_primary_key, "requires_primary_key")
        if self.primary_key_columns is not None:
            object.__setattr__(
                self,
                "primary_key_columns",
                _snapshot_string_set(
                    self.primary_key_columns,
                    "primary_key_columns",
                    nonempty=True,
                ),
            )
            if not all(_is_identifier(value) for value in self.primary_key_columns):
                raise ValueError("primary_key_columns must contain identifiers")
            if not self.primary_key_columns <= self.columns:
                raise ValueError("primary_key_columns must be allowed rule columns")
        if self.predicate_requirement is not None and (
            not isinstance(self.predicate_requirement, str)
            or self.predicate_requirement not in {"all", "any"}
        ):
            raise ValueError("predicate_requirement must be all or any")
        if self.expires_at is not None and (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must include a timezone")
        if self.ddl_categories is not None:
            object.__setattr__(
                self,
                "ddl_categories",
                _snapshot_string_set(
                    self.ddl_categories,
                    "ddl_categories",
                    nonempty=True,
                ),
            )
            if not self.ddl_categories <= MYSQL_DDL_CATEGORIES:
                raise ValueError("ddl_categories contain an unsupported value")
        if self.migration_ids is not None:
            object.__setattr__(
                self,
                "migration_ids",
                _snapshot_string_set(
                    self.migration_ids,
                    "migration_ids",
                    nonempty=True,
                ),
            )
            if not all(_is_migration_id(value) for value in self.migration_ids):
                raise ValueError("migration_ids must contain safe migration IDs")
        if self.migration_ledger_tables is not None:
            object.__setattr__(
                self,
                "migration_ledger_tables",
                _snapshot_string_set(
                    self.migration_ledger_tables,
                    "migration_ledger_tables",
                    nonempty=True,
                ),
            )
            if not all(_is_identifier(value) for value in self.migration_ledger_tables):
                raise ValueError(
                    "migration_ledger_tables must contain safe table identifiers"
                )
        if self.migration_directory is not None and not _is_migration_directory(
            self.migration_directory
        ):
            raise ValueError("migration_directory must be a safe relative directory name")

        if self.actions & {"metadata", "read", "explain"} and self.max_return_rows is None:
            raise ValueError("read policy rules require max_return_rows")
        if self.actions & MYSQL_DML_ACTIONS and self.max_dml_rows is None:
            raise ValueError("DML policy rules require max_dml_rows")
        if self.actions & {"update", "delete"}:
            if self.requires_where is None or self.requires_primary_key is None:
                raise ValueError(
                    "update/delete policy rules require WHERE and primary-key constraints"
                )
            if self.requires_primary_key and self.primary_key_columns is None:
                raise ValueError(
                    "primary-key policy rules require primary_key_columns"
                )
            if not self.requires_primary_key and self.primary_key_columns is not None:
                raise ValueError(
                    "primary_key_columns only apply when primary-key protection is required"
                )
            if not self.requires_where and not self.requires_primary_key:
                raise ValueError(
                    "update/delete policy rules require WHERE or primary-key protection"
                )
            if self.predicate_requirement == "any" and not (
                self.requires_where and self.requires_primary_key
            ):
                raise ValueError(
                    "predicate_requirement=any requires both protections"
                )
        elif (
            self.requires_where is not None
            or self.requires_primary_key is not None
            or self.primary_key_columns is not None
            or self.predicate_requirement is not None
        ):
            raise ValueError("WHERE and primary-key constraints only apply to update/delete")
        if "ddl" in self.actions:
            if self.ddl_categories is None:
                raise ValueError("DDL policy rules require ddl_categories")
        elif self.ddl_categories is not None:
            raise ValueError("ddl_categories only apply to DDL")
        if "apply_migration" in self.actions:
            if (
                self.migration_directory is None
                or self.migration_ids is None
                or self.migration_ledger_tables is None
            ):
                raise ValueError(
                    "migration policy rules require directory, IDs, and ledger tables"
                )
        elif (
            self.migration_directory is not None
            or self.migration_ids is not None
            or self.migration_ledger_tables is not None
        ):
            raise ValueError(
                "migration directory, IDs, and ledger tables only apply to migrations"
            )
        if self.actions & MYSQL_WRITE_ACTIONS and self.expires_at is None:
            raise ValueError("write policy rules require expires_at")


@dataclass(frozen=True)
class MySqlPolicy:
    rules: tuple[MySqlPolicyRule, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.rules, (list, tuple)):
            raise TypeError("MySQL policy rules must be a list or tuple")
        rules = tuple(self.rules)
        if not rules or not all(isinstance(rule, MySqlPolicyRule) for rule in rules):
            raise ValueError("MySQL policy rules must be nonempty MySqlPolicyRule values")
        if len({rule.name for rule in rules}) != len(rules):
            raise ValueError("MySQL policy rules must not contain duplicate names")
        object.__setattr__(self, "rules", rules)


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_USER_CONFIRMATION = "needs_user_confirmation"


@dataclass(frozen=True)
class MySqlPolicyDecision:
    outcome: PolicyOutcome
    reason: str
    rule_name: str | None = None
    operation_fingerprint: str | None = None

    @property
    def allowed(self) -> bool:
        return self.outcome is PolicyOutcome.ALLOW

    @property
    def requires_confirmation(self) -> bool:
        return self.outcome is PolicyOutcome.NEEDS_USER_CONFIRMATION


def operation_fingerprint(operation: MySqlOperation) -> str:
    """Return a stable fingerprint of metadata, never query values or credentials."""

    payload = {
        "action": operation.action,
        "columns": sorted(operation.columns),
        "ddl_category": operation.ddl_category,
        "environment": operation.environment,
        "estimated_rows": operation.estimated_rows,
        "migration_directory": operation.migration_directory,
        "migration_id": operation.migration_id,
        "ledger_table": operation.ledger_table,
        "schemas": sorted(operation.schemas),
        "tables": sorted(operation.tables),
        "target": operation.target,
        "where_columns": sorted(operation.where_columns),
    }
    try:
        serialized = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise ValueError("MySQL operation contains unsupported fingerprint data") from None
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _snapshot_string_set(
    value: object,
    field_name: str,
    *,
    nonempty: bool = False,
) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError(f"{field_name} must be a collection of strings")
    values = tuple(value)
    if nonempty and not values:
        raise ValueError(f"{field_name} must not be empty")
    if not all(isinstance(item, str) and item and item == item.strip() for item in values):
        raise TypeError(f"{field_name} must contain nonblank strings")
    result = frozenset(values)
    if len(result) != len(values):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


def _nonblank_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TypeError(f"{field_name} must be a nonblank string")
    return value


def _validate_optional_positive_integer(value: object, field_name: str) -> None:
    if value is not None and (
        not isinstance(value, int) or isinstance(value, bool) or value < 1
    ):
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_optional_bool(value: object, field_name: str) -> None:
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")


def _is_migration_directory(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "/" not in value
        and "\\" not in value
        and value not in {".", ".."}
    )


def _is_migration_id(value: object) -> bool:
    allowed = frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    )
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value[0].isdigit()
        and all(character in allowed for character in value)
    )


def _is_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value.isascii()
        and value.isidentifier()
    )


class MySqlStatementStatus(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    OUTCOME_UNKNOWN = "outcome_unknown"
    NOT_EXECUTED = "not_executed"


class MySqlErrorClassification(StrEnum):
    PARAMETER_ERROR = "parameter_error"
    CLIENT_COMMAND = "client_command"
    CONFIGURATION = "configuration"
    CONNECTION = "connection"
    TIMEOUT = "timeout"
    POLICY_NOT_APPLICABLE = "policy_not_applicable"


@dataclass(frozen=True)
class MySqlConnectionOverride:
    connection_string: str | None = field(default=None, repr=False)
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    tls_verify: bool | None = None
    ca_bundle: str | None = None
    max_result_rows: int | None = None


@dataclass(frozen=True)
class MySqlResultSet:
    columns: tuple[str, ...]
    rows: tuple[dict[str, object], ...] = field(repr=False)
    row_count: int
    truncated: bool


@dataclass(frozen=True)
class MySqlStatementResult:
    index: int
    status: MySqlStatementStatus
    classification: MySqlErrorClassification | None = None
    result_sets: tuple[MySqlResultSet, ...] = ()
    affected_rows: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class MySqlExecutionResult:
    statements: tuple[MySqlStatementResult, ...]


class MySqlExecutionError(RuntimeError):
    def __init__(self, classification: MySqlErrorClassification, message: str) -> None:
        super().__init__(message)
        self.classification = classification
        self.message = message
