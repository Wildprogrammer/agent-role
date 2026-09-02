from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Mapping

from .models import (
    ALLOWED_ACTIONS,
    JenkinsConfig,
    OperationRequest,
    Policy,
    PolicyDecision,
    PolicyRule,
    request_fingerprint,
)


class PolicyError(ValueError):
    pass


def parse_policy(raw: Mapping[str, Any]) -> Policy:
    if raw.get("version") != 1:
        raise PolicyError("Jenkins policy version must be 1")
    if set(raw) != {"version", "rules"}:
        raise PolicyError("Jenkins policy permits only version and rules")
    rules_raw = raw.get("rules")
    if not isinstance(rules_raw, list) or not rules_raw:
        raise PolicyError("Jenkins policy must define one or more rules")

    names: set[str] = set()
    rules: list[PolicyRule] = []
    for raw_rule in rules_raw:
        rule = _parse_rule(raw_rule)
        if rule.name in names:
            raise PolicyError(f"duplicate Jenkins policy rule {rule.name!r}")
        names.add(rule.name)
        rules.append(rule)
    return Policy(rules=tuple(rules))


def _evaluate_policy(
    policy: Policy,
    request: OperationRequest,
    *,
    config: JenkinsConfig,
    now: datetime,
) -> PolicyDecision:
    controller = config.controllers.get(request.controller)
    if controller is None:
        return PolicyDecision(False, False, "request controller is not configured")
    if now.tzinfo is None or now.utcoffset() is None:
        return PolicyDecision(False, False, "policy evaluation time must include a timezone")
    normalized_path = _normalize_item_path(request.item_path)
    if normalized_path is None:
        return PolicyDecision(False, False, "invalid item path")
    if request.action not in ALLOWED_ACTIONS:
        return PolicyDecision(False, False, f"unsupported action {request.action!r}")
    invalid_shape = _invalid_request_shape(request)
    if invalid_shape is not None:
        return PolicyDecision(False, False, invalid_shape)

    matched_action = False
    parameter_rejected = False
    expired_rule = False
    for rule in policy.rules:
        if request.action != rule.action:
            continue
        matched_action = True
        if rule.expires_at is not None and now >= rule.expires_at:
            expired_rule = True
            continue
        if request.controller not in rule.controllers:
            continue
        if controller.environment not in rule.environments:
            continue
        if not any(_path_is_within(normalized_path, prefix) for prefix in rule.path_prefixes):
            continue
        if request.action == "read" and (request.read_scope or "item") not in (
            rule.read_scopes or frozenset({"item"})
        ):
            continue
        if rule.item_types is not None and request.item_type not in rule.item_types:
            continue
        if rule.templates is not None and request.template not in rule.templates:
            continue
        if rule.allowed_fields is not None and not request.fields <= rule.allowed_fields:
            continue
        if not _parameters_match(request.parameters, rule.parameters):
            parameter_rejected = True
            continue

        return PolicyDecision(
            True,
            False,
            "allowed by matching policy rule",
            rule.name,
            rule.max_concurrent,
            request.controller,
            request.action,
            request_fingerprint(request),
        )

    if parameter_rejected:
        return PolicyDecision(False, False, "parameters are outside the policy schema")
    if expired_rule:
        return PolicyDecision(False, False, "matching policy rule is expired")
    if not matched_action:
        return PolicyDecision(False, False, f"no policy rule allows action {request.action}")
    return PolicyDecision(
        False,
        False,
        f"no policy rule allows action {request.action} for the requested target",
    )


def _invalid_request_shape(request: OperationRequest) -> str | None:
    if request.action == "read":
        if (
            request.template is not None
            or request.fields
            or request.parameters
            or request.change_digest is not None
            or request.target_build_number is not None
            or request.base_config_digest is not None
            or request.read_scope not in {None, "item", "controller", "root_list", "nodes"}
        ):
            return "read requests require a valid read scope and cannot include write fields"
    elif request.action == "create_item":
        if request.item_type is None or request.template is None:
            return "create_item requests require item_type and template"
        if (
            request.fields
            or request.change_digest is not None
            or request.target_build_number is not None
            or request.base_config_digest is not None
            or request.read_scope is not None
        ):
            return "create_item requests cannot include update fields, change_digest or target_build_number"
    elif request.action == "update_item":
        if request.item_type is None or request.template is None or not request.fields:
            return "update_item requests require item_type, template and fields"
        if request.target_build_number is not None or request.read_scope is not None:
            return "update_item requests cannot include a target build number or read scope"
        if not _is_sha256_digest(request.change_digest) or not _is_sha256_digest(request.base_config_digest):
            return "update_item requests require normalized change and base configuration digests"
    elif request.action == "trigger_build":
        if (
            request.item_type is not None
            or request.template is not None
            or request.fields
            or request.change_digest is not None
            or request.target_build_number is not None
            or request.base_config_digest is not None
            or request.read_scope is not None
        ):
            return "trigger_build requests cannot include item type, template, fields, change_digest or target_build_number"
    elif request.action == "cancel_build":
        if (
            request.item_type is not None
            or request.template is not None
            or request.fields
            or request.parameters
            or request.change_digest is not None
            or request.base_config_digest is not None
            or request.read_scope is not None
            or not _is_positive_integer(request.target_build_number)
        ):
            return "cancel_build requests require a target build number and cannot include item type, template, fields or parameters"
    return None


