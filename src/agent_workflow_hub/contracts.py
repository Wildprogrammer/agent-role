from __future__ import annotations

import base64
import binascii
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast
from urllib.parse import urlparse

from .support import PROJECT_HOSTS

ALLOWED_SKILL_FIELDS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
JSON_METADATA_FIELDS = {
    "execution-modes",
    "required-capabilities",
    "capability-slots",
    "roles",
    "supported-hosts",
    "isolation-required-steps",
    "config-templates",
    "config-requirements",
    "entrypoints",
}
LIST_METADATA_FIELDS = {
    "required-capabilities",
    "roles",
    "supported-hosts",
    "isolation-required-steps",
}
EXECUTION_MODES = {"single-agent", "multi-agent"}
CAPABILITY_FIELDS = {
    "spec_version",
    "id",
    "type",
    "locked_version",
    "official_source",
    "official_docs",
    "license",
    "last_verified",
    "recommended_version",
    "integrity",
    "systems",
    "hosts",
    "detect",
    "permissions",
    "network",
    "data_access",
    "installation",
}
CAPABILITY_HOSTS = PROJECT_HOSTS
CAPABILITY_HOST_STATES = {
    "verified",
    "documented",
    "conditional",
    "unverified",
    "unsupported",
}
INSTALLATION_POLICIES = {"agent-managed", "user-managed"}
INSTALLATION_SCOPES = {
    "workspace-shared",
    "workspace-workflow",
    "global-runtime",
    "system",
}
INSTALLATION_METHODS = {
    "existing",
    "manual",
    "official-artifact",
    "package-manager",
    "pip",
    "uv",
    "npm",
    "git",
}
AGENT_MANAGED_METHODS = {"existing", "official-artifact", "pip", "uv", "npm", "git"}
AGENT_MANAGED_GLOBAL_METHODS = {"existing", "pip", "uv", "npm"}
USER_MANAGED_METHODS = INSTALLATION_METHODS
FLOATING_VERSION_COMPONENTS = {
    "latest",
    "snapshot",
    "trunk",
    "vnext",
    "main",
    "master",
    "head",
    "develop",
    "development",
    "nightly",
    "stable",
}
IMMUTABLE_LOCKED_VERSION_TOKEN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:@#+-]*",
    re.ASCII,
)
GIT_LOCKED_VERSION = re.compile(
    r"git:(?P<digest>[0-9a-f]{40})",
    re.ASCII | re.IGNORECASE,
)
SHA256_LOCKED_VERSION = re.compile(
    r"sha256:(?P<digest>[0-9a-f]{64})",
    re.ASCII | re.IGNORECASE,
)
IMMUTABLE_DIGEST_TOKEN = re.compile(
    r"(?:"
    r"git:[0-9a-fA-F]{40}"
    r"|sha256:[0-9a-fA-F]{64}"
    r"|[0-9a-fA-F]{40}"
    r"|[0-9a-fA-F]{64}"
    r")",
    re.ASCII | re.IGNORECASE,
)
LOCKED_VERSION_COMPONENT_SEPARATOR = re.compile(r"[._:@#+-]+", re.ASCII)
SUPPORTED_CAPABILITY_SPEC_VERSION = "1.0"
GIT_COMMIT = re.compile(r"[0-9a-fA-F]{40}")
SHA256 = re.compile(r"[0-9a-fA-F]{64}")
VERSION_REQUIREMENT = re.compile(r"^>=\d+\.\d+\.\d+$", re.ASCII)
RECOMMENDED_VERSION = re.compile(r"^\d+\.\d+\.\d+$", re.ASCII)


class ContractError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigRequirement:
    scope: Literal["repository-external"]
    required: bool


@dataclass(frozen=True)
class SkillContract:
    name: str
    description: str
    metadata: Mapping[str, str]
    body: str


@dataclass(frozen=True)
class CapabilityContract:
    id: str
    type: str
    slug: str
    hosts: Mapping[str, str]
    frontmatter: Mapping[str, Any]
    body: str

    @property
    def locked_version(self) -> str:
        return cast(str, self.frontmatter["locked_version"])

    @property
    def version_requirement(self) -> str | None:
        value = self.frontmatter.get("version_requirement")
        return cast("str | None", value)

    @property
    def recommended_version(self) -> str:
        return cast(str, self.frontmatter["recommended_version"])

    @property
    def installation(self) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], self.frontmatter["installation"])


