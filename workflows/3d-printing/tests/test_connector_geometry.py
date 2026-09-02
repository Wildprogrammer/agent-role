import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "connector_geometry.py"
SPEC = importlib.util.spec_from_file_location("workflow_connector_geometry", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PLAN_SCRIPT = Path(__file__).parents[1] / "scripts" / "split_plan.py"
PLAN_SPEC = importlib.util.spec_from_file_location(
    "workflow_split_plan_for_connector_geometry", PLAN_SCRIPT
)
assert PLAN_SPEC and PLAN_SPEC.loader
PLAN_MODULE = importlib.util.module_from_spec(PLAN_SPEC)
sys.modules[PLAN_SPEC.name] = PLAN_MODULE
PLAN_SPEC.loader.exec_module(PLAN_MODULE)

FIXTURE = Path("workflows/3d-printing/tests/fixtures/split-keyed-pin.json")


def plan(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return PLAN_MODULE.load_split_plan(path)


def valid_evidence():
    return {
        "status": "validated",
        "connectors": [
            {
                "id": "neck-a",
                "cut_id": "neck",
                "male_piece": "head",
                "female_piece": "body",
                "solver": "EXACT",
                "union_applied": True,
                "difference_applied": True,
                "male_volume_before_mm3": 100.0,
                "male_volume_after_mm3": 125.0,
                "female_volume_before_mm3": 200.0,
                "female_volume_after_mm3": 170.0,
                "theoretical_pin_volume_mm3": 30.0,
                "measured_added_volume_mm3": 25.0,
                "measured_removed_volume_mm3": 30.0,
                "effective_length_mm": 7.0,
                "socket_depth_mm": 7.5,
                "minimum_wall_mm": 1.3,
                "minimum_edge_margin_mm": 1.4,
            }
        ],
        "measured_net_volume_delta_mm3": -5.0,
    }


def test_connector_frame_projects_and_normalizes_key_direction():
    frame = MODULE.connector_frame((0, 0, -2), (3, 0, 1))

    assert frame.axis == pytest.approx((0, 0, -1))
    assert frame.key == pytest.approx((1, 0, 0))
    assert frame.side == pytest.approx((0, -1, 0))
    assert sum(a * b for a, b in zip(frame.axis, frame.key)) == pytest.approx(0)


def test_socket_dimensions_apply_per_side_and_bottom_clearance(tmp_path):
    connector = plan(tmp_path).connectors[0]

    assert MODULE.socket_dimensions(connector) == pytest.approx((6.5, 5.0, 7.5))
    assert MODULE.rounded_rectangle_area(6.0, 4.5, 1.0) == pytest.approx(
        27.0 - (4.0 - MODULE.math.pi)
    )
    assert MODULE.nominal_pin_volume(connector) == pytest.approx(
        MODULE.rounded_rectangle_area(6.0, 4.5, 1.0) * 7.0
    )


def test_connector_evidence_accepts_complete_measurements(tmp_path):
    result = MODULE.validate_connector_evidence(valid_evidence(), plan(tmp_path))

    assert result["connector_count"] == 1
    assert result["measured_net_volume_delta_mm3"] == -5.0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("male_piece", "body", "male piece"),
        ("female_piece", "head", "female piece"),
        ("solver", "FAST", "EXACT"),
        ("union_applied", False, "union"),
        ("difference_applied", False, "difference"),
        ("minimum_wall_mm", 1.1, "wall"),
        ("minimum_edge_margin_mm", 1.1, "edge"),
        ("effective_length_mm", 6.9, "length"),
        ("socket_depth_mm", 7.4, "socket depth"),
        ("measured_added_volume_mm3", 24.0, "male volume"),
        ("measured_removed_volume_mm3", 29.0, "female volume"),
    ],
)
def test_connector_evidence_rejects_failed_or_inconsistent_measurements(
    tmp_path, field, value, message
):
    evidence = valid_evidence()
    evidence["connectors"][0][field] = value

    with pytest.raises(MODULE.ConnectorEvidenceError, match=message):
        MODULE.validate_connector_evidence(evidence, plan(tmp_path))


def test_connector_evidence_requires_every_connector_once(tmp_path):
    evidence = valid_evidence()
    evidence["connectors"] = []

    with pytest.raises(MODULE.ConnectorEvidenceError, match="connector set"):
        MODULE.validate_connector_evidence(evidence, plan(tmp_path))


def test_connector_evidence_rejects_wrong_reported_net_delta(tmp_path):
    evidence = valid_evidence()
    evidence["measured_net_volume_delta_mm3"] = 0.0

    with pytest.raises(MODULE.ConnectorEvidenceError, match="net volume"):
        MODULE.validate_connector_evidence(evidence, plan(tmp_path))