def _is_sha256_digest(value: str | None) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _parse_rule(raw: Any) -> PolicyRule:
    if not isinstance(raw, Mapping):
        raise PolicyError("Jenkins policy rules must be mappings")
    required = {"name", "action", "controllers", "environments", "path_prefixes"}
    optional = {
        "item_types",
        "templates",
        "allowed_fields",
        "parameters",
        "max_concurrent",
        "expires_at",
        "read_scopes",
    }
    missing = required - set(raw)
    unknown = set(raw) - required - optional
    if missing or unknown:
        raise PolicyError(
            f"policy rule missing {sorted(missing)} or contains unknown fields {sorted(unknown)}"
        )

    name = _string(raw["name"], "policy rule name")
    action = _string(raw["action"], f"policy rule {name!r} action")
    if action not in ALLOWED_ACTIONS:
        raise PolicyError(f"policy rule {name!r} has unsupported action {action!r}")
    controllers = _string_set(raw["controllers"], f"policy rule {name!r} controllers")
    environments = _string_set(raw["environments"], f"policy rule {name!r} environments")
    path_prefixes = tuple(
        _required_path_prefix(value, f"policy rule {name!r} path_prefixes")
        for value in _string_list(raw["path_prefixes"], f"policy rule {name!r} path_prefixes")
    )
    item_types = _optional_string_set(raw.get("item_types"), f"policy rule {name!r} item_types")
    templates = _optional_string_set(raw.get("templates"), f"policy rule {name!r} templates")
    allowed_fields = _optional_string_set(
        raw.get("allowed_fields"),
        f"policy rule {name!r} allowed_fields",
    )
    parameters = _parse_parameters(raw.get("parameters"), name)
    expires_at = _parse_expiry(raw.get("expires_at"), name)
    max_concurrent = raw.get("max_concurrent")
    read_scopes = _optional_string_set(raw.get("read_scopes"), f"policy rule {name!r} read_scopes")
    if max_concurrent is not None and (
        not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool) or max_concurrent < 1
    ):
        raise PolicyError(f"policy rule {name!r} max_concurrent must be a positive integer")
    _validate_write_rule_constraints(
        name=name,
        action=action,
        item_types=item_types,
        templates=templates,
        allowed_fields=allowed_fields,
        parameters=parameters,
        expires_at=expires_at,
        max_concurrent=max_concurrent,
        read_scopes=read_scopes,
    )
    return PolicyRule(
        name=name,
        action=action,
        controllers=controllers,
        environments=environments,
        path_prefixes=path_prefixes,
        item_types=item_types,
        templates=templates,
        allowed_fields=allowed_fields,
        parameters=parameters,
        expires_at=expires_at,
        max_concurrent=max_concurrent,
        read_scopes=read_scopes,
    )


def _parameters_match(
    supplied: Mapping[str, str],
    allowed: Mapping[str, frozenset[str]] | None,
) -> bool:
    if allowed is None:
        return not supplied
    if set(supplied) != set(allowed):
        return False
    return all(value in allowed[name] for name, value in supplied.items())


def _parse_parameters(raw: Any, name: str) -> Mapping[str, frozenset[str]] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PolicyError(f"policy rule {name!r} parameters must be a mapping")
    result: dict[str, frozenset[str]] = {}
    for parameter_name, schema in raw.items():
        parameter = _string(parameter_name, f"policy rule {name!r} parameter name")
        if not isinstance(schema, Mapping) or set(schema) != {"enum"}:
            raise PolicyError(f"policy rule {name!r} parameter {parameter!r} must use enum")
        result[parameter] = _string_set(
            schema["enum"],
            f"policy rule {name!r} parameter {parameter!r} enum",
        )
    return result