def workflow_config_templates(metadata: Mapping[str, str]) -> Mapping[str, str]:
    value = _parse_json_metadata(
        "config-templates",
        metadata.get("config-templates", "{}"),
    )
    return MappingProxyType(_string_mapping("config-templates", value))


def workflow_config_requirements(
    metadata: Mapping[str, str],
) -> Mapping[str, ConfigRequirement]:
    value = _parse_json_metadata(
        "config-requirements",
        metadata.get("config-requirements", "{}"),
    )
    return MappingProxyType(_config_requirements(value))


def workflow_entrypoints(metadata: Mapping[str, str]) -> Mapping[str, str]:
    value = _parse_json_metadata(
        "entrypoints",
        metadata.get("entrypoints", "{}"),
    )
    return MappingProxyType(_validated_entrypoints(value))


def _validated_entrypoints(value: Any) -> dict[str, str]:
    entrypoints = _string_mapping("entrypoints", value)
    if any(
        _has_entrypoint_control_or_connector(key)
        or _has_entrypoint_control_or_connector(command)
        for key, command in entrypoints.items()
    ):
        raise ContractError(
            "metadata.entrypoints keys and values must not contain control "
            "characters or shell command connectors"
        )
    return entrypoints


def _string_mapping(field: str, value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str)
        and bool(key.strip())
        and isinstance(item, str)
        and bool(item.strip())
        for key, item in value.items()
    ):
        raise ContractError(
            f"metadata.{field} must be an object mapping nonblank strings "
            "to nonblank strings"
        )
    return dict(value)


def _config_requirements(value: Any) -> dict[str, ConfigRequirement]:
    if not isinstance(value, dict):
        raise ContractError(
            "metadata.config-requirements must be an object mapping labels "
            "to closed requirement objects"
        )
    requirements: dict[str, ConfigRequirement] = {}
    for label, requirement in value.items():
        if (
            not isinstance(label, str)
            or not label.strip()
            or not isinstance(requirement, dict)
            or set(requirement) != {"scope", "required"}
            or requirement.get("scope") != "repository-external"
            or not isinstance(requirement.get("required"), bool)
        ):
            raise ContractError(
                "metadata.config-requirements must map nonblank labels to "
                "objects containing only scope=repository-external and a "
                "boolean required value"
            )
        requirements[label] = ConfigRequirement(
            scope="repository-external",
            required=requirement["required"],
        )
    return requirements


def _has_entrypoint_control_or_connector(value: str) -> bool:
    return any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
        or character in ";&|`"
        for character in value
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {value}")


def _parse_json_metadata(field: str, value: str) -> Any:
    try:
        return json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractError(f"metadata.{field} must be valid JSON") from exc


def _is_unique_string_list(value: Any, *, nonempty: bool = False) -> bool:
    return (
        isinstance(value, list)
        and (not nonempty or bool(value))
        and all(isinstance(item, str) for item in value)
        and len(value) == len(set(value))
    )


def _validate_json_metadata(field: str, value: Any) -> None:
    if field == "execution-modes":
        if (
            not _is_unique_string_list(value, nonempty=True)
            or not set(value) <= EXECUTION_MODES
        ):
            raise ContractError(
                "metadata.execution-modes must be a non-empty list of unique "
                "single-agent or multi-agent values"
            )
    elif field in LIST_METADATA_FIELDS:
        if not _is_unique_string_list(value):
            raise ContractError(f"metadata.{field} must be a list of unique strings")
    elif field == "capability-slots":
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and _is_unique_string_list(items, nonempty=True)
            for key, items in value.items()
        ):
            raise ContractError(
                "metadata.capability-slots must be an object mapping strings "
                "to non-empty lists of unique strings"
            )
    elif field == "config-templates":
        _string_mapping(field, value)
    elif field == "config-requirements":
        _config_requirements(value)
    elif field == "entrypoints":
        _validated_entrypoints(value)


def _is_nonblank_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_locked_version(value: Any) -> None:
    if (
        not _is_nonblank_string(value)
        or value != value.strip()
        or any(character.isspace() for character in value)
        or not IMMUTABLE_LOCKED_VERSION_TOKEN.fullmatch(value)
    ):
        raise ContractError(
            "locked_version must be an immutable portable ASCII token"
        )
    normalized = value.casefold()
    if (
        normalized.startswith("git:")
        and not GIT_LOCKED_VERSION.fullmatch(value)
    ) or (
        normalized.startswith("sha256:")
        and not SHA256_LOCKED_VERSION.fullmatch(value)
    ):
        raise ContractError(
            "locked_version must be an immutable exact digest token"
        )
    components = LOCKED_VERSION_COMPONENT_SEPARATOR.split(normalized)
    if (
        any(
            component in FLOATING_VERSION_COMPONENTS or component == "x"
            for component in components
        )
        or (
            not any("0" <= character <= "9" for character in value)
            and not IMMUTABLE_DIGEST_TOKEN.fullmatch(value)
        )
    ):
        raise ContractError(
            "locked_version must be an immutable exact version token"
        )


