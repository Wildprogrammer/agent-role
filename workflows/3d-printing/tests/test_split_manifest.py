import importlib.util
import json
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "split_manifest.py"
SPEC = importlib.util.spec_from_file_location("workflow_split_manifest", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PLAN_SCRIPT = Path(__file__).parents[1] / "scripts" / "split_plan.py"
PLAN_SPEC = importlib.util.spec_from_file_location("workflow_split_plan_for_manifest", PLAN_SCRIPT)
assert PLAN_SPEC and PLAN_SPEC.loader
PLAN_MODULE = importlib.util.module_from_spec(PLAN_SPEC)
sys.modules[PLAN_SPEC.name] = PLAN_MODULE
PLAN_SPEC.loader.exec_module(PLAN_MODULE)


FIXTURE = Path("workflows/3d-printing/tests/fixtures/split-three-pieces.json")
KEYED_FIXTURE = Path("workflows/3d-printing/tests/fixtures/split-keyed-pin.json")
IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def plan(tmp_path):
    path = tmp_path / "plan.json"
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return PLAN_MODULE.load_split_plan(path)


def keyed_plan(tmp_path):
    path = tmp_path / "keyed-plan.json"
    path.write_text(KEYED_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return PLAN_MODULE.load_split_plan(path)


def diagram_plan(tmp_path):
    data = json.loads(KEYED_FIXTURE.read_text(encoding="utf-8"))
    data.pop("assembly_filename")
    data["structure_diagram_filename"] = "structure-diagram.png"
    path = tmp_path / "diagram-plan.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return PLAN_MODULE.load_split_plan(path)


def evidence():
    pieces = [
        ("piece-a", "c" * 64),
        ("piece-b1", "d" * 64),
        ("piece-b2", "e" * 64),
    ]
    return {
        "source_model_sha256": "a" * 64,
        "script_sha256": "b" * 64,
        "imported_script_sha256": MODULE.local_script_hashes(),
        "cut_ids": ["cut-01", "cut-02"],
        "piece_hashes": {
            "piece-a": "c" * 64,
            "piece-b1": "d" * 64,
            "piece-b2": "e" * 64,
        },
        "source_volume_mm3": 3.0,
        "volume_sum_mm3": 3.0,
        "pieces": [
            {
                "piece_id": piece_id,
                "sha256": digest,
                "stats": {
                    "connected_components": 1,
                    "components": [{"face_count": 1, "volume_mm3": 1.0}],
                    "boundary_edges": 0,
                    "non_manifold_edges": 0,
                    "self_intersections": 0,
                    "minimum_wall_mm": 0.8,
                    "volume_mm3": 1.0,
                },
            }
            for piece_id, digest in pieces
        ],
        "validation": {
            "source_unchanged": True,
            "piece_count": True,
            "all_piece_files_nonempty": True,
        },
    }


def ready_manifest(tmp_path):
    manifest = MODULE.SplitRunManifest(run_id="demo")
    current_plan = plan(tmp_path)
    manifest.record_explicit_split_request(current_plan.reason)
    manifest.confirm_split_plan(current_plan, source_sha256="a" * 64)
    manifest.record_cut_evidence(evidence())
    manifest.record_geometry_validation("validation.json", passed=True)
    manifest.confirm_bambu_provider(
        {
            "provider_id": "app.bambu-studio",
            "smoke_status": "passed",
            "host": "codex",
            "version": "2.7.1.62",
            "printer_model": "Bambu Lab A1 mini",
            "nozzle_mm": 0.4,
            "print_settings_id": "0.20mm Standard @BBL A1M",
        }
    )
    return manifest, current_plan


def keyed_evidence():
    return {
        "source_model_sha256": "a" * 64,
        "script_sha256": "b" * 64,
        "imported_script_sha256": MODULE.local_script_hashes(),
        "cut_ids": ["neck"],
        "piece_hashes": {"body": "c" * 64, "head": "d" * 64},
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


def standard_result(tmp_path):
    path = tmp_path / "fixture-assembly.3mf"
    path.write_bytes(b"verified standard package")
    return MODULE.Standard3MFVerification(
        valid=True,
        package_path=str(path),
        sha256=MODULE.standard_sha256_file(path),
        object_names=("body", "head"),
        transforms={"body": IDENTITY, "head": IDENTITY},
        gcode_members=(),
    )


def standard_ready_manifest(tmp_path):
    manifest = MODULE.SplitRunManifest(run_id="standard-demo")
    current_plan = keyed_plan(tmp_path)
    manifest.record_explicit_split_request(current_plan.reason)
    manifest.confirm_split_plan(current_plan, source_sha256="a" * 64)
    manifest.record_cut_evidence(keyed_evidence())
    manifest.record_geometry_validation("validation.json", passed=True)
    return manifest, current_plan


def _write_png(path, width=1800, height=1400):
    def chunk(kind, payload):
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\0" + b"\0\0\0" * width for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk("IHDR".encode(), struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk("IDAT".encode(), zlib.compress(rows))
        + chunk("IEND".encode(), b"")
    )
    return path


def diagram_ready_manifest(tmp_path):
    diagram = _write_png(tmp_path / "structure-diagram.png")
    current_plan = diagram_plan(tmp_path)
    current_evidence = keyed_evidence()
    current_evidence["structure_diagram"] = {
        "path": str(diagram),
        "sha256": MODULE.standard_sha256_file(diagram),
        "width_px": 1800,
        "height_px": 1400,
    }
    manifest = MODULE.SplitRunManifest(run_id="diagram-demo")
    manifest.record_explicit_split_request(current_plan.reason)
    manifest.confirm_split_plan(current_plan, source_sha256="a" * 64)
    manifest.record_cut_evidence(current_evidence)
    manifest.record_geometry_validation("validation.json", passed=True)
    return manifest, current_plan, diagram


def package_result(tmp_path, plate_id, object_names, *, valid=True):
    path = tmp_path / f"{plate_id}.gcode.3mf"
    path.write_bytes(b"verified package placeholder")
    return MODULE.PackageVerification(
        valid=valid,
        package_path=str(path),
        gcode_member="Metadata/plate_1.gcode",
        gcode_sha256="f" * 64,
        gcode_md5="A" * 32,
        object_names=tuple(sorted(object_names)),
        bbox_all=(10.0, 10.0, 20.0, 20.0),
        printer_model="Bambu Lab A1 mini",
        nozzle_mm=0.4,
        print_settings_id="0.20mm Standard @BBL A1M",
    )


def test_split_manifest_requires_explicit_request():
    manifest = MODULE.SplitRunManifest(run_id="demo")
    with pytest.raises(MODULE.StageError, match="explicit split request"):
        manifest.confirm_split_plan(SimpleNamespace(), source_sha256="a" * 64)


def test_split_manifest_records_gates_and_blocks_missing_plate_package(tmp_path):
    manifest, _ = ready_manifest(tmp_path)
    manifest.record_plate_package("plate-01", package_result(tmp_path, "plate-01", ("piece-a",)))

    with pytest.raises(MODULE.StageError, match="missing plate packages"):
        manifest.confirm_final_review()

    assert manifest.printer_started is False
    assert manifest.status == "in_progress"


def test_split_manifest_requires_all_unique_verified_packages(tmp_path):
    manifest, current_plan = ready_manifest(tmp_path)
    for plate in current_plan.plates:
        manifest.record_plate_package(
            plate.id,
            package_result(tmp_path, plate.id, plate.piece_ids),
        )

    manifest.confirm_final_review()
    manifest.deliver()

    assert manifest.status == "done"
    assert manifest.final_review_confirmed is True
    assert manifest.printer_started is False


def test_split_manifest_marks_geometry_mismatch_without_guessing(tmp_path):
    current_plan = plan(tmp_path)
    manifest = MODULE.SplitRunManifest(run_id="demo")
    manifest.record_explicit_split_request(current_plan.reason)
    manifest.confirm_split_plan(current_plan, source_sha256="a" * 64)
    broken = evidence()
    broken["cut_ids"] = ["cut-01"]

    with pytest.raises(MODULE.StageError, match="cut sequence"):
        manifest.record_cut_evidence(broken)

    assert manifest.status == "needs_geometry_repair"


def test_split_manifest_requires_host_specific_bambu_smoke(tmp_path):
    current_plan = plan(tmp_path)
    manifest = MODULE.SplitRunManifest(run_id="demo")
    manifest.record_explicit_split_request(current_plan.reason)
    manifest.confirm_split_plan(current_plan, source_sha256="a" * 64)
    manifest.record_cut_evidence(evidence())
    manifest.record_geometry_validation("validation.json", passed=True)

    with pytest.raises(MODULE.StageError, match="smoke"):
        manifest.confirm_bambu_provider(
            {
                "provider_id": "app.bambu-studio",
                "smoke_status": "pending",
                "host": "codex",
                "version": "2.7.1.62",
                "printer_model": "Bambu Lab A1 mini",
                "nozzle_mm": 0.4,
                "print_settings_id": "0.20mm Standard @BBL A1M",
            }
        )

    assert manifest.status == "needs_provider_support"


def test_split_manifest_rejects_unverified_package_and_stops_provider_path(
    tmp_path,
):
    manifest, _ = ready_manifest(tmp_path)

    with pytest.raises(MODULE.StageError, match="provider support"):
        manifest.record_plate_package(
            "plate-01",
            package_result(tmp_path, "plate-01", ("piece-a",), valid=False),
        )

    assert manifest.status == "needs_provider_support"
    with pytest.raises(MODULE.StageError, match="provider support"):
        manifest.confirm_final_review()


def test_split_manifest_serializes_single_authoritative_state(tmp_path):
    manifest, _ = ready_manifest(tmp_path)
    payload = manifest.to_dict()

    assert payload["run_id"] == "demo"
    assert payload["printer_started"] is False
    assert payload["plate_mapping"]["plate-01"] == ["piece-a"]
    assert json.loads(json.dumps(payload))["status"] == "in_progress"


def test_split_manifest_keeps_only_compact_cut_evidence_summary(tmp_path):
    manifest = MODULE.SplitRunManifest(run_id="compact")
    current_plan = plan(tmp_path)
    current_evidence = evidence()
    current_evidence["validation_level"] = "light"
    current_evidence["status"] = "generated_for_user_review"
    current_evidence["structure_diagram"] = {
        "path": "structure-diagram.png",
        "sha256": "f" * 64,
        "width_px": 1800,
        "height_px": 1400,
        "debug_layout": {"centers": {"piece-a": [1, 2, 3]}},
    }
    manifest.record_explicit_split_request(current_plan.reason)
    manifest.confirm_split_plan(current_plan, source_sha256="a" * 64)
    manifest.record_cut_evidence(current_evidence)

    assert manifest.cut_evidence["validation_level"] == "light"
    assert manifest.cut_evidence["status"] == "generated_for_user_review"
    assert manifest.cut_evidence["piece_hashes"] == current_evidence["piece_hashes"]
    assert "pieces" not in manifest.cut_evidence
    assert "connector_evidence" not in manifest.cut_evidence
    assert "debug_layout" not in manifest.cut_evidence["structure_diagram"]


def test_gate_c_records_explicit_user_acceptance_for_light_validation(tmp_path):
    manifest, _current_plan, diagram = diagram_ready_manifest(tmp_path)
    manifest.cut_evidence["validation_level"] = "light"
    manifest.record_stl_diagram_artifacts(
        {"body": "c" * 64, "head": "d" * 64},
        diagram,
        diagram_sha256=MODULE.standard_sha256_file(diagram),
        width_px=1800,
        height_px=1400,
    )

    manifest.confirm_final_review()

    assert manifest.user_review_status == "accepted_by_user"
    assert manifest.to_dict()["user_review_status"] == "accepted_by_user"


def test_standard_mesh_artifacts_can_deliver_without_bambu_provider(tmp_path):
    manifest, _current_plan = standard_ready_manifest(tmp_path)
    manifest.record_standard_artifacts(
        {"body": "c" * 64, "head": "d" * 64},
        standard_result(tmp_path),
    )

    manifest.confirm_final_review()
    manifest.deliver()

    assert manifest.status == "done"
    assert manifest.delivery_target == "stl+standard-3mf"
    assert manifest.bambu_provider is None
    assert manifest.plate_packages == {}


def test_standard_mesh_delivery_blocks_missing_or_mixed_artifacts(tmp_path):
    manifest, _current_plan = standard_ready_manifest(tmp_path)

    with pytest.raises(MODULE.StageError, match="standard artifacts"):
        manifest.confirm_final_review()

    manifest.record_standard_artifacts(
        {"body": "c" * 64, "head": "d" * 64},
        standard_result(tmp_path),
    )
    with pytest.raises(MODULE.StageError, match="delivery target"):
        manifest.confirm_bambu_provider(
            {
                "provider_id": "app.bambu-studio",
                "smoke_status": "passed",
                "host": "codex",
                "version": "2.7.1.62",
                "printer_model": "Bambu Lab A1 mini",
                "nozzle_mm": 0.4,
                "print_settings_id": "0.20mm Standard @BBL A1M",
            }
        )


def test_standard_mesh_delivery_rejects_wrong_stl_or_package_mapping(tmp_path):
    manifest, _current_plan = standard_ready_manifest(tmp_path)

    with pytest.raises(MODULE.StageError, match="STL hash set"):
        manifest.record_standard_artifacts(
            {"body": "c" * 64}, standard_result(tmp_path)
        )

    broken = standard_result(tmp_path)
    broken = MODULE.Standard3MFVerification(
        valid=True,
        package_path=broken.package_path,
        sha256=broken.sha256,
        object_names=("body",),
        transforms={"body": IDENTITY},
        gcode_members=(),
    )
    with pytest.raises(MODULE.StageError, match="object mapping"):
        manifest.record_standard_artifacts(
            {"body": "c" * 64, "head": "d" * 64}, broken
        )


def test_stl_diagram_artifacts_can_deliver_without_3mf_or_provider(tmp_path):
    manifest, _current_plan, diagram = diagram_ready_manifest(tmp_path)

    manifest.record_stl_diagram_artifacts(
        {"body": "c" * 64, "head": "d" * 64},
        diagram,
        diagram_sha256=MODULE.standard_sha256_file(diagram),
        width_px=1800,
        height_px=1400,
    )
    manifest.confirm_final_review()
    manifest.deliver()
    payload = manifest.to_dict()

    assert payload["status"] == "done"
    assert payload["delivery_target"] == "stl+structure-diagram"
    assert payload["standard_3mf"] is None
    assert payload["structure_diagram"]["width_px"] == 1800
    assert payload["bambu_provider"] is None
    assert payload["plate_packages"] == {}


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("stl-set", "STL hash set"),
        ("digest", "diagram hash mismatch"),
        ("dimensions", "diagram dimensions mismatch"),
        ("signature", "PNG"),
    ],
)
def test_stl_diagram_delivery_rejects_unverified_artifacts(
    tmp_path, mutation, message
):
    manifest, _current_plan, diagram = diagram_ready_manifest(tmp_path)
    stl_hashes = {"body": "c" * 64, "head": "d" * 64}
    digest = MODULE.standard_sha256_file(diagram)
    width, height = 1800, 1400
    if mutation == "stl-set":
        stl_hashes.pop("head")
    elif mutation == "digest":
        digest = "f" * 64
    elif mutation == "dimensions":
        width = 1799
    elif mutation == "signature":
        diagram.write_bytes(b"not a PNG")
        digest = MODULE.standard_sha256_file(diagram)

    with pytest.raises(MODULE.StageError, match=message):
        manifest.record_stl_diagram_artifacts(
            stl_hashes,
            diagram,
            diagram_sha256=digest,
            width_px=width,
            height_px=height,
        )


def test_manifest_serializes_all_external_side_effects_as_false(tmp_path):
    manifest, _current_plan = standard_ready_manifest(tmp_path)
    payload = manifest.to_dict()

    assert payload["upload"] is False
    assert payload["send"] is False
    assert payload["queue"] is False
    assert payload["printer_started"] is False
