from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path

import yaml

from .config import _UnsafePathError, _guarded_regular_file
from .models import (
    MYSQL_DDL_CATEGORIES,
    MYSQL_DML_ACTIONS,
    MYSQL_POLICY_ACTIONS,
    MYSQL_WRITE_ACTIONS,
    MySqlOperation,
    MySqlPolicy,
    MySqlPolicyDecision,
    MySqlPolicyRule,
    MySqlTarget,
    PolicyOutcome,
    operation_fingerprint,
)


class PolicyError(ValueError):
    """A MySQL policy document is malformed, unsafe, or cannot be authorized."""


_READ_ACTIONS = frozenset({"metadata", "read", "explain"})
_HIGH_RISK_DDL_CATEGORIES = frozenset({"alter", "drop"})
_RULE_REQUIRED_KEYS = frozenset(
    {"name", "targets", "environments", "actions", "schemas", "tables", "columns"}
)
_RULE_OPTIONAL_KEYS = frozenset(
    {
        "max_return_rows",
        "max_dml_rows",
        "requires_where",
        "requires_primary_key",
        "primary_key_columns",
        "predicate_requirement",
        "ddl_categories",
        "migration_directory",
        "migration_ids",
        "migration_ledger_tables",
        "expires_at",
    }
)
_MIGRATION_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def load_policy(path: Path) -> MySqlPolicy:
    """Read one guarded policy document using a duplicate-key-safe YAML loader."""

    policy_path = Path(path)
    if not policy_path.is_absolute():
        raise PolicyError("MySQL policy path must be absolute")
    try:
        with _guarded_regular_file(policy_path, binary=False) as handle:
            raw = yaml.load(handle, Loader=_UniqueSafeLoader)
    except (_UnsafePathError, OSError, UnicodeError):
        raise PolicyError("could not read MySQL policy") from None
    except yaml.YAMLError:
        raise PolicyError("could not parse MySQL policy") from None
    if not isinstance(raw, Mapping):
        raise PolicyError("MySQL policy must be a mapping")
    return parse_policy(raw)


def parse_policy(raw: Mapping[str, object]) -> MySqlPolicy:
    """Parse a strict, explicit MySQL least-privilege policy."""

    if not isinstance(raw, Mapping):
        raise PolicyError("MySQL policy must be a mapping")
    if set(raw) != {"version", "rules"}:
        raise PolicyError("MySQL policy permits only version and rules")
    if type(raw.get("version")) is not int or raw["version"] != 1:
        raise PolicyError("MySQL policy version must be the integer 1")
    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise PolicyError("MySQL policy must define one or more rules")

    names: set[str] = set()
    rules: list[MySqlPolicyRule] = []
    for raw_rule in rules_raw:
        rule = _parse_rule(raw_rule)
        if rule.name in names:
            raise PolicyError("MySQL policy contains a duplicate rule name")
        names.add(rule.name)
        rules.append(rule)
    return MySqlPolicy(rules=tuple(rules))