def _validate_https_url(field: str, value: Any) -> None:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be an https URL")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        _ = parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ContractError(f"{field} must be an https URL") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or any(character.isspace() for character in parsed.netloc)
        or any(character.isspace() for character in hostname)
    ):
        raise ContractError(f"{field} must be an https URL")


def _is_nonempty_string_mapping(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and bool(value)
        and all(
            _is_nonblank_string(key) and _is_nonblank_string(item)
            for key, item in value.items()
        )
    )


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_nonblank_string(item) for item in value)


def _validate_integrity(
    integrity: Any,
    locked_version: Any,
) -> None:
    if not isinstance(integrity, Mapping) or not all(
        _is_nonblank_string(integrity.get(field)) for field in ("method", "value")
    ):
        raise ContractError(
            "integrity must contain nonblank string method and value fields"
        )

    method = integrity["method"]
    value = integrity["value"]
    git_lock = (
        GIT_LOCKED_VERSION.fullmatch(locked_version)
        if isinstance(locked_version, str)
        else None
    )
    sha256_lock = (
        SHA256_LOCKED_VERSION.fullmatch(locked_version)
        if isinstance(locked_version, str)
        else None
    )
    if sha256_lock is not None and method != "sha256":
        raise ContractError(
            "integrity.method must be sha256 for a sha256-prefixed "
            "locked_version"
        )
    if method == "git-commit":
        if not GIT_COMMIT.fullmatch(value):
            raise ContractError(
                "integrity.value must be exactly 40 hexadecimal characters "
                "for git-commit"
            )
        if (
            git_lock is None
            or git_lock.group("digest").casefold() != value.casefold()
        ):
            raise ContractError(
                "locked_version must equal git:<integrity.value> for git-commit"
            )
    elif method == "sha256":
        if not SHA256.fullmatch(value):
            raise ContractError(
                "integrity.value must be exactly 64 hexadecimal characters "
                "for sha256"
            )
        if (
            not isinstance(integrity.get("locked_version"), str)
            or integrity["locked_version"] != locked_version
        ):
            raise ContractError(
                "integrity.locked_version must exactly equal locked_version "
                "for sha256"
            )
        if (
            sha256_lock is not None
            and sha256_lock.group("digest").casefold() != value.casefold()
        ):
            raise ContractError(
                "integrity.value must equal the digest in a sha256-prefixed "
                "locked_version"
            )
    elif method == "npm-sha512":
        if (
            not isinstance(integrity.get("locked_version"), str)
            or integrity["locked_version"] != locked_version
        ):
            raise ContractError(
                "integrity.locked_version must exactly equal locked_version "
                "for npm-sha512"
            )
        prefix = "sha512-"
        if not value.startswith(prefix):
            raise ContractError(
                "integrity.value must use a sha512- prefixed base64 digest "
                "for npm-sha512"
            )
        encoded_digest = value.removeprefix(prefix)
        try:
            digest = base64.b64decode(encoded_digest, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ContractError(
                "integrity.value must contain valid base64 for npm-sha512"
            ) from exc
        if (
            len(digest) != 64
            or base64.b64encode(digest).decode("ascii") != encoded_digest
        ):
            raise ContractError(
                "integrity.value must contain one canonical 512-bit base64 "
                "digest for npm-sha512"
            )
    else:
        raise ContractError(
            "integrity.method must be git-commit, sha256, or npm-sha512 "
            "for spec_version 1.0"
        )


def _validate_capability_fields(frontmatter: Mapping[str, Any]) -> None:
    for field in ("spec_version", "license"):
        if not _is_nonblank_string(frontmatter[field]):
            raise ContractError(f"{field} must be a nonblank string")
    if frontmatter["spec_version"] != SUPPORTED_CAPABILITY_SPEC_VERSION:
        raise ContractError(
            "spec_version must equal supported version "
            f"{SUPPORTED_CAPABILITY_SPEC_VERSION}"
        )

    last_verified = frontmatter["last_verified"]
    if not isinstance(last_verified, str):
        raise ContractError("last_verified must be an ISO YYYY-MM-DD string")
    try:
        parsed_date = date.fromisoformat(last_verified)
    except ValueError as exc:
        raise ContractError("last_verified must be an ISO YYYY-MM-DD string") from exc
    if parsed_date.isoformat() != last_verified:
        raise ContractError("last_verified must be an ISO YYYY-MM-DD string")

    for field in ("official_source", "official_docs"):
        _validate_https_url(field, frontmatter[field])

    _validate_integrity(frontmatter["integrity"], frontmatter["locked_version"])
    _validate_version_compatibility(
        frontmatter.get("version_requirement"),
        frontmatter["recommended_version"],
    )

    systems = frontmatter["systems"]
    if not isinstance(systems, Mapping) or not all(
        _is_nonempty_string_mapping(systems.get(field)) for field in ("os", "arch")
    ):
        raise ContractError(
            "systems must contain nonempty string mappings for os and arch"
        )
    for field in ("runtimes", "hardware"):
        if field in systems and not _is_string_list(systems[field]):
            raise ContractError(f"systems.{field} must be a list of nonblank strings")

    detect = frontmatter["detect"]
    if not isinstance(detect, Mapping) or detect.get("mode") != "read-only":
        raise ContractError("detect.mode must be read-only")
    if "command" in detect and not _is_nonblank_string(detect["command"]):
        raise ContractError("detect.command must be a nonblank string")

    for field in ("permissions", "data_access"):
        if not _is_string_list(frontmatter[field]):
            raise ContractError(f"{field} must be a list of nonblank strings")

    network = frontmatter["network"]
    if (
        not isinstance(network, Mapping)
        or not isinstance(network.get("required_for_install"), bool)
    ):
        raise ContractError(
            "network.required_for_install must be present and boolean"
        )
    if "required_for_core_use" in network and not isinstance(
        network["required_for_core_use"], bool
    ):
        raise ContractError("network.required_for_core_use must be boolean")

    _validate_installation(frontmatter["installation"])


def _validate_version_compatibility(
    version_requirement: Any, recommended_version: Any
) -> None:
    if version_requirement is not None:
        if (
            not _is_nonblank_string(version_requirement)
            or not VERSION_REQUIREMENT.fullmatch(version_requirement)
        ):
            raise ContractError(
                "version_requirement must be a >=X.Y.Z minimum version requirement"
            )
    if not _is_nonblank_string(recommended_version) or not RECOMMENDED_VERSION.fullmatch(
        recommended_version
    ):
        raise ContractError(
            "recommended_version must be an exact X.Y.Z version"
        )


def _validate_installation(installation: Any) -> None:
    if not isinstance(installation, Mapping):
        raise ContractError("installation must be a mapping")

    required = {"policy", "scope", "methods"}
    missing = required - set(installation)
    if missing:
        raise ContractError(
            f"installation missing required fields: {sorted(missing)}"
        )

    policy = installation["policy"]
    scope = installation["scope"]
    methods = installation["methods"]
    if not _is_nonblank_string(policy):
        raise ContractError("installation.policy must be a nonblank string")
    if not _is_nonblank_string(scope):
        raise ContractError("installation.scope must be a nonblank string")
    if policy not in INSTALLATION_POLICIES:
        raise ContractError(
            "installation.policy must be agent-managed or user-managed"
        )
    if scope not in INSTALLATION_SCOPES:
        raise ContractError(
            "installation.scope must be workspace-shared, workspace-workflow, "
            "global-runtime, or system"
        )
    if not _is_string_list(methods) or len(set(methods)) != len(methods):
        raise ContractError(
            "installation.methods must be a unique list of nonblank strings"
        )
    unknown_methods = set(methods) - INSTALLATION_METHODS
    if unknown_methods:
        raise ContractError(
            f"installation.methods contains unsupported values: {sorted(unknown_methods)}"
        )
    if "existing" not in methods:
        raise ContractError("installation.methods must include existing for detection")

    if policy == "agent-managed":
        if scope == "system":
            raise ContractError(
                "agent-managed installation.scope must stay inside the workspace"
            )
        allowed_methods = (
            AGENT_MANAGED_GLOBAL_METHODS
            if scope == "global-runtime"
            else AGENT_MANAGED_METHODS
        )
        disallowed = set(methods) - allowed_methods
        if disallowed:
            raise ContractError(
                "agent-managed installation.methods cannot include "
                f"{sorted(disallowed)}"
            )
        return

    disallowed = set(methods) - USER_MANAGED_METHODS
    if disallowed:
        raise ContractError(
            "user-managed installation.methods cannot include " f"{sorted(disallowed)}"
        )


def _freeze(value: Any, active: set[int] | None = None) -> Any:
    if not isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return value

    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        raise ContractError("recursive or cyclic contract data is not allowed")
    active.add(identity)
    try:
        if isinstance(value, Mapping):
            return MappingProxyType(
                {key: _freeze(item, active) for key, item in value.items()}
            )
        if isinstance(value, (list, tuple)):
            return tuple(_freeze(item, active) for item in value)
        return frozenset(_freeze(item, active) for item in value)
    finally:
        active.remove(identity)


def validate_skill(
    path: Path, frontmatter: dict[str, Any], body: str
) -> SkillContract:
    if not all(isinstance(field, str) for field in frontmatter):
        raise ContractError("top-level field names must be strings")
    unexpected = set(frontmatter) - ALLOWED_SKILL_FIELDS
    if unexpected:
        raise ContractError(f"Unexpected fields: {sorted(unexpected)}")
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 64
        or not SKILL_NAME.fullmatch(name)
    ):
        raise ContractError("name must be a lowercase hyphenated slug of 1-64 characters")
    if path.parent.name != name:
        raise ContractError("Skill name must match directory")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > 1024
    ):
        raise ContractError("description must be nonblank and at most 1024 characters")
    if "license" in frontmatter and not isinstance(frontmatter["license"], str):
        raise ContractError("license must be a string")
    if "compatibility" in frontmatter and (
        not isinstance(frontmatter["compatibility"], str)
        or not frontmatter["compatibility"].strip()
        or len(frontmatter["compatibility"]) > 500
    ):
        raise ContractError("compatibility must be nonblank and at most 500 characters")
    if "allowed-tools" in frontmatter and not isinstance(
        frontmatter["allowed-tools"], str
    ):
        raise ContractError("allowed-tools must be a space-separated string")
    metadata = frontmatter.get("metadata", {})
    if not isinstance(metadata, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in metadata.items()
    ):
        raise ContractError("metadata must be a string-to-string mapping")
    for field in JSON_METADATA_FIELDS & set(metadata):
        value = _parse_json_metadata(field, metadata[field])
        _validate_json_metadata(field, value)
    if not body.strip():
        raise ContractError("Skill body must not be empty")
    frozen_metadata = cast(Mapping[str, str], _freeze(metadata))
    return SkillContract(name, description, frozen_metadata, body)