def _validate_write_rule_constraints(
    *,
    name: str,
    action: str,
    item_types: frozenset[str] | None,
    templates: frozenset[str] | None,
    allowed_fields: frozenset[str] | None,
    parameters: Mapping[str, frozenset[str]] | None,
    expires_at: datetime | None,
    max_concurrent: Any,
    read_scopes: frozenset[str] | None,
) -> None:
    if action == "read":
        _reject_rule_fields(
            name,
            action,
            templates=templates,
            allowed_fields=allowed_fields,
            parameters=parameters,
            max_concurrent=max_concurrent,
        )
        if read_scopes is not None and not read_scopes <= {"item", "controller", "root_list", "nodes"}:
            raise PolicyError(f"read policy rule {name!r} has an unsupported read scope")
        return
    if expires_at is None:
        raise PolicyError(f"write policy rule {name!r} requires expires_at")
    if action == "create_item":
        if item_types is None or templates is None:
            raise PolicyError(f"create_item policy rule {name!r} requires item_types and templates")
        _reject_rule_fields(name, action, allowed_fields=allowed_fields, max_concurrent=max_concurrent, read_scopes=read_scopes)
    elif action == "update_item":
        if item_types is None or templates is None or allowed_fields is None:
            raise PolicyError(
                f"update_item policy rule {name!r} requires item_types, templates and allowed_fields"
            )
        _reject_rule_fields(name, action, max_concurrent=max_concurrent, read_scopes=read_scopes)
    elif action == "trigger_build":
        if parameters is None or max_concurrent is None:
            raise PolicyError(
                f"trigger_build policy rule {name!r} requires an explicit parameter schema and max_concurrent"
            )
        _reject_rule_fields(
            name,
            action,
            item_types=item_types,
            templates=templates,
            allowed_fields=allowed_fields,
            read_scopes=read_scopes,
        )
    elif action == "cancel_build":
        if max_concurrent is None:
            raise PolicyError(f"cancel_build policy rule {name!r} requires max_concurrent")
        _reject_rule_fields(
            name,
            action,
            item_types=item_types,
            templates=templates,
            allowed_fields=allowed_fields,
            parameters=parameters,
            read_scopes=read_scopes,
        )


def _reject_rule_fields(
    name: str,
    action: str,
    *,
    item_types: frozenset[str] | None = None,
    templates: frozenset[str] | None = None,
    allowed_fields: frozenset[str] | None = None,
    parameters: Mapping[str, frozenset[str]] | None = None,
    max_concurrent: int | None = None,
    read_scopes: frozenset[str] | None = None,
) -> None:
    present = [
        field
        for field, value in {
            "item_types": item_types,
            "templates": templates,
            "allowed_fields": allowed_fields,
            "parameters": parameters,
            "max_concurrent": max_concurrent,
            "read_scopes": read_scopes,
        }.items()
        if value is not None
    ]
    if present:
        raise PolicyError(
            f"{action} policy rule {name!r} does not permit {', '.join(present)}"
        )


def _parse_expiry(raw: Any, name: str) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise PolicyError(f"policy rule {name!r} expires_at must be an ISO timestamp")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise PolicyError(f"policy rule {name!r} expires_at must be an ISO timestamp") from None
    if value.tzinfo is None or value.utcoffset() is None:
        raise PolicyError(f"policy rule {name!r} expires_at must include a timezone")
    return value.astimezone(UTC)


def _normalize_item_path(value: str) -> str | None:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        return None
    parts = value.split("/")
    if any(not part or part in {".", ".."} or any(character.isspace() for character in part[:1]) for part in parts):
        return None
    return "/".join(parts)


def _required_path_prefix(value: str, field: str) -> str:
    normalized = _normalize_item_path(value)
    if normalized is None:
        raise PolicyError(f"{field} contains an invalid item path prefix")
    return normalized


def _path_is_within(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(f"{prefix}/")


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(f"{field} must be a nonblank string")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise PolicyError(f"{field} must be a nonempty list of strings")
    return [_string(item, field) for item in value]


def _string_set(value: Any, field: str) -> frozenset[str]:
    values = _string_list(value, field)
    if len(values) != len(set(values)):
        raise PolicyError(f"{field} must not contain duplicates")
    return frozenset(values)


def _optional_string_set(value: Any, field: str) -> frozenset[str] | None:
    return None if value is None else _string_set(value, field)