class PolicyService:
    """Return non-executing policy decisions for typed MySQL preflight claims."""

    def __init__(
        self,
        policy: MySqlPolicy,
        *,
        target: MySqlTarget,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(policy, MySqlPolicy):
            raise TypeError("policy must be a parsed MySqlPolicy")
        if not isinstance(target, MySqlTarget):
            raise TypeError("target must be a MySqlTarget")
        self._policy = policy
        self._target = target
        self._clock = clock or (lambda: datetime.now(UTC))

    def authorize(self, operation: MySqlOperation) -> MySqlPolicyDecision:
        """Return an outcome without consulting a connection or confirmation token."""

        if not isinstance(operation, MySqlOperation):
            return _deny("operation must be a MySqlOperation")

        # This must remain before all target, rule, confirmation, and client work.
        # The trusted configuration's exact match is the only read-only check.
        if (
            self._target.is_read_only
            and _is_write_action(operation.action)
        ):
            return _deny("configured read-only environment rejects write operations")

        if (
            operation.target != self._target.name
            or operation.environment != self._target.environment
        ):
            return _deny("operation target does not match the trusted configuration")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return _deny("policy evaluation time must include a timezone")
        if _invalid_operation(operation) is not None:
            return _deny("operation shape is invalid")

        matching_rules = tuple(
            rule for rule in self._policy.rules if _rule_matches(rule, operation)
        )
        if not matching_rules:
            return _deny("no single active policy rule allows the complete operation")
        if any(
            rule.expires_at is not None and now >= rule.expires_at
            for rule in matching_rules
        ):
            return _deny("a matching policy rule is expired")

        rule = matching_rules[0]
        fingerprint = operation_fingerprint(operation)
        if (
            self._target.environment == "production"
            and operation.action in MYSQL_WRITE_ACTIONS
        ):
            return MySqlPolicyDecision(
                PolicyOutcome.NEEDS_USER_CONFIRMATION,
                "production writes require host-mediated user confirmation",
                rule.name,
                fingerprint,
            )
        if operation.action == "apply_migration":
            return MySqlPolicyDecision(
                PolicyOutcome.NEEDS_USER_CONFIRMATION,
                "migration application requires host-mediated user confirmation",
                rule.name,
                fingerprint,
            )
        if (
            operation.action == "ddl"
            and operation.ddl_category in _HIGH_RISK_DDL_CATEGORIES
        ):
            return MySqlPolicyDecision(
                PolicyOutcome.NEEDS_USER_CONFIRMATION,
                "high-risk DDL requires host-mediated user confirmation",
                rule.name,
                fingerprint,
            )
        if (
            operation.action == "delete"
            and operation.estimated_rows is not None
            and operation.estimated_rows > 1
        ):
            return MySqlPolicyDecision(
                PolicyOutcome.NEEDS_USER_CONFIRMATION,
                "bulk delete requires host-mediated user confirmation",
                rule.name,
                fingerprint,
            )
        return MySqlPolicyDecision(
            PolicyOutcome.ALLOW,
            "allowed by one complete active policy rule",
            rule.name,
            fingerprint,
        )

    def context_fingerprint(self) -> str:
        """Fingerprint the current non-secret target configuration and policy."""

        target = self._target
        rules = [
            {
                "name": rule.name,
                "targets": sorted(rule.targets),
                "environments": sorted(rule.environments),
                "actions": sorted(rule.actions),
                "schemas": sorted(rule.schemas),
                "tables": sorted(rule.tables),
                "columns": sorted(rule.columns),
                "max_return_rows": rule.max_return_rows,
                "max_dml_rows": rule.max_dml_rows,
                "requires_where": rule.requires_where,
                "requires_primary_key": rule.requires_primary_key,
                "primary_key_columns": None if rule.primary_key_columns is None else sorted(rule.primary_key_columns),
                "predicate_requirement": rule.predicate_requirement,
                "ddl_categories": None if rule.ddl_categories is None else sorted(rule.ddl_categories),
                "migration_directory": rule.migration_directory,
                "migration_ids": None if rule.migration_ids is None else sorted(rule.migration_ids),
                "migration_ledger_tables": None if rule.migration_ledger_tables is None else sorted(rule.migration_ledger_tables),
                "expires_at": None if rule.expires_at is None else rule.expires_at.astimezone(UTC).isoformat(),
            }
            for rule in self._policy.rules
        ]
        payload = {
            "target": {
                "name": target.name, "environment": target.environment,
                "host": target.host, "port": target.port, "database": target.database,
                "tls_verify": target.tls_verify,
                "ca_bundle": None if target.ca_bundle is None else str(target.ca_bundle),
                "connect_timeout_seconds": target.connect_timeout_seconds,
                "read_only_environments": sorted(target.read_only_environments),
                "policy_path": None if target.policy_path is None else str(target.policy_path),
                "migrations_dir": None if target.migrations_dir is None else str(target.migrations_dir),
                "migration_ledger_table": target.migration_ledger_table,
                "allow_insecure_tls": target.allow_insecure_tls,
            },
            "policy": rules,
        }
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def _parse_rule(raw: object) -> MySqlPolicyRule:
    if not isinstance(raw, Mapping):
        raise PolicyError("MySQL policy rules must be mappings")
    missing = _RULE_REQUIRED_KEYS - set(raw)
    unknown = set(raw) - _RULE_REQUIRED_KEYS - _RULE_OPTIONAL_KEYS
    if missing or unknown:
        raise PolicyError("MySQL policy rule has missing or unknown fields")
    if any(raw[key] is None for key in _RULE_OPTIONAL_KEYS if key in raw):
        raise PolicyError("MySQL policy rule does not permit null optional values")

    name = _string(raw["name"], "rule name")
    targets = _string_set(raw["targets"], "targets")
    environments = _string_set(raw["environments"], "environments")
    actions = _string_set(raw["actions"], "actions")
    schemas = _string_set(raw["schemas"], "schemas")
    tables = _string_set(raw["tables"], "tables")
    columns = _string_set(raw["columns"], "columns")
    if len(schemas) != 1 or len(tables) != 1:
        raise PolicyError(
            "MySQL policy rules require one explicit schema-to-table mapping"
        )
    if not actions <= MYSQL_POLICY_ACTIONS:
        raise PolicyError("MySQL policy rule has an unsupported action")

    max_return_rows = _optional_positive_integer(
        raw.get("max_return_rows"),
        "max_return_rows",
    )
    max_dml_rows = _optional_positive_integer(raw.get("max_dml_rows"), "max_dml_rows")
    requires_where = _optional_bool(raw.get("requires_where"), "requires_where")
    requires_primary_key = _optional_bool(
        raw.get("requires_primary_key"),
        "requires_primary_key",
    )
    primary_key_columns = _optional_string_set(
        raw.get("primary_key_columns"),
        "primary_key_columns",
    )
    predicate_requirement = _optional_predicate_requirement(
        raw.get("predicate_requirement")
    )
    ddl_categories = _optional_string_set(raw.get("ddl_categories"), "ddl_categories")
    migration_directory = _optional_migration_directory(raw.get("migration_directory"))
    migration_ids = _optional_migration_ids(raw.get("migration_ids"))
    migration_ledger_tables = _optional_migration_ledger_tables(
        raw.get("migration_ledger_tables")
    )
    expires_at = _parse_expiry(raw.get("expires_at"))

    if actions & _READ_ACTIONS and max_return_rows is None:
        raise PolicyError("read policy rules require max_return_rows")
    if actions & MYSQL_DML_ACTIONS and max_dml_rows is None:
        raise PolicyError("DML policy rules require max_dml_rows")
    if actions & {"update", "delete"}:
        if requires_where is None or requires_primary_key is None:
            raise PolicyError("update/delete policy rules require WHERE and primary-key constraints")
        if not requires_where and not requires_primary_key:
            raise PolicyError("update/delete policy rules require WHERE or primary-key protection")
        if predicate_requirement == "any" and not (
            requires_where and requires_primary_key
        ):
            raise PolicyError("predicate_requirement=any requires both protections")
    elif (
        requires_where is not None
        or requires_primary_key is not None
        or predicate_requirement is not None
    ):
        raise PolicyError("WHERE and primary-key constraints only apply to update/delete")
    if "ddl" in actions:
        if ddl_categories is None or not ddl_categories <= MYSQL_DDL_CATEGORIES:
            raise PolicyError("DDL policy rules require supported ddl_categories")
    elif ddl_categories is not None:
        raise PolicyError("ddl_categories only apply to DDL")
    if "apply_migration" in actions:
        if (
            migration_directory is None
            or migration_ids is None
            or migration_ledger_tables is None
        ):
            raise PolicyError(
                "migration policy rules require directory, IDs, and ledger tables"
            )
    elif (
        migration_directory is not None
        or migration_ids is not None
        or migration_ledger_tables is not None
    ):
        raise PolicyError(
            "migration directory, IDs, and ledger tables only apply to migrations"
        )
    if actions & MYSQL_WRITE_ACTIONS and expires_at is None:
        raise PolicyError("write policy rules require expires_at")

    try:
        return MySqlPolicyRule(
            name=name,
            targets=targets,
            environments=environments,
            actions=actions,
            schemas=schemas,
            tables=tables,
            columns=columns,
            max_return_rows=max_return_rows,
            max_dml_rows=max_dml_rows,
            requires_where=requires_where,
            requires_primary_key=requires_primary_key,
            primary_key_columns=primary_key_columns,
            predicate_requirement=predicate_requirement,
            ddl_categories=ddl_categories,
            migration_directory=migration_directory,
            migration_ids=migration_ids,
            migration_ledger_tables=migration_ledger_tables,
            expires_at=expires_at,
        )
    except (TypeError, ValueError):
        raise PolicyError("MySQL policy rule violates a safety constraint") from None


def _rule_matches(rule: MySqlPolicyRule, operation: MySqlOperation) -> bool:
    if (
        operation.target not in rule.targets
        or operation.environment not in rule.environments
        or operation.action not in rule.actions
        or operation.schemas != rule.schemas
        or operation.tables != rule.tables
        or not operation.columns <= rule.columns
        or not operation.where_columns <= rule.columns
    ):
        return False
    if operation.action in _READ_ACTIONS:
        return (
            rule.max_return_rows is not None
            and operation.estimated_rows is not None
            and operation.estimated_rows <= rule.max_return_rows
        )
    if operation.action in MYSQL_DML_ACTIONS:
        if (
            rule.max_dml_rows is None
            or operation.estimated_rows is None
            or operation.estimated_rows > rule.max_dml_rows
        ):
            return False
        if operation.action in {"update", "delete"}:
            has_where = bool(operation.where_columns)
            has_primary_key = (
                rule.primary_key_columns is not None
                and rule.primary_key_columns <= operation.where_columns
            )
            if rule.predicate_requirement == "any":
                if not (
                    (rule.requires_where and has_where)
                    or (rule.requires_primary_key and has_primary_key)
                ):
                    return False
            else:
                if rule.requires_where and not has_where:
                    return False
                if rule.requires_primary_key and not has_primary_key:
                    return False
    if operation.action == "ddl":
        if operation.ddl_category not in rule.ddl_categories:
            return False
    if operation.action == "apply_migration":
        if (
            operation.migration_directory != rule.migration_directory
            or operation.migration_id not in rule.migration_ids
            or operation.ledger_table not in rule.migration_ledger_tables
        ):
            return False
    return True


def _invalid_operation(operation: MySqlOperation) -> str | None:
    if not _is_nonblank_string(operation.target) or not _is_nonblank_string(operation.environment):
        return "operation target or environment is invalid"
    if (
        not _is_nonblank_string(operation.action)
        or operation.action not in MYSQL_POLICY_ACTIONS
    ):
        return "operation action is invalid"
    if operation.estimated_rows is not None and (
        not isinstance(operation.estimated_rows, int)
        or isinstance(operation.estimated_rows, bool)
        or operation.estimated_rows < 0
    ):
        return "operation row estimate is invalid"
    if (
        len(operation.schemas) != 1
        or len(operation.tables) != 1
        or not operation.columns
    ):
        return "operation object scope is incomplete or ambiguous"
    if operation.action == "transaction":
        return "transactions require per-statement authorization evidence"
    if operation.action == "ddl":
        if (
            not _is_nonblank_string(operation.ddl_category)
            or operation.ddl_category not in MYSQL_DDL_CATEGORIES
        ):
            return "operation DDL category is invalid"
    elif operation.ddl_category is not None:
        return "operation DDL category is not applicable"
    if operation.action == "apply_migration":
        if (
            not _is_migration_directory(operation.migration_directory)
            or not _is_migration_id(operation.migration_id)
            or not _is_identifier(operation.ledger_table)
        ):
            return "operation migration metadata is invalid"
    elif (
        operation.migration_directory is not None
        or operation.migration_id is not None
        or operation.ledger_table is not None
    ):
        return "operation migration metadata is not applicable"
    if operation.action in MYSQL_DML_ACTIONS and operation.estimated_rows is None:
        return "DML operations require a row estimate"
    return None


def _deny(reason: str) -> MySqlPolicyDecision:
    return MySqlPolicyDecision(PolicyOutcome.DENY, reason)


def _string(value: object, field_name: str) -> str:
    if not _is_nonblank_string(value):
        raise PolicyError(f"{field_name} must be a nonblank string")
    return value


def _string_set(
    value: object,
    field_name: str,
    *,
    allow_empty: bool = False,
) -> frozenset[str]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise PolicyError(f"{field_name} must be a nonempty list of strings")
    if not all(_is_nonblank_string(item) for item in value):
        raise PolicyError(f"{field_name} must contain nonblank strings")
    result = frozenset(value)
    if len(result) != len(value):
        raise PolicyError(f"{field_name} must not contain duplicates")
    return result


def _optional_string_set(value: object, field_name: str) -> frozenset[str] | None:
    return None if value is None else _string_set(value, field_name)


def _optional_positive_integer(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PolicyError(f"{field_name} must be a positive integer")
    return value


def _optional_bool(value: object, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise PolicyError(f"{field_name} must be a boolean")
    return value


def _optional_predicate_requirement(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in {"all", "any"}:
        raise PolicyError("predicate_requirement must be all or any")
    return value


def _optional_migration_directory(value: object) -> str | None:
    if value is None:
        return None
    if not _is_migration_directory(value):
        raise PolicyError("migration_directory must be a safe relative directory name")
    return value


def _optional_migration_ids(value: object) -> frozenset[str] | None:
    if value is None:
        return None
    migration_ids = _string_set(value, "migration_ids")
    if not all(_is_migration_id(item) for item in migration_ids):
        raise PolicyError("migration_ids must contain safe migration IDs")
    return migration_ids


def _optional_migration_ledger_tables(value: object) -> frozenset[str] | None:
    if value is None:
        return None
    ledger_tables = _string_set(value, "migration_ledger_tables")
    if not all(_is_identifier(item) for item in ledger_tables):
        raise PolicyError(
            "migration_ledger_tables must contain safe table identifiers"
        )
    return ledger_tables


def _parse_expiry(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyError("expires_at must be an ISO timestamp")
    try:
        expiry = datetime.fromisoformat(value)
    except ValueError:
        raise PolicyError("expires_at must be an ISO timestamp") from None
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise PolicyError("expires_at must include a timezone")
    return expiry.astimezone(UTC)


def _is_nonblank_string(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _is_write_action(value: object) -> bool:
    return isinstance(value, str) and value in MYSQL_WRITE_ACTIONS


def _is_migration_directory(value: object) -> bool:
    return (
        _is_nonblank_string(value)
        and "/" not in value
        and "\\" not in value
        and value not in {".", ".."}
    )


def _is_migration_id(value: object) -> bool:
    return (
        _is_nonblank_string(value)
        and 1 <= len(value) <= 128
        and value[0].isdigit()
        and all(character in _MIGRATION_ID_CHARACTERS for character in value)
    )


def _is_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 64
        and value.isascii()
        and value.isidentifier()
    )


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueSafeLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)
