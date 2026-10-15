from __future__ import annotations

import re
from collections.abc import Iterable, Mapping


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EXACT_COMPONENT_REFERENCE = re.compile(
    r"^([a-z][a-z0-9_-]{0,63})@([1-9][0-9]*)$"
)


class CompositionContractError(ValueError):
    """A stable validation failure in the platform-neutral composition core."""


def parse_component_reference(value: str) -> tuple[str, int]:
    if not isinstance(value, str):
        raise CompositionContractError(
            "component_version_mismatch: component reference must be text"
        )
    match = EXACT_COMPONENT_REFERENCE.fullmatch(value)
    if match is None:
        raise CompositionContractError(
            "component_version_mismatch: component reference must use exact <id>@<version>"
        )
    component_id, version = match.groups()
    return component_id, int(version)


def validate_parameter_mapping(
    *,
    component_parameters: Iterable[str],
    mapping: Mapping[str, str],
    scenario_parameters: Iterable[str],
) -> dict[str, str]:
    expected = tuple(component_parameters)
    expected_keys = set(expected)
    actual_keys = set(mapping)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unknown = sorted(actual_keys - expected_keys)
        details = []
        if missing:
            details.append(f"missing mappings: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown mappings: {', '.join(unknown)}")
        raise CompositionContractError(
            "component_parameter_invalid: " + "; ".join(details)
        )

    known_targets = set(scenario_parameters)
    normalized: dict[str, str] = {}
    for key in sorted(mapping):
        target = mapping[key]
        if not isinstance(key, str) or IDENTIFIER.fullmatch(key) is None:
            raise CompositionContractError(
                "component_parameter_invalid: mapping key must be a lowercase identifier"
            )
        if not isinstance(target, str) or target not in known_targets:
            raise CompositionContractError(
                "component_parameter_invalid: mapping references an unknown scenario parameter"
            )
        normalized[key] = target
    return normalized
