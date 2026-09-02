import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "inspect_mesh.py"
SPEC = importlib.util.spec_from_file_location("workflow_mesh_inspection", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PLAN_SCRIPT = Path(__file__).parents[1] / "scripts" / "split_plan.py"
PLAN_SPEC = importlib.util.spec_from_file_location(
    "workflow_split_plan_for_inspection", PLAN_SCRIPT
)
assert PLAN_SPEC and PLAN_SPEC.loader
PLAN_MODULE = importlib.util.module_from_spec(PLAN_SPEC)
sys.modules[PLAN_SPEC.name] = PLAN_MODULE
PLAN_SPEC.loader.exec_module(PLAN_MODULE)

FIXTURE = Path("workflows/3d-printing/tests/fixtures/split-keyed-pin.json")
VIEW_IDS = ("front", "back", "left", "right", "top", "bottom")


def plan(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return PLAN_MODULE.load_split_plan(path), path


def evidence(tmp_path, *, with_candidates=False):
    views = {}
    for view_id in VIEW_IDS:
        path = tmp_path / f"{view_id}.png"
        path.write_bytes(b"png")
        views[view_id] = str(path)
    result = {
        "status": "ready_for_review",
        "source_model_sha256": "a" * 64,
        "source_sha256_before": "a" * 64,
        "source_sha256_after": "a" * 64,
        "units": "mm",
        "bounds": {
            "min_mm": [-10.0, -10.0, -10.0],
            "max_mm": [10.0, 10.0, 10.0],
            "dimensions_mm": [20.0, 20.0, 20.0],
        },
        "views": views,
        "candidate_cuts": [],
        "candidate_connectors": [],
    }
    if with_candidates:
        result.update(
            status="ready_for_gate_a",
            candidate_cuts=[
                {
                    "id": "neck",
                    "point_mm": [0.0, 0.0, 0.0],
                    "normal": [0.0, 0.0, 1.0],
                    "section_width_mm": 20.0,
                    "section_height_mm": 20.0,
                }
            ],
            candidate_connectors=[
                {
                    "id": "neck-a",
                    "cut_id": "neck",
                    "center_mm": [0.0, 0.0, 0.0],
                    "axis": [0.0, 0.0, -1.0],
                    "section_outline_inside": True,
                    "suggested_center_mm": [0.0, 0.0, 0.0],
                    "suggested_minimum_edge_margin_mm": 5.0,
                    "minimum_edge_margin_mm": 2.0,
                    "estimated_minimum_wall_mm": 2.0,
                    "available_depth_mm": 9.0,
                    "required_socket_depth_mm": 7.5,
                }
            ],
        )
    return result


def test_blender_inspection_command_is_background_and_read_only(tmp_path):
    _current_plan, plan_path = plan(tmp_path)
    command = MODULE.build_blender_inspection_command(
        Path("C:/Blender/blender.exe"),
        Path("inspect_mesh.py"),
        Path("source.3mf"),
        tmp_path / "inspection",
        candidate_plan=plan_path,
    )

    assert command[:3] == (
        "C:\\Blender\\blender.exe",
        "--background",
        "--python",
    )
    assert "--source" in command
    assert "--output-dir" in command
    assert "--candidate-plan" in command
    assert not any(token.lower() in {"--render-anim", "--save", "--print"} for token in command)


def test_inspection_entrypoint_forces_nonzero_exit_on_blender_python_failure():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "os._exit(1)" in source


def test_six_view_renderer_initializes_a_world_for_empty_factory_scenes():
    source = SCRIPT.read_text(encoding="utf-8")
    render_source = source[source.index("def _render_views") : source.index("def _parse_args")]

    assert "if scene.world is None:" in render_source
    assert "bpy.data.worlds.new" in render_source
    assert "scene.world.color = (0.02, 0.02, 0.02)" in render_source


def test_candidate_inspection_measures_the_sequential_input_pieces():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "from headless_cut import _cut_piece, _import_source, sha256_file" in source
    assert "def _measure_candidate_sequence" in source
    assert "target = pieces.pop(cut.input_piece)" in source
    assert "_section_measurement(target, cut)" in source
    assert "pieces[connector.female_piece]" in source


def test_candidate_inspection_renders_colored_leaf_pieces_with_emission_materials():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "return cut_records, connector_records, pieces" in source
    assert "source_obj.hide_render = True" in source
    assert "_assign_emission_material(piece" in source
    assert 'nodes.new("ShaderNodeEmission")' in source


def test_section_fit_recommends_an_interior_connector_center(tmp_path):
    current_plan, _path = plan(tmp_path)
    connector = replace(current_plan.connectors[0], center_mm=(9.0, 0.0, 0.0))
    square = {
        "segments": [
            [(-10.0, -10.0, 0.0), (10.0, -10.0, 0.0)],
            [(10.0, -10.0, 0.0), (10.0, 10.0, 0.0)],
            [(10.0, 10.0, 0.0), (-10.0, 10.0, 0.0)],
            [(-10.0, 10.0, 0.0), (-10.0, -10.0, 0.0)],
        ]
    }

    result = MODULE._connector_section_fit(connector, square)

    assert result["section_outline_inside"] is False
    assert result["suggested_center_mm"] == pytest.approx([0.0, 0.0, 0.0])
    assert result["suggested_minimum_edge_margin_mm"] >= 6.0


def test_section_fit_recenters_an_inside_connector_with_low_corner_margin(tmp_path):
    current_plan, _path = plan(tmp_path)
    connector = replace(current_plan.connectors[0], center_mm=(6.0, 0.0, 0.0))
    square = {
        "segments": [
            [(-10.0, -10.0, 0.0), (10.0, -10.0, 0.0)],
            [(10.0, -10.0, 0.0), (10.0, 10.0, 0.0)],
            [(10.0, 10.0, 0.0), (-10.0, 10.0, 0.0)],
            [(-10.0, 10.0, 0.0), (-10.0, -10.0, 0.0)],
        ]
    }

    result = MODULE._connector_section_fit(connector, square)

    assert result["section_outline_inside"] is True
    assert result["suggested_center_mm"] == pytest.approx([0.0, 0.0, 0.0])
    assert result["suggested_minimum_edge_margin_mm"] >= 6.0


def test_base_inspection_evidence_requires_six_nonempty_views(tmp_path):
    result = MODULE.validate_inspection_evidence(
        evidence(tmp_path), source_sha256="a" * 64
    )

    assert result["view_count"] == 6
    assert result["candidate_cut_count"] == 0
    assert result["ready_for_gate_a"] is False


def test_candidate_inspection_covers_every_approved_cut_and_connector(tmp_path):
    current_plan, _path = plan(tmp_path)

    result = MODULE.validate_inspection_evidence(
        evidence(tmp_path, with_candidates=True),
        source_sha256="a" * 64,
        candidate_plan=current_plan,
    )

    assert result["candidate_cut_count"] == 1
    assert result["candidate_connector_count"] == 1
    assert result["ready_for_gate_a"] is True


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update(source_sha256_after="b" * 64), "source"),
        (lambda data: data["views"].pop("bottom"), "six views"),
        (lambda data: data["bounds"].update(dimensions_mm=[20.0, 0.0, 20.0]), "dimensions"),
        (lambda data: data.update(candidate_cuts=[]), "candidate cut"),
        (
            lambda data: data["candidate_connectors"][0].update(
                minimum_edge_margin_mm=1.0
            ),
            "edge margin",
        ),
        (
            lambda data: data["candidate_connectors"][0].update(
                estimated_minimum_wall_mm=1.0
            ),
            "wall",
        ),
        (
            lambda data: data["candidate_connectors"][0].update(
                available_depth_mm=7.0
            ),
            "depth",
        ),
        (
            lambda data: data["candidate_connectors"][0].update(
                section_outline_inside=False
            ),
            "section outline",
        ),
    ],
)
def test_candidate_inspection_rejects_missing_or_unsafe_evidence(
    tmp_path, mutate, message
):
    current_plan, _path = plan(tmp_path)
    data = evidence(tmp_path, with_candidates=True)
    mutate(data)

    with pytest.raises(MODULE.InspectionEvidenceError, match=message):
        MODULE.validate_inspection_evidence(
            data,
            source_sha256="a" * 64,
            candidate_plan=current_plan,
        )
