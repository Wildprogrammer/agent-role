from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping

from agent_workflow_hub.confirmation import canonical_request_fingerprint


ALLOWED_ACTIONS = frozenset(
    {
        "read",
        "create_item",
        "update_item",
        "trigger_build",
        "cancel_build",
    }
)


@dataclass(frozen=True)
class ControllerConfig:
    name: str
    url: str
    environment: str
    username_env: str | None = None
    token_env: str | None = None
    allow_insecure_http: bool = False
    require_crumb: bool = False
    ca_bundle: Path | None = None
    username: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)
    confirm_writes: bool = True


@dataclass(frozen=True)
class JenkinsConfig:
    controllers: Mapping[str, ControllerConfig]
    policy_path: Path | None = None


@dataclass(frozen=True)
class JenkinsCredentials:
    username: str = field(repr=False)
    token: str = field(repr=False)


@dataclass(frozen=True)
class OperationRequest:
    controller: str
    action: str
    item_path: str
    item_type: str | None = None
    template: str | None = None
    fields: frozenset[str] = field(default_factory=frozenset)
    parameters: Mapping[str, str] = field(default_factory=dict)
    change_digest: str | None = None
    target_build_number: int | None = None
    base_config_digest: str | None = None
    read_scope: str | None = None
    confirmation_details: Mapping[str, object] = field(
        default_factory=dict, compare=False, repr=False
    )


def request_fingerprint(request: OperationRequest) -> str:
    return canonical_request_fingerprint(operation_request_mapping(request))


def operation_request_mapping(request: OperationRequest) -> dict[str, object]:
    return {
        "action": request.action,
        "controller": request.controller,
        "fields": sorted(request.fields),
        "item_path": request.item_path,
        "item_type": request.item_type,
        "parameters": dict(request.parameters),
        "template": request.template,
        "change_digest": request.change_digest,
        "target_build_number": request.target_build_number,
        "base_config_digest": request.base_config_digest,
        "read_scope": request.read_scope,
    }


@dataclass(frozen=True)
class PolicyRule:
    name: str
    action: str
    controllers: frozenset[str]
    environments: frozenset[str]
    path_prefixes: tuple[str, ...]
    item_types: frozenset[str] | None = None
    templates: frozenset[str] | None = None
    allowed_fields: frozenset[str] | None = None
    parameters: Mapping[str, frozenset[str]] | None = None
    expires_at: datetime | None = None
    max_concurrent: int | None = None
    read_scopes: frozenset[str] | None = None


@dataclass(frozen=True)
class Policy:
    rules: tuple[PolicyRule, ...]


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    requires_confirmation: bool
    reason: str
    rule_name: str | None = None
    max_concurrent: int | None = None
    controller: str | None = None
    action: str | None = None
    request_fingerprint: str | None = None


@dataclass(frozen=True)
class WritePermit:
    permit_id: str
    controller: str
    action: str
    item_path: str
    item_type: str
    template: str
    expires_at: datetime
    payload_digest: str | None = None
    base_config_digest: str | None = None
