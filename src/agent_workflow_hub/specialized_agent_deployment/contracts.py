"""Finite, immutable contracts for specialized agent deployments."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Literal, cast

_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HOSTS = frozenset(("hermes", "deepseek-harness"))
_MODES = frozenset(("create", "update"))
_SOURCE_KINDS = frozenset(("hub-workflow", "external-skill"))
_COMPATIBILITY = frozenset(
    ("verified", "conditional", "unverified", "missing", "compatible_not_runnable")
)
_WRITE_ACTIONS = frozenset(("create", "update", "command", "config-set", "config-unset"))
_MANIFEST_STATUSES = frozenset(
    (
        "planned",
        "guidance_only",
        "applying",
        "applied",
        "verified",
        "partially_verified",
        "failed",
        "rolled_back",
        "outcome_unknown",
    )
)
_VERIFICATION_STATUSES = frozenset(
    ("verified", "partially_verified", "failed", "outcome_unknown")
)
_ENABLEMENT_STATUSES = frozenset(
    ("not_requested", "verified", "partially_ready", "rolled_back", "outcome_unknown")
)
_ENABLEMENT_CHECK_STATUSES = frozenset(("passed", "failed", "not_checked"))
_PLATFORM_RE = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")


class DeploymentContractError(ValueError):
    """Raised when deployment data is outside its closed contract."""


def _clone_finite_json(
    value: object,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> object:
    if _depth > 64:
        raise DeploymentContractError("JSON value is too deeply nested")
    if value is None or type(value) in (bool, str):
        if type(value) is str:
            try:
                cast(str, value).encode("utf-8")
            except UnicodeEncodeError:
                raise DeploymentContractError("string is not valid UTF-8") from None
        return value
    if type(value) is int:
        if cast(int, value).bit_length() > 14_000:
            raise DeploymentContractError("integer is too large")
        return value
    if type(value) is float:
        if not math.isfinite(cast(float, value)):
            raise DeploymentContractError("JSON numbers must be finite")
        return value
    if type(value) in (bytes, bytearray, set, frozenset, Path):
        raise DeploymentContractError("value is not finite JSON")

    if _seen is None:
        _seen = set()
    identity = id(value)
    if identity in _seen:
        raise DeploymentContractError("recursive JSON value")
    if type(value) in (list, tuple):
        _seen.add(identity)
        try:
            return [
                _clone_finite_json(item, _seen=_seen, _depth=_depth + 1)
                for item in cast(Sequence[object], value)
            ]
        finally:
            _seen.remove(identity)
    if isinstance(value, Mapping):
        _seen.add(identity)
        try:
            result: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise DeploymentContractError("JSON object keys must be strings")
                result[key] = _clone_finite_json(
                    item, _seen=_seen, _depth=_depth + 1
                )
            return result
        finally:
            _seen.remove(identity)
    raise DeploymentContractError("value is not finite JSON")


def _freeze(value: object) -> object:
    cloned = _clone_finite_json(value)
    return _freeze_cloned(cloned)


def _freeze_cloned(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_cloned(item) for key, item in cast(dict[str, object], value).items()}
        )
    if type(value) is list:
        return tuple(_freeze_cloned(item) for item in cast(list[object], value))
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if type(value) in (tuple, list):
        return [_thaw(item) for item in cast(Sequence[object], value)]
    return value


def _mapping(
    value: object,
    fields: set[str],
    *,
    optional: set[str] | None = None,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DeploymentContractError("expected an object")
    keys = set(value)
    optional = optional or set()
    required = fields - optional
    if keys - fields:
        raise DeploymentContractError(f"unknown fields: {sorted(keys - fields)!r}")
    if required - keys:
        raise DeploymentContractError(f"missing fields: {sorted(required - keys)!r}")
    return cast(Mapping[str, object], value)


def _string(value: object, name: str, *, blank: bool = False) -> str:
    if type(value) is not str or (not blank and not cast(str, value).strip()):
        raise DeploymentContractError(f"{name} must be a non-blank string")
    try:
        cast(str, value).encode("utf-8")
    except UnicodeEncodeError:
        raise DeploymentContractError(f"{name} must be valid UTF-8") from None
    return cast(str, value)


def _kebab(value: object, name: str) -> str:
    text = _string(value, name)
    if _KEBAB_RE.fullmatch(text) is None:
        raise DeploymentContractError(f"{name} must be kebab-case")
    return text


def _sha256(value: object, name: str) -> str:
    text = _string(value, name)
    if _SHA256_RE.fullmatch(text) is None:
        raise DeploymentContractError(f"{name} must be a lowercase SHA-256")
    return text


def _absolute(value: object, name: str) -> str:
    text = _string(value, name)
    if not Path(text).is_absolute():
        raise DeploymentContractError(f"{name} must be an absolute path")
    return str(Path(text))


def _tuple(value: object, name: str) -> tuple[object, ...]:
    if type(value) not in (list, tuple):
        raise DeploymentContractError(f"{name} must be an array")
    return tuple(cast(Sequence[object], value))


def canonical_sha256(value: object) -> str:
    """Return a stable digest for one finite JSON value."""

    normalized = _clone_finite_json(value)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_json_object(path: Path) -> Mapping[str, object]:
    """Read a strict UTF-8 JSON object and reject non-standard constants."""

    if not isinstance(path, Path):
        raise DeploymentContractError("path must be a Path")

    def reject_constant(value: str) -> object:
        raise DeploymentContractError(f"non-finite JSON constant: {value}")

    try:
        parsed = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=reject_constant,
        )
    except DeploymentContractError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentContractError(f"invalid JSON object: {exc}") from None
    if type(parsed) is not dict:
        raise DeploymentContractError("JSON document must be an object")
    return cast(Mapping[str, object], _clone_finite_json(parsed))


@dataclass(frozen=True, kw_only=True)
class SkillSelection:
    name: str
    source_kind: Literal["hub-workflow", "external-skill"]
    source: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _kebab(self.name, "name"))
        if self.source_kind not in _SOURCE_KINDS:
            raise DeploymentContractError("unsupported source_kind")
        source = _string(self.source, "source")
        if self.source_kind == "external-skill":
            source = _absolute(source, "external Skill source")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "reason", _string(self.reason, "reason"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SkillSelection:
        data = _mapping(value, {"name", "source_kind", "source", "reason"})
        return cls(
            name=cast(str, data["name"]),
            source_kind=cast(Literal["hub-workflow", "external-skill"], data["source_kind"]),
            source=cast(str, data["source"]),
            reason=cast(str, data["reason"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source_kind": self.source_kind,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass(frozen=True, kw_only=True)
class DeploymentRequest:
    schema_version: str
    deployment_id: str
    agent_id: str
    display_name: str
    purpose: str
    host: Literal["hermes", "deepseek-harness"]
    mode: Literal["create", "update"]
    primary_workflow: str
    related_workflows: tuple[SkillSelection, ...]
    auxiliary_skills: tuple[SkillSelection, ...]
    workdir: str
    config_refs: tuple[str, ...]
    host_options: Mapping[str, object]
    runtime: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _string(self.schema_version, "schema_version"))
        object.__setattr__(self, "deployment_id", _kebab(self.deployment_id, "deployment_id"))
        object.__setattr__(self, "agent_id", _kebab(self.agent_id, "agent_id"))
        object.__setattr__(self, "display_name", _string(self.display_name, "display_name"))
        object.__setattr__(self, "purpose", _string(self.purpose, "purpose"))
        if self.host not in _HOSTS:
            raise DeploymentContractError("unsupported host")
        if self.mode not in _MODES:
            raise DeploymentContractError("unsupported mode")
        object.__setattr__(self, "primary_workflow", _kebab(self.primary_workflow, "primary_workflow"))
        related = tuple(self.related_workflows)
        auxiliary = tuple(self.auxiliary_skills)
        if not all(type(item) is SkillSelection for item in related + auxiliary):
            raise DeploymentContractError("Skill selections must be typed")
        names = [item.name for item in related + auxiliary]
        if len(names) != len(set(names)):
            raise DeploymentContractError("Skill selections must be unique")
        if self.primary_workflow in names:
            raise DeploymentContractError("primary workflow cannot be selected again")
        object.__setattr__(self, "related_workflows", related)
        object.__setattr__(self, "auxiliary_skills", auxiliary)
        object.__setattr__(self, "workdir", _absolute(self.workdir, "workdir"))
        refs = tuple(_absolute(item, "config ref") for item in self.config_refs)
        if len(refs) != len(set(refs)):
            raise DeploymentContractError("config refs must be unique")
        object.__setattr__(self, "config_refs", refs)
        frozen = _freeze(self.host_options)
        if not isinstance(frozen, Mapping):
            raise DeploymentContractError("host_options must be an object")
        object.__setattr__(self, "host_options", frozen)
        if self.runtime is not None:
            if not isinstance(self.runtime, Mapping):
                raise DeploymentContractError("runtime must be an object")
            mode = self.runtime.get(
                "mode", "isolated" if "wheelhouse" in self.runtime else "system-source"
            )
            if mode not in {"system-source", "isolated"}:
                raise DeploymentContractError("unsupported runtime mode")
            fields = {"mode", "python", "wheelhouse", "destination"}
            data = _mapping(self.runtime, fields, optional={"mode"})
            normalized = {
                "mode": cast(str, mode),
                "python": _absolute(data["python"], "runtime.python"),
                "destination": _absolute(data["destination"], "runtime.destination"),
            }
            normalized["wheelhouse"] = _absolute(
                data["wheelhouse"], "runtime.wheelhouse"
            )
            object.__setattr__(self, "runtime", MappingProxyType(normalized))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DeploymentRequest:
        fields = {
            "schema_version", "deployment_id", "agent_id", "display_name", "purpose",
            "host", "mode", "primary_workflow", "related_workflows", "auxiliary_skills",
            "workdir", "config_refs", "host_options", "runtime",
        }
        data = _mapping(value, fields, optional={"runtime"})
        related = tuple(
            SkillSelection.from_mapping(cast(Mapping[str, object], item))
            for item in _tuple(data["related_workflows"], "related_workflows")
        )
        auxiliary = tuple(
            SkillSelection.from_mapping(cast(Mapping[str, object], item))
            for item in _tuple(data["auxiliary_skills"], "auxiliary_skills")
        )
        return cls(
            schema_version=cast(str, data["schema_version"]),
            deployment_id=cast(str, data["deployment_id"]),
            agent_id=cast(str, data["agent_id"]),
            display_name=cast(str, data["display_name"]),
            purpose=cast(str, data["purpose"]),
            host=cast(Literal["hermes", "deepseek-harness"], data["host"]),
            mode=cast(Literal["create", "update"], data["mode"]),
            primary_workflow=cast(str, data["primary_workflow"]),
            related_workflows=related,
            auxiliary_skills=auxiliary,
            workdir=cast(str, data["workdir"]),
            config_refs=tuple(cast(str, item) for item in _tuple(data["config_refs"], "config_refs")),
            host_options=cast(Mapping[str, object], data["host_options"]),
            runtime=cast(Mapping[str, str] | None, data.get("runtime")),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "purpose": self.purpose,
            "host": self.host,
            "mode": self.mode,
            "primary_workflow": self.primary_workflow,
            "related_workflows": [item.to_mapping() for item in self.related_workflows],
            "auxiliary_skills": [item.to_mapping() for item in self.auxiliary_skills],
            "workdir": self.workdir,
            "config_refs": list(self.config_refs),
            "host_options": cast(dict[str, object], _thaw(self.host_options)),
            **({"runtime": dict(self.runtime)} if self.runtime is not None else {}),
        }


@dataclass(frozen=True, kw_only=True)
class SkillFile:
    relative_path: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        path = _string(self.relative_path, "relative_path")
        pure = PurePosixPath(path)
        if pure.is_absolute() or path != pure.as_posix() or ".." in pure.parts or "." in pure.parts:
            raise DeploymentContractError("relative_path must be a normalized relative POSIX path")
        object.__setattr__(self, "relative_path", path)
        if type(self.size) is not int or self.size < 0:
            raise DeploymentContractError("size must be a non-negative integer")
        object.__setattr__(self, "sha256", _sha256(self.sha256, "sha256"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SkillFile:
        data = _mapping(value, {"relative_path", "size", "sha256"})
        return cls(
            relative_path=cast(str, data["relative_path"]),
            size=cast(int, data["size"]),
            sha256=cast(str, data["sha256"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {"relative_path": self.relative_path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True, kw_only=True)
class SkillSnapshot:
    selection: SkillSelection
    files: tuple[SkillFile, ...]
    tree_sha256: str

    def __post_init__(self) -> None:
        if type(self.selection) is not SkillSelection:
            raise DeploymentContractError("selection must be typed")
        files = tuple(self.files)
        if not files or not all(type(item) is SkillFile for item in files):
            raise DeploymentContractError("snapshot must contain typed files")
        paths = [item.relative_path for item in files]
        if paths != sorted(paths, key=lambda item: item.encode("utf-8")) or len(paths) != len(set(paths)):
            raise DeploymentContractError("snapshot files must be unique and byte-sorted")
        object.__setattr__(self, "files", files)
        tree_sha256 = _sha256(self.tree_sha256, "tree_sha256")
        expected = canonical_sha256([item.to_mapping() for item in files])
        if tree_sha256 != expected:
            raise DeploymentContractError(
                "tree_sha256 does not match the snapshot file list"
            )
        object.__setattr__(self, "tree_sha256", tree_sha256)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SkillSnapshot:
        data = _mapping(value, {"selection", "files", "tree_sha256"})
        return cls(
            selection=SkillSelection.from_mapping(cast(Mapping[str, object], data["selection"])),
            files=tuple(
                SkillFile.from_mapping(cast(Mapping[str, object], item))
                for item in _tuple(data["files"], "files")
            ),
            tree_sha256=cast(str, data["tree_sha256"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "selection": self.selection.to_mapping(),
            "files": [item.to_mapping() for item in self.files],
            "tree_sha256": self.tree_sha256,
        }


@dataclass(frozen=True, kw_only=True)
class HostFacts:
    host: Literal["hermes", "deepseek-harness"]
    compatibility: str
    version: str | None
    target_root: str | None
    facts: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.host not in _HOSTS:
            raise DeploymentContractError("unsupported host")
        if self.compatibility not in _COMPATIBILITY:
            raise DeploymentContractError("unsupported compatibility")
        if self.version is not None:
            object.__setattr__(self, "version", _string(self.version, "version"))
        if self.target_root is not None:
            object.__setattr__(self, "target_root", _absolute(self.target_root, "target_root"))
        frozen = _freeze(self.facts)
        if not isinstance(frozen, Mapping):
            raise DeploymentContractError("facts must be an object")
        object.__setattr__(self, "facts", frozen)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> HostFacts:
        data = _mapping(value, {"host", "compatibility", "version", "target_root", "facts"})
        return cls(
            host=cast(Literal["hermes", "deepseek-harness"], data["host"]),
            compatibility=cast(str, data["compatibility"]),
            version=cast(str | None, data["version"]),
            target_root=cast(str | None, data["target_root"]),
            facts=cast(Mapping[str, object], data["facts"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "host": self.host,
            "compatibility": self.compatibility,
            "version": self.version,
            "target_root": self.target_root,
            "facts": cast(dict[str, object], _thaw(self.facts)),
        }


@dataclass(frozen=True, kw_only=True)
class WriteIntent:
    target: str
    action: str
    content_sha256: str
    size: int
    description: str
    expected_before_sha256: str | None = None
    parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "target", _absolute(self.target, "write target"))
        if self.action not in _WRITE_ACTIONS:
            raise DeploymentContractError("unsupported write action")
        object.__setattr__(self, "content_sha256", _sha256(self.content_sha256, "content_sha256"))
        if type(self.size) is not int or self.size < 0:
            raise DeploymentContractError("size must be a non-negative integer")
        object.__setattr__(self, "description", _string(self.description, "description"))
        if self.expected_before_sha256 is not None:
            object.__setattr__(
                self,
                "expected_before_sha256",
                _sha256(self.expected_before_sha256, "expected_before_sha256"),
            )
        frozen = _freeze(self.parameters)
        if not isinstance(frozen, Mapping):
            raise DeploymentContractError("write parameters must be an object")
        object.__setattr__(self, "parameters", frozen)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> WriteIntent:
        fields = {
            "target",
            "action",
            "content_sha256",
            "size",
            "description",
            "expected_before_sha256",
            "parameters",
        }
        data = _mapping(
            value,
            fields,
            optional={"expected_before_sha256", "parameters"},
        )
        return cls(
            target=cast(str, data["target"]),
            action=cast(str, data["action"]),
            content_sha256=cast(str, data["content_sha256"]),
            size=cast(int, data["size"]),
            description=cast(str, data["description"]),
            expected_before_sha256=cast(
                str | None, data.get("expected_before_sha256")
            ),
            parameters=cast(Mapping[str, object], data.get("parameters", {})),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "target": self.target,
            "action": self.action,
            "content_sha256": self.content_sha256,
            "size": self.size,
            "description": self.description,
            "expected_before_sha256": self.expected_before_sha256,
            "parameters": cast(dict[str, object], _thaw(self.parameters)),
        }


@dataclass(frozen=True, kw_only=True)
class DeploymentPlan:
    schema_version: str
    request: DeploymentRequest
    request_sha256: str
    snapshots: tuple[SkillSnapshot, ...]
    persona: str
    persona_sha256: str
    host_facts: HostFacts
    writes: tuple[WriteIntent, ...]
    generated_at: str | None
    plan_sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _string(self.schema_version, "schema_version"))
        if type(self.request) is not DeploymentRequest:
            raise DeploymentContractError("request must be typed")
        request_sha = _sha256(self.request_sha256, "request_sha256")
        if request_sha != canonical_sha256(self.request.to_mapping()):
            raise DeploymentContractError("request_sha256 does not match request")
        snapshots = tuple(self.snapshots)
        if not snapshots or not all(type(item) is SkillSnapshot for item in snapshots):
            raise DeploymentContractError("plan must contain typed snapshots")
        names = [item.selection.name for item in snapshots]
        if len(names) != len(set(names)):
            raise DeploymentContractError("snapshot names must be unique")
        expected_names = [
            self.request.primary_workflow,
            *(item.name for item in self.request.related_workflows),
            *(item.name for item in self.request.auxiliary_skills),
        ]
        if names != expected_names:
            raise DeploymentContractError(
                "snapshots must exactly follow the requested Skill order"
            )
        object.__setattr__(self, "snapshots", snapshots)
        persona = _string(self.persona, "persona")
        persona_digest = hashlib.sha256(persona.encode("utf-8")).hexdigest()
        if _sha256(self.persona_sha256, "persona_sha256") != persona_digest:
            raise DeploymentContractError("persona_sha256 does not match persona")
        if type(self.host_facts) is not HostFacts or self.host_facts.host != self.request.host:
            raise DeploymentContractError("host facts do not match request")
        writes = tuple(self.writes)
        if not all(type(item) is WriteIntent for item in writes):
            raise DeploymentContractError("plan writes must be typed")
        guidance_only = self.host_facts.compatibility in {
            "missing",
            "unverified",
            "compatible_not_runnable",
        }
        if not writes and not guidance_only:
            raise DeploymentContractError(
                "a runnable deployment plan must contain writes"
            )
        file_targets = [
            item.target for item in writes if item.action in {"create", "update"}
        ]
        if len(file_targets) != len(set(file_targets)):
            raise DeploymentContractError("file write targets must be unique")
        object.__setattr__(self, "writes", writes)
        if self.generated_at is not None:
            object.__setattr__(
                self, "generated_at", _string(self.generated_at, "generated_at")
            )
        expected = canonical_sha256(self.digest_mapping())
        if self.plan_sha256 and _sha256(self.plan_sha256, "plan_sha256") != expected:
            raise DeploymentContractError("plan_sha256 does not match plan")
        object.__setattr__(self, "plan_sha256", expected)

    def digest_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "request": self.request.to_mapping(),
            "request_sha256": self.request_sha256,
            "snapshots": [item.to_mapping() for item in self.snapshots],
            "persona": self.persona,
            "persona_sha256": self.persona_sha256,
            "host_facts": self.host_facts.to_mapping(),
            "writes": [item.to_mapping() for item in self.writes],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DeploymentPlan:
        fields = {
            "schema_version", "request", "request_sha256", "snapshots", "persona",
            "persona_sha256", "host_facts", "writes", "generated_at", "plan_sha256",
        }
        data = _mapping(value, fields, optional={"plan_sha256"})
        return cls(
            schema_version=cast(str, data["schema_version"]),
            request=DeploymentRequest.from_mapping(cast(Mapping[str, object], data["request"])),
            request_sha256=cast(str, data["request_sha256"]),
            snapshots=tuple(
                SkillSnapshot.from_mapping(cast(Mapping[str, object], item))
                for item in _tuple(data["snapshots"], "snapshots")
            ),
            persona=cast(str, data["persona"]),
            persona_sha256=cast(str, data["persona_sha256"]),
            host_facts=HostFacts.from_mapping(cast(Mapping[str, object], data["host_facts"])),
            writes=tuple(
                WriteIntent.from_mapping(cast(Mapping[str, object], item))
                for item in _tuple(data["writes"], "writes")
            ),
            generated_at=cast(str | None, data["generated_at"]),
            plan_sha256=cast(str, data.get("plan_sha256", "")),
        )

    def to_mapping(self) -> dict[str, object]:
        return self.digest_mapping() | {
            "generated_at": self.generated_at,
            "plan_sha256": self.plan_sha256,
        }


@dataclass(frozen=True, kw_only=True)
class DeploymentManifest:
    schema_version: str
    deployment_id: str
    agent_id: str
    request: DeploymentRequest
    request_sha256: str
    plan_sha256: str
    skill_tree_sha256s: Mapping[str, str]
    host_facts: HostFacts
    managed_paths: tuple[str, ...]
    status: str
    updated_at: str | None
    previous_manifest: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _string(self.schema_version, "schema_version"))
        object.__setattr__(self, "deployment_id", _kebab(self.deployment_id, "deployment_id"))
        object.__setattr__(self, "agent_id", _kebab(self.agent_id, "agent_id"))
        if type(self.request) is not DeploymentRequest:
            raise DeploymentContractError("request must be typed")
        if (self.deployment_id, self.agent_id) != (self.request.deployment_id, self.request.agent_id):
            raise DeploymentContractError("manifest identity does not match request")
        if _sha256(self.request_sha256, "request_sha256") != canonical_sha256(self.request.to_mapping()):
            raise DeploymentContractError("request_sha256 does not match request")
        object.__setattr__(self, "plan_sha256", _sha256(self.plan_sha256, "plan_sha256"))
        digests: dict[str, str] = {}
        if not isinstance(self.skill_tree_sha256s, Mapping) or not self.skill_tree_sha256s:
            raise DeploymentContractError("skill_tree_sha256s must not be empty")
        for name, digest in self.skill_tree_sha256s.items():
            digests[_kebab(name, "Skill name")] = _sha256(digest, "Skill tree digest")
        expected_names = {
            self.request.primary_workflow,
            *(item.name for item in self.request.related_workflows),
            *(item.name for item in self.request.auxiliary_skills),
        }
        if set(digests) != expected_names:
            raise DeploymentContractError(
                "skill tree digests must exactly match the requested Skills"
            )
        object.__setattr__(self, "skill_tree_sha256s", MappingProxyType(dict(sorted(digests.items()))))
        if type(self.host_facts) is not HostFacts or self.host_facts.host != self.request.host:
            raise DeploymentContractError("host facts do not match request")
        paths = tuple(_absolute(item, "managed path") for item in self.managed_paths)
        if len(paths) != len(set(paths)):
            raise DeploymentContractError("managed paths must be unique")
        if self.status == "guidance_only":
            if paths or self.host_facts.compatibility not in {
                "missing",
                "unverified",
                "compatible_not_runnable",
            }:
                raise DeploymentContractError(
                    "guidance-only manifests cannot manage host paths"
                )
        elif not paths:
            raise DeploymentContractError("deployment manifests must manage paths")
        object.__setattr__(self, "managed_paths", paths)
        if self.status not in _MANIFEST_STATUSES:
            raise DeploymentContractError("unsupported manifest status")
        if self.updated_at is not None:
            object.__setattr__(
                self, "updated_at", _string(self.updated_at, "updated_at")
            )
        if self.previous_manifest is not None:
            object.__setattr__(self, "previous_manifest", _absolute(self.previous_manifest, "previous_manifest"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DeploymentManifest:
        fields = {
            "schema_version", "deployment_id", "agent_id", "request", "request_sha256",
            "plan_sha256", "skill_tree_sha256s", "host_facts", "managed_paths", "status",
            "updated_at", "previous_manifest",
        }
        data = _mapping(value, fields)
        return cls(
            schema_version=cast(str, data["schema_version"]),
            deployment_id=cast(str, data["deployment_id"]),
            agent_id=cast(str, data["agent_id"]),
            request=DeploymentRequest.from_mapping(cast(Mapping[str, object], data["request"])),
            request_sha256=cast(str, data["request_sha256"]),
            plan_sha256=cast(str, data["plan_sha256"]),
            skill_tree_sha256s=cast(Mapping[str, str], data["skill_tree_sha256s"]),
            host_facts=HostFacts.from_mapping(cast(Mapping[str, object], data["host_facts"])),
            managed_paths=tuple(cast(str, item) for item in _tuple(data["managed_paths"], "managed_paths")),
            status=cast(str, data["status"]),
            updated_at=cast(str | None, data["updated_at"]),
            previous_manifest=cast(str | None, data["previous_manifest"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "agent_id": self.agent_id,
            "request": self.request.to_mapping(),
            "request_sha256": self.request_sha256,
            "plan_sha256": self.plan_sha256,
            "skill_tree_sha256s": dict(self.skill_tree_sha256s),
            "host_facts": self.host_facts.to_mapping(),
            "managed_paths": list(self.managed_paths),
            "status": self.status,
            "updated_at": self.updated_at,
            "previous_manifest": self.previous_manifest,
        }


@dataclass(frozen=True, kw_only=True)
class EnablementCheck:
    name: str
    status: Literal["passed", "failed", "not_checked"]
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _string(self.name, "enablement check name"))
        if self.status not in _ENABLEMENT_CHECK_STATUSES:
            raise DeploymentContractError("unsupported enablement check status")
        frozen = _freeze(self.details)
        if not isinstance(frozen, Mapping):
            raise DeploymentContractError("enablement check details must be an object")
        object.__setattr__(self, "details", frozen)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EnablementCheck:
        data = _mapping(value, {"name", "status", "details"})
        return cls(
            name=cast(str, data["name"]),
            status=cast(Literal["passed", "failed", "not_checked"], data["status"]),
            details=cast(Mapping[str, object], data["details"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "details": cast(dict[str, object], _thaw(self.details)),
        }


@dataclass(frozen=True, kw_only=True)
class EnablementResult:
    requested: bool
    platforms: tuple[str, ...]
    status: Literal[
        "not_requested", "verified", "partially_ready", "rolled_back", "outcome_unknown"
    ]
    checks: tuple[EnablementCheck, ...]
    details: Mapping[str, object]

    def __post_init__(self) -> None:
        if type(self.requested) is not bool:
            raise DeploymentContractError("enablement requested must be a boolean")
        platforms = tuple(
            _string(item, "enablement platform").strip().lower()
            for item in self.platforms
        )
        if any(_PLATFORM_RE.fullmatch(item) is None for item in platforms):
            raise DeploymentContractError("invalid enablement platform")
        if len(platforms) != len(set(platforms)):
            raise DeploymentContractError("enablement platforms must be unique")
        if platforms != tuple(sorted(platforms, key=lambda item: item.encode("utf-8"))):
            raise DeploymentContractError("enablement platforms must be byte-sorted")
        object.__setattr__(self, "platforms", platforms)
        if self.status not in _ENABLEMENT_STATUSES:
            raise DeploymentContractError("unsupported enablement status")
        checks = tuple(self.checks)
        if not all(type(item) is EnablementCheck for item in checks):
            raise DeploymentContractError("enablement checks must be typed")
        names = [item.name for item in checks]
        if len(names) != len(set(names)):
            raise DeploymentContractError("enablement checks must be unique")
        object.__setattr__(self, "checks", checks)
        frozen = _freeze(self.details)
        if not isinstance(frozen, Mapping):
            raise DeploymentContractError("enablement details must be an object")
        object.__setattr__(self, "details", frozen)
        if self.requested:
            if not platforms or self.status == "not_requested":
                raise DeploymentContractError("requested enablement needs platforms and a result")
        elif platforms or checks or self.status != "not_requested" or self.details:
            raise DeploymentContractError("non-requested enablement must be empty")

    @classmethod
    def not_requested(cls) -> EnablementResult:
        return cls(
            requested=False,
            platforms=(),
            status="not_requested",
            checks=(),
            details={},
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> EnablementResult:
        data = _mapping(value, {"requested", "platforms", "status", "checks", "details"})
        return cls(
            requested=cast(bool, data["requested"]),
            platforms=tuple(
                cast(str, item) for item in _tuple(data["platforms"], "enablement platforms")
            ),
            status=cast(
                Literal[
                    "not_requested",
                    "verified",
                    "partially_ready",
                    "rolled_back",
                    "outcome_unknown",
                ],
                data["status"],
            ),
            checks=tuple(
                EnablementCheck.from_mapping(cast(Mapping[str, object], item))
                for item in _tuple(data["checks"], "enablement checks")
            ),
            details=cast(Mapping[str, object], data["details"]),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "requested": self.requested,
            "platforms": list(self.platforms),
            "status": self.status,
            "checks": [item.to_mapping() for item in self.checks],
            "details": cast(dict[str, object], _thaw(self.details)),
        }


@dataclass(frozen=True, kw_only=True)
class VerificationResult:
    schema_version: str
    deployment_id: str
    status: Literal["verified", "partially_verified", "failed", "outcome_unknown"]
    static: Mapping[str, object]
    discovery: Mapping[str, object]
    behavior: Mapping[str, object]
    details: tuple[object, ...]
    enablement: EnablementResult = field(default_factory=EnablementResult.not_requested)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _string(self.schema_version, "schema_version"))
        object.__setattr__(self, "deployment_id", _kebab(self.deployment_id, "deployment_id"))
        if self.status not in _VERIFICATION_STATUSES:
            raise DeploymentContractError("unsupported verification status")
        for field in ("static", "discovery", "behavior"):
            frozen = _freeze(getattr(self, field))
            if not isinstance(frozen, Mapping):
                raise DeploymentContractError(f"{field} must be an object")
            object.__setattr__(self, field, frozen)
        frozen_details = _freeze(self.details)
        if type(frozen_details) is not tuple:
            raise DeploymentContractError("details must be an array")
        object.__setattr__(self, "details", frozen_details)
        if type(self.enablement) is not EnablementResult:
            raise DeploymentContractError("enablement must be typed")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> VerificationResult:
        fields = {
            "schema_version", "deployment_id", "status", "static", "discovery",
            "behavior", "details", "enablement",
        }
        data = _mapping(value, fields, optional={"enablement"})
        return cls(
            schema_version=cast(str, data["schema_version"]),
            deployment_id=cast(str, data["deployment_id"]),
            status=cast(Literal["verified", "partially_verified", "failed", "outcome_unknown"], data["status"]),
            static=cast(Mapping[str, object], data["static"]),
            discovery=cast(Mapping[str, object], data["discovery"]),
            behavior=cast(Mapping[str, object], data["behavior"]),
            details=_tuple(data["details"], "details"),
            enablement=(
                EnablementResult.from_mapping(
                    cast(Mapping[str, object], data["enablement"])
                )
                if "enablement" in data
                else EnablementResult.not_requested()
            ),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "status": self.status,
            "static": cast(dict[str, object], _thaw(self.static)),
            "discovery": cast(dict[str, object], _thaw(self.discovery)),
            "behavior": cast(dict[str, object], _thaw(self.behavior)),
            "details": cast(list[object], _thaw(self.details)),
            "enablement": self.enablement.to_mapping(),
        }


__all__ = [
    "DeploymentContractError",
    "DeploymentManifest",
    "DeploymentPlan",
    "DeploymentRequest",
    "EnablementCheck",
    "EnablementResult",
    "HostFacts",
    "SkillFile",
    "SkillSelection",
    "SkillSnapshot",
    "VerificationResult",
    "WriteIntent",
    "canonical_sha256",
    "read_json_object",
]
