from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


class ConnectorEvidenceError(ValueError):
    pass


@dataclass(frozen=True)
class ConnectorFrame:
    axis: tuple[float, float, float]
    key: tuple[float, float, float]
    side: tuple[float, float, float]


def _vector(value: Sequence[float], label: str) -> tuple[float, float, float]:
    if len(value) != 3:
        raise ValueError(f"{label} must contain three values")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label} must contain finite values")
    return result  # type: ignore[return-value]


def _dot(
    first: tuple[float, float, float], second: tuple[float, float, float]
) -> float:
    return sum(a * b for a, b in zip(first, second))


def _scale(
    value: tuple[float, float, float], factor: float
) -> tuple[float, float, float]:
    return tuple(item * factor for item in value)  # type: ignore[return-value]


def _subtract(
    first: tuple[float, float, float], second: tuple[float, float, float]
) -> tuple[float, float, float]:
    return tuple(a - b for a, b in zip(first, second))  # type: ignore[return-value]


def _cross(
    first: tuple[float, float, float], second: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _normalize(
    value: tuple[float, float, float], label: str
) -> tuple[float, float, float]:
    length = math.sqrt(_dot(value, value))
    if math.isclose(length, 0.0, abs_tol=1e-12):
        raise ValueError(f"{label} must be non-zero")
    return _scale(value, 1.0 / length)


def connector_frame(
    axis: Sequence[float], key_direction: Sequence[float]
) -> ConnectorFrame:
    normalized_axis = _normalize(_vector(axis, "axis"), "axis")
    raw_key = _vector(key_direction, "key_direction")
    projected_key = _subtract(raw_key, _scale(normalized_axis, _dot(raw_key, normalized_axis)))
    normalized_key = _normalize(projected_key, "key_direction projection")
    side = _normalize(_cross(normalized_axis, normalized_key), "connector side")
    return ConnectorFrame(axis=normalized_axis, key=normalized_key, side=side)


def socket_dimensions(connector: Any) -> tuple[float, float, float]:
    clearance = float(connector.clearance_per_side_mm)
    return (
        float(connector.width_mm) + 2.0 * clearance,
        float(connector.height_mm) + 2.0 * clearance,
        float(connector.engagement_mm) + float(connector.socket_bottom_clearance_mm),
    )


def rounded_rectangle_area(width: float, height: float, radius: float) -> float:
    return float(width) * float(height) - (4.0 - math.pi) * float(radius) ** 2


def nominal_pin_volume(connector: Any) -> float:
    return rounded_rectangle_area(
        connector.width_mm,
        connector.height_mm,
        connector.corner_radius_mm,
    ) * float(connector.engagement_mm)


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConnectorEvidenceError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ConnectorEvidenceError(f"{label} must be finite")
    return result


def _close(first: float, second: float) -> bool:
    return math.isclose(first, second, rel_tol=1e-6, abs_tol=1e-6)


def validate_connector_evidence(
    evidence: Mapping[str, Any], plan: Any
) -> dict[str, Any]:
    if _value(evidence, "status") != "validated":
        raise ConnectorEvidenceError("connector evidence status must be validated")
    raw_records = _value(evidence, "connectors")
    if not isinstance(raw_records, Sequence) or isinstance(raw_records, (str, bytes)):
        raise ConnectorEvidenceError("connector set must be a list")
    expected = {connector.id: connector for connector in plan.connectors}
    record_ids = [_value(record, "id") for record in raw_records]
    if len(record_ids) != len(set(record_ids)) or set(record_ids) != set(expected):
        raise ConnectorEvidenceError("connector set does not match plan")

    measured_net = 0.0
    for record in raw_records:
        connector_id = _value(record, "id")
        connector = expected[connector_id]
        if _value(record, "cut_id") != connector.cut_id:
            raise ConnectorEvidenceError(f"cut id mismatch for {connector_id}")
        if _value(record, "male_piece") != connector.male_piece:
            raise ConnectorEvidenceError(f"male piece mismatch for {connector_id}")
        if _value(record, "female_piece") != connector.female_piece:
            raise ConnectorEvidenceError(f"female piece mismatch for {connector_id}")
        if _value(record, "solver") != "EXACT":
            raise ConnectorEvidenceError(f"EXACT solver required for {connector_id}")
        if _value(record, "union_applied") is not True:
            raise ConnectorEvidenceError(f"union failed for {connector_id}")
        if _value(record, "difference_applied") is not True:
            raise ConnectorEvidenceError(f"difference failed for {connector_id}")

        male_before = _number(
            _value(record, "male_volume_before_mm3"), "male volume before"
        )
        male_after = _number(
            _value(record, "male_volume_after_mm3"), "male volume after"
        )
        female_before = _number(
            _value(record, "female_volume_before_mm3"), "female volume before"
        )
        female_after = _number(
            _value(record, "female_volume_after_mm3"), "female volume after"
        )
        added = _number(
            _value(record, "measured_added_volume_mm3"), "measured added volume"
        )
        removed = _number(
            _value(record, "measured_removed_volume_mm3"), "measured removed volume"
        )
        if added <= 0 or not _close(male_after - male_before, added):
            raise ConnectorEvidenceError(f"male volume mismatch for {connector_id}")
        if removed <= 0 or not _close(female_before - female_after, removed):
            raise ConnectorEvidenceError(f"female volume mismatch for {connector_id}")
        theoretical = _number(
            _value(record, "theoretical_pin_volume_mm3"), "theoretical pin volume"
        )
        if theoretical <= 0 or added > theoretical * 1.01:
            raise ConnectorEvidenceError(
                f"theoretical pin volume mismatch for {connector_id}"
            )

        effective_length = _number(
            _value(record, "effective_length_mm"), "effective length"
        )
        if effective_length + 1e-6 < connector.engagement_mm:
            raise ConnectorEvidenceError(f"effective length failed for {connector_id}")
        socket_depth = _number(_value(record, "socket_depth_mm"), "socket depth")
        required_depth = connector.engagement_mm + connector.socket_bottom_clearance_mm
        if socket_depth + 1e-6 < required_depth:
            raise ConnectorEvidenceError(f"socket depth failed for {connector_id}")
        minimum_wall = _number(_value(record, "minimum_wall_mm"), "minimum wall")
        if minimum_wall + 1e-6 < connector.minimum_wall_mm:
            raise ConnectorEvidenceError(f"minimum wall failed for {connector_id}")
        edge_margin = _number(
            _value(record, "minimum_edge_margin_mm"), "minimum edge margin"
        )
        if edge_margin + 1e-6 < connector.minimum_edge_margin_mm:
            raise ConnectorEvidenceError(f"minimum edge margin failed for {connector_id}")
        measured_net += added - removed

    reported_net = _number(
        _value(evidence, "measured_net_volume_delta_mm3"),
        "measured net volume delta",
    )
    if not _close(reported_net, measured_net):
        raise ConnectorEvidenceError("net volume delta does not match connector records")
    return {
        "connector_count": len(raw_records),
        "measured_net_volume_delta_mm3": measured_net,
    }
