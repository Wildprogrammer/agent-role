"""Strict, host-neutral configuration contracts for one knowledge agent."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from typing import Literal, Mapping
from urllib.parse import urlsplit


class KnowledgeSupportContractError(ValueError):
    """Raised when a knowledge agent configuration violates its contract."""


SourceKind = Literal["git", "local-file", "collected-document"]
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_FORBIDDEN_KEYS = {
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
    "username",
}


@dataclass(frozen=True, kw_only=True)
class KnowledgeSource:
    source_id: str
    source_type: SourceKind
    path: Path | None = None
    repository: Path | None = None
    origin_url: str | None = None
    include_code: bool = False


@dataclass(frozen=True, kw_only=True)
class SupplementalSkill:
    name: str
    purpose: str


@dataclass(frozen=True, kw_only=True)
class EmbeddingConfig:
    provider: Literal["ollama"]
    base_url: str
    model: Literal["qwen3-embedding:0.6b"]
    fallback: Literal["fts"]


@dataclass(frozen=True, kw_only=True)
class AgentConfig:
    schema_version: str
    agent_id: str
    display_name: str
    purpose: str
    audiences: tuple[str, ...]
    workdir: Path
    sources: tuple[KnowledgeSource, ...]
    supplemental_skills: tuple[SupplementalSkill, ...]
    embedding: EmbeddingConfig


def canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def database_path(config: AgentConfig) -> Path:
    return (config.workdir / "knowledge-support" / "lancedb").resolve()


def load_agent_config(path: Path) -> AgentConfig:
    target = Path(path)
    if not target.is_absolute():
        raise KnowledgeSupportContractError("config path must be absolute")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeSupportContractError(f"cannot read agent config: {exc}") from None
    if not isinstance(raw, dict):
        raise KnowledgeSupportContractError("agent config must be a JSON object")
    _reject_forbidden_keys(raw)
    _require_keys(
        raw,
        {
            "schema_version",
            "agent_id",
            "display_name",
            "purpose",
            "audiences",
            "workdir",
            "sources",
            "supplemental_skills",
            "embedding",
        },
        "agent config",
    )
    if raw["schema_version"] != "1.0":
        raise KnowledgeSupportContractError("unsupported schema_version")
    agent_id = _identifier(raw["agent_id"], "agent_id")
    display_name = _nonempty_text(raw["display_name"], "display_name")
    purpose = _nonempty_text(raw["purpose"], "purpose")
    audiences_raw = raw["audiences"]
    if not isinstance(audiences_raw, list) or not audiences_raw:
        raise KnowledgeSupportContractError("audiences must be a non-empty array")
    audiences = tuple(
        _nonempty_text(value, "audience") for value in audiences_raw
    )
    workdir = _absolute_path(raw["workdir"], "workdir")

    sources_raw = raw["sources"]
    if not isinstance(sources_raw, list) or not sources_raw:
        raise KnowledgeSupportContractError("sources must be a non-empty array")
    sources = tuple(_parse_source(value) for value in sources_raw)
    source_ids = [source.source_id for source in sources]
    if len(source_ids) != len(set(source_ids)):
        raise KnowledgeSupportContractError("duplicate source id")

    skills_raw = raw["supplemental_skills"]
    if not isinstance(skills_raw, list):
        raise KnowledgeSupportContractError("supplemental_skills must be an array")
    supplemental_skills = tuple(_parse_skill(value) for value in skills_raw)
    names = [skill.name for skill in supplemental_skills]
    if len(names) != len(set(names)):
        raise KnowledgeSupportContractError("duplicate supplemental skill")

    embedding = _parse_embedding(raw["embedding"])
    return AgentConfig(
        schema_version="1.0",
        agent_id=agent_id,
        display_name=display_name,
        purpose=purpose,
        audiences=audiences,
        workdir=workdir,
        sources=sources,
        supplemental_skills=supplemental_skills,
        embedding=embedding,
    )


def _parse_source(value: object) -> KnowledgeSource:
    if not isinstance(value, dict):
        raise KnowledgeSupportContractError("source must be an object")
    source_type = value.get("type")
    if source_type == "git":
        _require_keys(value, {"id", "type", "repository", "include_code"}, "git source")
        if not isinstance(value["include_code"], bool):
            raise KnowledgeSupportContractError("include_code must be boolean")
        return KnowledgeSource(
            source_id=_identifier(value["id"], "source id"),
            source_type="git",
            repository=_absolute_path(value["repository"], "repository"),
            include_code=value["include_code"],
        )
    if source_type == "local-file":
        _require_keys(value, {"id", "type", "path"}, "local-file source")
        return KnowledgeSource(
            source_id=_identifier(value["id"], "source id"),
            source_type="local-file",
            path=_absolute_path(value["path"], "source path"),
        )
    if source_type == "collected-document":
        _require_keys(
            value,
            {"id", "type", "path", "origin_url"},
            "collected-document source",
        )
        return KnowledgeSource(
            source_id=_identifier(value["id"], "source id"),
            source_type="collected-document",
            path=_absolute_path(value["path"], "source path"),
            origin_url=_safe_url(value["origin_url"], "origin_url"),
        )
    raise KnowledgeSupportContractError("unsupported source type")


def _parse_skill(value: object) -> SupplementalSkill:
    if not isinstance(value, dict):
        raise KnowledgeSupportContractError("supplemental skill must be an object")
    _require_keys(value, {"name", "purpose"}, "supplemental skill")
    return SupplementalSkill(
        name=_identifier(value["name"], "supplemental skill name"),
        purpose=_nonempty_text(value["purpose"], "supplemental skill purpose"),
    )


def _parse_embedding(value: object) -> EmbeddingConfig:
    if not isinstance(value, dict):
        raise KnowledgeSupportContractError("embedding must be an object")
    _require_keys(value, {"provider", "base_url", "model", "fallback"}, "embedding")
    if value["provider"] != "ollama":
        raise KnowledgeSupportContractError("embedding provider must be ollama")
    if value["model"] != "qwen3-embedding:0.6b":
        raise KnowledgeSupportContractError("unsupported embedding model")
    if value["fallback"] != "fts":
        raise KnowledgeSupportContractError("embedding fallback must be fts")
    base_url = _safe_url(value["base_url"], "embedding base_url")
    parsed = urlsplit(base_url)
    try:
        is_loopback = parsed.hostname == "localhost" or ipaddress.ip_address(
            parsed.hostname or ""
        ).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback:
        raise KnowledgeSupportContractError("embedding base_url must be loopback")
    return EmbeddingConfig(
        provider="ollama",
        base_url=base_url.rstrip("/"),
        model="qwen3-embedding:0.6b",
        fallback="fts",
    )


def _reject_forbidden_keys(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold().replace("-", "_") in _FORBIDDEN_KEYS:
                raise KnowledgeSupportContractError(
                    f"credential field is forbidden: {key}"
                )
            _reject_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_forbidden_keys(child)


def _require_keys(value: Mapping[str, object], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        unknown = sorted(actual - expected)
        missing = sorted(expected - actual)
        raise KnowledgeSupportContractError(
            f"{label} fields mismatch; unknown={unknown}, missing={missing}"
        )


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise KnowledgeSupportContractError(f"{label} must be a canonical identifier")
    return value


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeSupportContractError(f"{label} must be non-empty text")
    return value.strip()


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise KnowledgeSupportContractError(f"{label} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise KnowledgeSupportContractError(f"{label} must be an absolute path")
    return path.resolve()


def _safe_url(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise KnowledgeSupportContractError(f"{label} must be an HTTP URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise KnowledgeSupportContractError(
            f"{label} must be an HTTP URL without credentials or fragment"
        )
    return value
