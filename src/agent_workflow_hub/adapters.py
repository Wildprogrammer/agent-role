from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .support import PROJECT_HOSTS

SUPPORTED_SPEC_VERSION = "1.0"
EVIDENCE_STATES = frozenset(
    {"verified", "conditional", "unverified", "unsupported"}
)
EVIDENCE_READY_STATES = EVIDENCE_STATES - {"unverified"}
REGISTRATION_MODES = frozenset(
    {"project-native", "symlink", "junction", "shim"}
)
REQUIRED_FIELDS = frozenset(
    {
        "spec_version",
        "host",
        "official_docs",
        "last_verified",
        "minimum_version",
        "skill_discovery",
        "mcp_support",
        "subagent_support",
        "explicit_subagent_consent",
        "registration_modes",
    }
)


class AdapterError(ValueError):
    pass


@dataclass(frozen=True)
class AdapterContract:
    spec_version: str
    host: str
    official_docs: str
    last_verified: str
    minimum_version: str
    skill_discovery: str
    mcp_support: str
    subagent_support: str
    explicit_subagent_consent: bool
    registration_modes: tuple[str, ...]
    body: str

    @property
    def is_verified(self) -> bool:
        """Report evidence readiness, not support for every host feature."""
        return (
            self.skill_discovery == "verified"
            and self.mcp_support in EVIDENCE_READY_STATES
            and self.subagent_support in EVIDENCE_READY_STATES
            and self.explicit_subagent_consent is True
        )


def _validate_path(path: Path) -> None:
    if (
        not isinstance(path, Path)
        or path.name != "ADAPTER.md"
        or path.parent.parent.name != "adapters"
    ):
        raise AdapterError(
            "Adapter path must match canonical path "
            "adapters/<host>/ADAPTER.md"
        )


def _validate_official_docs(value: Any) -> str:
    message = "Adapter official_docs must be an HTTPS URL"
    if (
        not isinstance(value, str)
        or "\\" in value
        or any(
            character.isspace() or not character.isprintable()
            for character in value
        )
    ):
        raise AdapterError(message)
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
        _ = parsed.port
    except (UnicodeError, ValueError):
        raise AdapterError(message) from None
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.netloc.endswith(":")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AdapterError(message)

    if "%" in hostname:
        raise AdapterError(message)
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        is_ip_address = False
    else:
        is_ip_address = True

    if not is_ip_address:
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            raise AdapterError(message) from None
        labels = ascii_hostname.split(".")
        if (
            len(ascii_hostname) > 253
            or any(
                not 1 <= len(label) <= 63
                or not label[0].isalnum()
                or not label[-1].isalnum()
                or not all(
                    character.isalnum() or character == "-"
                    for character in label
                )
                for label in labels
            )
        ):
            raise AdapterError(message)
        for label in labels:
            if label.casefold().startswith("xn--"):
                try:
                    decoded_label = label.encode("ascii").decode("idna")
                    round_trip = (
                        decoded_label.encode("idna").decode("ascii")
                    )
                except UnicodeError:
                    raise AdapterError(message) from None
                if round_trip.casefold() != label.casefold():
                    raise AdapterError(message)
    return value


def _validate_last_verified(value: Any) -> str:
    message = (
        "Adapter last_verified must be a strict ISO YYYY-MM-DD calendar date"
    )
    if not isinstance(value, str):
        raise AdapterError(message)
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise AdapterError(message) from None
    if parsed.isoformat() != value:
        raise AdapterError(message)
    return value


def validate_adapter(
    path: Path, frontmatter: Mapping[str, Any], body: str
) -> AdapterContract:
    _validate_path(path)
    if not isinstance(frontmatter, Mapping):
        raise AdapterError("Adapter frontmatter must be a mapping")
    if not all(isinstance(field, str) for field in frontmatter):
        raise AdapterError("Adapter top-level field names must be strings")

    fields = set(frontmatter)
    missing = REQUIRED_FIELDS - fields
    if missing:
        raise AdapterError(
            f"Adapter missing required fields: {sorted(missing)}"
        )
    if fields - REQUIRED_FIELDS:
        raise AdapterError("Adapter has unexpected top-level fields")

    host = frontmatter["host"]
    if not isinstance(host, str) or host not in PROJECT_HOSTS:
        raise AdapterError("Adapter host must be a known project host")
    if host != path.parent.name:
        raise AdapterError("Adapter host must exactly match its directory")

    spec_version = frontmatter["spec_version"]
    if (
        not isinstance(spec_version, str)
        or spec_version != SUPPORTED_SPEC_VERSION
    ):
        raise AdapterError(
            "Adapter spec_version must equal supported version 1.0"
        )

    official_docs = _validate_official_docs(frontmatter["official_docs"])
    last_verified = _validate_last_verified(frontmatter["last_verified"])

    minimum_version = frontmatter["minimum_version"]
    if (
        not isinstance(minimum_version, str)
        or not minimum_version
        or not minimum_version.isprintable()
        or any(character.isspace() for character in minimum_version)
    ):
        raise AdapterError(
            "Adapter minimum_version must be a nonblank printable string "
            "without whitespace"
        )

    evidence: dict[str, str] = {}
    for field in (
        "skill_discovery",
        "mcp_support",
        "subagent_support",
    ):
        state = frontmatter[field]
        if not isinstance(state, str) or state not in EVIDENCE_STATES:
            raise AdapterError(
                f"Adapter {field} must be a valid evidence state"
            )
        evidence[field] = state

    consent = frontmatter["explicit_subagent_consent"]
    if type(consent) is not bool:
        raise AdapterError(
            "Adapter explicit_subagent_consent must be an exact boolean"
        )
    if not consent:
        raise AdapterError(
            "Adapter explicit_subagent_consent must be true for subagent use"
        )

    modes = frontmatter["registration_modes"]
    if (
        not isinstance(modes, list)
        or not modes
        or not all(
            isinstance(mode, str) and mode in REGISTRATION_MODES
            for mode in modes
        )
        or len(modes) != len(set(modes))
    ):
        raise AdapterError(
            "Adapter registration_modes must be a non-empty list of unique "
            "allowed strings"
        )

    if (
        not isinstance(body, str)
        or not body.strip()
        or any(
            not character.isprintable()
            and character not in "\t\n\r"
            for character in body
        )
    ):
        raise AdapterError("Adapter body must be a nonblank string")

    return AdapterContract(
        spec_version=spec_version,
        host=host,
        official_docs=official_docs,
        last_verified=last_verified,
        minimum_version=minimum_version,
        skill_discovery=evidence["skill_discovery"],
        mcp_support=evidence["mcp_support"],
        subagent_support=evidence["subagent_support"],
        explicit_subagent_consent=consent,
        registration_modes=tuple(modes),
        body=body,
    )
