import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "split_plan.py"
SPEC = importlib.util.spec_from_file_location("workflow_split_plan", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


FIXTURE = Path("workflows/3d-printing/tests/fixtures/split-three-pieces.json")
KEYED_FIXTURE = Path("workflows/3d-printing/tests/fixtures/split-keyed-pin.json")


def valid_data() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_three_pieces_can_map_to_two_confirmed_plates(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(valid_data()), encoding="utf-8")

    plan = MODULE.load_split_plan(path)

    assert plan.leaf_piece_ids == ("piece-a", "piece-b1", "piece-b2")
    assert [plate.id for plate in plan.plates] == ["plate-01", "plate-02"]
    assert plan.plates[0].placement_policy == "provider-default-single-piece"
    assert len(plan.plates[1].layout) == 2


def test_keyed_connector_plan_preserves_every_approved_parameter(tmp_path):
    path = tmp_path / "keyed.json"
    path.write_text(KEYED_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    plan = MODULE.load_split_plan(path)

    assert plan.connection_strategy == "integrated-keyed-pin"
    assert plan.assembly_filename == "fixture-assembly.3mf"
    assert len(plan.connectors) == 1
    connector = plan.connectors[0]
    assert connector.id == "neck-a"
    assert connector.cut_id == "neck"
    assert connector.male_piece == "head"
    assert connector.female_piece == "body"
    assert connector.center_mm == (0.0, 0.0, 0.0)
    assert connector.axis == (0.0, 0.0, -1.0)
    assert connector.key_direction == (1.0, 0.0, 0.0)
    assert connector.width_mm == 6.0
    assert connector.height_mm == 4.5
    assert connector.corner_radius_mm == 1.0
    assert connector.engagement_mm == 7.0
    assert connector.root_fillet_mm == 0.8
    assert connector.tip_chamfer_mm == 0.6
    assert connector.clearance_per_side_mm == 0.25
    assert connector.socket_bottom_clearance_mm == 0.5
    assert connector.minimum_wall_mm == 1.2
    assert connector.minimum_edge_margin_mm == 1.2


def test_keyed_plan_can_omit_optional_standard_3mf(tmp_path):
    data = keyed_data()
    data.pop("assembly_filename")
    path = tmp_path / "keyed-stl-only.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    plan = MODULE.load_split_plan(path)

    assert plan.connection_strategy == "integrated-keyed-pin"
    assert plan.assembly_filename is None
    assert plan.structure_diagram_filename is None


def test_keyed_plan_can_request_a_local_png_structure_diagram(tmp_path):
    data = keyed_data()
    data["structure_diagram_filename"] = "structure-diagram.png"
    path = tmp_path / "keyed-with-diagram.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    plan = MODULE.load_split_plan(path)

    assert plan.structure_diagram_filename == "structure-diagram.png"


def test_none_strategy_remains_backward_compatible(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    plan = MODULE.load_split_plan(path)

    assert plan.connection_strategy == "none"
    assert plan.connectors == ()
    assert plan.assembly_filename is None


def keyed_data() -> dict:
    return json.loads(KEYED_FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["connectors"][0].update(cut_id="missing"), "cut_id"),
        (lambda data: data["connectors"][0].update(male_piece="missing"), "male_piece"),
        (lambda data: data["connectors"][0].update(female_piece="head"), "distinct"),
        (lambda data: data["connectors"].append(dict(data["connectors"][0])), "unique"),
        (lambda data: data["connectors"][0].update(axis=[0, 0, 0]), "axis"),
        (lambda data: data["connectors"][0].update(key_direction=[0, 0, 0]), "key_direction"),
        (lambda data: data["connectors"][0].update(key_direction=[0, 0, 2]), "collinear"),
        (lambda data: data["connectors"][0].update(width_mm=0), "width_mm"),
        (lambda data: data["connectors"][0].update(corner_radius_mm=3), "corner_radius_mm"),
        (lambda data: data["connectors"][0].update(tip_chamfer_mm=7), "tip_chamfer_mm"),
        (lambda data: data["connectors"][0].update(clearance_per_side_mm=-0.1), "clearance"),
        (lambda data: data["connectors"][0].update(clearance_per_side_mm=1.1), "clearance"),
        (lambda data: data["connectors"][0].update(socket_bottom_clearance_mm=0), "socket"),
        (lambda data: data.update(connection_strategy="none"), "connectors"),
        (lambda data: data.update(connectors=[]), "connectors"),
        (lambda data: data.update(assembly_filename="nested/assembly.3mf"), "assembly_filename"),
        (
            lambda data: data.update(
                structure_diagram_filename="nested/structure-diagram.png"
            ),
            "structure_diagram_filename",
        ),
        (
            lambda data: data.update(structure_diagram_filename="diagram.jpg"),
            "structure_diagram_filename",
        ),
    ],
)
def test_keyed_plan_rejects_unsafe_or_ambiguous_connector_data(
    tmp_path, mutate, message
):
    data = keyed_data()
    mutate(data)
    path = tmp_path / "invalid-keyed.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(MODULE.SplitPlanError, match=message):
        MODULE.load_split_plan(path)


def test_connector_must_join_opposite_sides_of_its_cut(tmp_path):
    data = keyed_data()
    data["cuts"] = [
        {
            "id": "first",
            "input_piece": "source",
            "point_mm": [0, 0, 0],
            "normal": [1, 0, 0],
            "negative_piece": "left",
            "positive_piece": "rest",
        },
        {
            "id": "second",
            "input_piece": "rest",
            "point_mm": [1, 0, 0],
            "normal": [0, 1, 0],
            "negative_piece": "body",
            "positive_piece": "right",
        },
    ]
    data["connectors"][0].update(
        cut_id="first", male_piece="body", female_piece="right"
    )
    data["plates"][0]["piece_ids"] = ["left", "body", "right"]
    data["plates"][0]["layout"] = [
        {"piece_id": piece, "position_mm": [0, 0, 0], "rotation_deg": [0, 0, 0]}
        for piece in ("left", "body", "right")
    ]
    path = tmp_path / "same-side.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(MODULE.SplitPlanError, match="opposite sides"):
        MODULE.load_split_plan(path)


@pytest.mark.parametrize(
    ("key_path", "value", "message"),
    [
        ("split_requested", False, "split_requested"),
        ("connection_strategy", "", "connection_strategy"),
        ("connection_strategy", "pins", "only supports"),
        ("cuts", [], "cuts"),
        ("cuts.0.normal", [0, 0, 0], "normal"),
        ("plates.1.layout", None, "layout"),
        ("plates.1.piece_ids", ["piece-b1", "piece-b1"], "piece id"),
        ("plates.1.piece_ids", ["piece-b1", "piece-unknown"], "plate mapping"),
    ],
)
def test_split_plan_rejects_missing_or_ambiguous_user_decisions(
    tmp_path, key_path, value, message
):
    data = valid_data()
    target = data
    if "." in key_path:
        parts = key_path.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if part.isdigit() else target[part]
        target[parts[-1]] = value
    else:
        data[key_path] = value
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(MODULE.SplitPlanError, match=message):
        MODULE.load_split_plan(path)


def test_split_plan_rejects_consuming_piece_twice(tmp_path):
    data = valid_data()
    data["cuts"][1]["input_piece"] = "source"
    path = tmp_path / "invalid-tree.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(MODULE.SplitPlanError, match="active piece"):
        MODULE.load_split_plan(path)
