import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "headless_cut.py"
SMOKE_SCRIPT = Path(__file__).parent / "blender_connector_smoke.py"
SPEC = importlib.util.spec_from_file_location("workflow_headless_cut", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PLAN_SCRIPT = Path(__file__).parents[1] / "scripts" / "split_plan.py"
PLAN_SPEC = importlib.util.spec_from_file_location("workflow_split_plan_for_cut", PLAN_SCRIPT)
assert PLAN_SPEC and PLAN_SPEC.loader
PLAN_MODULE = importlib.util.module_from_spec(PLAN_SPEC)
sys.modules[PLAN_SPEC.name] = PLAN_MODULE
PLAN_SPEC.loader.exec_module(PLAN_MODULE)


FIXTURE = Path("workflows/3d-printing/tests/fixtures/split-three-pieces.json")
KEYED_FIXTURE = Path("workflows/3d-printing/tests/fixtures/split-keyed-pin.json")


def split_plan(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return PLAN_MODULE.load_split_plan(path)


def keyed_plan(tmp_path):
    path = tmp_path / "keyed-plan.json"
    path.write_text(KEYED_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return PLAN_MODULE.load_split_plan(path)


def valid_evidence():
    return {
        "source_model_sha256": "a" * 64,
        "script_sha256": "b" * 64,
        "imported_script_sha256": MODULE.local_script_hashes(),
        "cut_ids": ["cut-01", "cut-02"],
        "source_volume_mm3": 3.0,
        "volume_sum_mm3": 3.0,
        "pieces": [
            {
                "piece_id": "piece-a",
                "sha256": "c" * 64,
                "stats": {
                    "connected_components": 1,
                    "components": [{"face_count": 1, "volume_mm3": 1.0}],
                    "boundary_edges": 0,
                    "non_manifold_edges": 0,
                    "self_intersections": 0,
                    "minimum_wall_mm": 0.8,
                    "volume_mm3": 1.0,
                },
            },
            {
                "piece_id": "piece-b1",
                "sha256": "d" * 64,
                "stats": {
                    "connected_components": 1,
                    "components": [{"face_count": 1, "volume_mm3": 1.0}],
                    "boundary_edges": 0,
                    "non_manifold_edges": 0,
                    "self_intersections": 0,
                    "minimum_wall_mm": 0.8,
                    "volume_mm3": 1.0,
                },
            },
            {
                "piece_id": "piece-b2",
                "sha256": "e" * 64,
                "stats": {
                    "connected_components": 1,
                    "components": [{"face_count": 1, "volume_mm3": 1.0}],
                    "boundary_edges": 0,
                    "non_manifold_edges": 0,
                    "self_intersections": 0,
                    "minimum_wall_mm": 0.8,
                    "volume_mm3": 1.0,
                },
            },
        ],
        "validation": {
            "source_unchanged": True,
            "piece_count": True,
            "all_piece_files_nonempty": True,
        },
    }


def valid_keyed_evidence():
    return {
        "source_model_sha256": "a" * 64,
        "script_sha256": "b" * 64,
        "imported_script_sha256": MODULE.local_script_hashes(),
        "cut_ids": ["neck"],
        "source_volume_mm3": 300.0,
        "volume_sum_mm3": 295.0,
        "pieces": [
            {
                "piece_id": "body",
                "sha256": "c" * 64,
                "stats": {
                    "connected_components": 1,
                    "components": [{"face_count": 1, "volume_mm3": 170.0}],
                    "boundary_edges": 0,
                    "non_manifold_edges": 0,
                    "self_intersections": 0,
                    "minimum_wall_mm": 1.3,
                    "volume_mm3": 170.0,
                },
            },
            {
                "piece_id": "head",
                "sha256": "d" * 64,
                "stats": {
                    "connected_components": 1,
                    "components": [{"face_count": 1, "volume_mm3": 125.0}],
                    "boundary_edges": 0,
                    "non_manifold_edges": 0,
                    "self_intersections": 0,
                    "minimum_wall_mm": 1.3,
                    "volume_mm3": 125.0,
                },
            },
        ],
        "connector_evidence": {
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
        },
        "validation": {
            "source_unchanged": True,
            "piece_count": True,
            "all_piece_files_nonempty": True,
        },
    }


def test_blender_command_is_background_only(tmp_path):
    command = MODULE.build_blender_cut_command(
        Path("C:/Blender/blender.exe"),
        Path("headless_cut.py"),
        Path("source.stl"),
        Path("split-plan.json"),
        tmp_path / "run",
    )

    assert command[:4] == (
        "C:\\Blender\\blender.exe",
        "--background",
        "--python",
        str(Path("headless_cut.py").resolve()),
    )
    assert "--source" in command
    assert "--plan" in command
    assert "--output-dir" in command


def test_cut_evidence_accepts_complete_geometry_evidence(tmp_path):
    evidence = valid_evidence()
    result = MODULE.validate_cut_evidence(
        evidence,
        split_plan(tmp_path),
        source_sha256="a" * 64,
        script_sha256="b" * 64,
    )

    assert result["piece_count"] == 3
    assert result["volume_relative_error"] == 0


def test_cut_evidence_accepts_light_validation_with_deferred_expensive_checks(
    tmp_path,
):
    evidence = valid_evidence()
    evidence["validation_level"] = "light"
    for piece in evidence["pieces"]:
        piece["stats"]["self_intersections"] = "not_evaluated"
        piece["stats"]["minimum_wall_mm"] = "not_evaluated"

    result = MODULE.validate_cut_evidence(
        evidence,
        split_plan(tmp_path),
        source_sha256="a" * 64,
        script_sha256="b" * 64,
    )

    assert result["validation_level"] == "light"
    assert result["deferred_checks"] == [
        "full_self_intersection",
        "exhaustive_wall_thickness",
        "printability_and_slicer_import",
    ]


def test_headless_cut_defaults_to_light_validation(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "headless_cut.py",
            "--source",
            "source.3mf",
            "--plan",
            "split-plan.json",
            "--output-dir",
            "run",
        ],
    )

    assert MODULE._parse_args().validation_level == "light"


def test_cut_evidence_expands_environment_variables_in_source_path(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.3mf"
    source.write_bytes(b"portable source")
    source_sha = MODULE.sha256_file(source)
    monkeypatch.setenv("WORKFLOW_TEST_ROOT", str(tmp_path))
    evidence = valid_evidence()
    evidence["source_model_sha256"] = source_sha
    evidence["source_model"] = "%WORKFLOW_TEST_ROOT%/source.3mf"

    result = MODULE.validate_cut_evidence(
        evidence,
        split_plan(tmp_path),
        source_sha256=source_sha,
        script_sha256="b" * 64,
    )

    assert result["piece_count"] == 3


def test_cut_evidence_accepts_connector_explained_volume_delta(tmp_path):
    result = MODULE.validate_cut_evidence(
        valid_keyed_evidence(),
        keyed_plan(tmp_path),
        source_sha256="a" * 64,
        script_sha256="b" * 64,
    )

    assert result["piece_count"] == 2
    assert result["connector_count"] == 1
    assert result["expected_volume_delta_mm3"] == -5.0
    assert result["volume_relative_error"] == 0


def test_cut_evidence_rejects_unexplained_connector_volume_delta(tmp_path):
    evidence = valid_keyed_evidence()
    evidence["volume_sum_mm3"] = 300.0
    evidence["pieces"][0]["stats"]["volume_mm3"] = 175.0

    with pytest.raises(MODULE.CutEvidenceError, match="volume tolerance"):
        MODULE.validate_cut_evidence(
            evidence,
            keyed_plan(tmp_path),
            source_sha256="a" * 64,
            script_sha256="b" * 64,
        )


def test_cut_evidence_rejects_disconnected_final_stl(tmp_path):
    evidence = valid_evidence()
    evidence["pieces"][0]["stats"]["connected_components"] = 2

    with pytest.raises(MODULE.CutEvidenceError, match="connected components"):
        MODULE.validate_cut_evidence(
            evidence,
            split_plan(tmp_path),
            source_sha256="a" * 64,
            script_sha256="b" * 64,
        )


def test_cut_evidence_rejects_changed_imported_script(tmp_path):
    evidence = valid_evidence()
    evidence["imported_script_sha256"]["bounded_cut.py"] = "f" * 64

    with pytest.raises(MODULE.CutEvidenceError, match="imported script hash"):
        MODULE.validate_cut_evidence(
            evidence,
            split_plan(tmp_path),
            source_sha256="a" * 64,
            script_sha256="b" * 64,
        )


def test_headless_run_uses_bounded_component_splitter():
    source = SCRIPT.read_text(encoding="utf-8")
    run_source = source[source.index("def _run_blender") :]

    assert "from bounded_cut import" in source
    assert "bounded_split(" in run_source
    assert "component_assignment" in run_source


def test_local_script_hashes_cover_every_runtime_import():
    hashes = MODULE.local_script_hashes()

    assert set(hashes) == {
        "bounded_cut.py",
        "component_selection.py",
        "connector_geometry.py",
        "split_plan.py",
        "standard_3mf.py",
        "structure_diagram.py",
        "three_mf_import.py",
    }
    assert all(len(digest) == 64 for digest in hashes.values())


def test_headless_run_renders_only_an_explicitly_requested_structure_diagram():
    source = SCRIPT.read_text(encoding="utf-8")
    run_source = source[source.index("def _run_blender") :]

    assert "from structure_diagram import render_structure_diagram" in source
    assert "if plan.structure_diagram_filename is not None" in run_source
    assert "render_structure_diagram(" in run_source
    assert '"structure_diagram": structure_diagram' in run_source


def test_keyed_cut_targets_the_male_connector_branch(tmp_path):
    plan = keyed_plan(tmp_path)
    cut = plan.cuts[0]

    assert MODULE._cut_target_side(plan, cut) == "positive"
    assert MODULE._connector_seed_points(plan, cut) == {"neck-a": (0.0, 0.0, 0.4)}


def test_plain_cut_preserves_positive_target_semantics(tmp_path):
    plan = split_plan(tmp_path)

    assert MODULE._cut_target_side(plan, plan.cuts[0]) == "positive"
    assert MODULE._connector_seed_points(plan, plan.cuts[0]) == {}


def test_headless_cutter_applies_imported_object_transform_before_cutting():
    source = SCRIPT.read_text(encoding="utf-8")
    run_source = source[source.index("def _run_blender") :]

    assert "transform_apply(location=True, rotation=True, scale=True)" in run_source
    assert run_source.index("transform_apply(location=True, rotation=True, scale=True)") < run_source.index(
        "for cut in plan.cuts"
    )


def test_headless_3mf_import_uses_internal_streaming_parser():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "from three_mf_import import" in source
    assert "load_3mf_mesh" in source
    assert "bpy.ops.wm.threemf_import" not in source


def test_headless_entrypoint_forces_nonzero_exit_on_blender_python_failure():
    source = SCRIPT.read_text(encoding="utf-8")

    assert "os._exit(1)" in source


def test_source_match_expands_windows_userprofile_placeholder(
    tmp_path, monkeypatch
):
    user_home = tmp_path / "user-home"
    source = user_home / "Downloads" / "月薪喵.3mf"
    plan_path = tmp_path / "run" / "split-plan.json"
    monkeypatch.setenv("USERPROFILE", str(user_home))

    assert MODULE._source_matches_plan(
        plan_path,
        source,
        r"%USERPROFILE%\Downloads\月薪喵.3mf",
    )


def test_standard_assembly_uses_identity_layout_for_world_space_pieces(tmp_path):
    transforms = MODULE._assembly_transforms(keyed_plan(tmp_path))

    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    assert transforms == {"body": identity, "head": identity}


def test_standard_assembly_rejects_print_layout_as_assembly_transform(tmp_path):
    data = json.loads(KEYED_FIXTURE.read_text(encoding="utf-8"))
    data["plates"][0]["layout"][0]["position_mm"] = [20, 20, 0]
    path = tmp_path / "moved-plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(MODULE.CutEvidenceError, match="identity layout"):
        MODULE._assembly_transforms(PLAN_MODULE.load_split_plan(path))


def test_blender_smoke_forces_nonzero_process_exit_on_python_exception():
    source = SMOKE_SCRIPT.read_text(encoding="utf-8")

    assert "os._exit(1)" in source


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("source_model_sha256", "f" * 64), "source hash"),
        (("script_sha256", "f" * 64), "script hash"),
        (("cut_ids", ["cut-01"]), "cut sequence"),
        (("volume_sum_mm3", 4.0), "volume"),
    ],
)
def test_cut_evidence_rejects_changed_invariants(tmp_path, change, message):
    evidence = valid_evidence()
    evidence[change[0]] = change[1]

    with pytest.raises(MODULE.CutEvidenceError, match=message):
        MODULE.validate_cut_evidence(
            evidence,
            split_plan(tmp_path),
            source_sha256="a" * 64,
            script_sha256="b" * 64,
        )


def test_cut_evidence_rejects_unmeasured_wall_or_intersections(tmp_path):
    evidence = valid_evidence()
    evidence["pieces"][0]["stats"]["minimum_wall_mm"] = "not_evaluated"

    with pytest.raises(MODULE.CutEvidenceError, match="not evaluated"):
        MODULE.validate_cut_evidence(
            evidence,
            split_plan(tmp_path),
            source_sha256="a" * 64,
            script_sha256="b" * 64,
        )