def validate_capability(
    path: Path, frontmatter: Mapping[str, Any], body: str
) -> CapabilityContract:
    capability_id = frontmatter.get("id")
    capability_type = frontmatter.get("type")
    slug = path.parent.name
    if not isinstance(capability_id, str) or not capability_id.strip():
        raise ContractError("Capability id must be a nonblank string")
    if not isinstance(capability_type, str) or not capability_type.strip():
        raise ContractError("Capability type must be a nonblank string")
    if (
        path.name != "CAPABILITY.md"
        or path.parent.parent.parent.name != "capabilities"
        or not 1 <= len(capability_type) <= 64
        or not SKILL_NAME.fullmatch(capability_type)
        or not 1 <= len(slug) <= 64
        or not SKILL_NAME.fullmatch(slug)
        or path.parent.parent.name != capability_type
        or capability_id != f"{capability_type}.{slug}"
    ):
        raise ContractError(
            "Capability id must match canonical path "
            "capabilities/<type>/<slug>/CAPABILITY.md"
        )

    fields = set(frontmatter)
    missing = CAPABILITY_FIELDS - fields
    if missing:
        raise ContractError(f"Capability missing required fields: {sorted(missing)}")

    _validate_locked_version(frontmatter["locked_version"])
    _validate_capability_fields(frontmatter)

    hosts = frontmatter["hosts"]
    if not isinstance(hosts, Mapping) or set(hosts) != CAPABILITY_HOSTS:
        raise ContractError("hosts must be an exact five-host matrix")
    if not all(
        isinstance(state, str) and state in CAPABILITY_HOST_STATES
        for state in hosts.values()
    ):
        raise ContractError(
            "host states must be strings using an allowed support state"
        )
    if not isinstance(body, str) or not body.strip():
        raise ContractError("Capability body must not be empty")

    frozen_frontmatter = cast(Mapping[str, Any], _freeze(frontmatter))
    frozen_hosts = cast(Mapping[str, str], frozen_frontmatter["hosts"])
    return CapabilityContract(
        capability_id,
        capability_type,
        slug,
        frozen_hosts,
        frozen_frontmatter,
        body,
    )
