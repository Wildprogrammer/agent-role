import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_manifest.py"
SPEC = importlib.util.spec_from_file_location("workflow_3d_run_manifest", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
RunManifest = MODULE.RunManifest
StageError = MODULE.StageError


def ready_manifest() -> RunManifest:
    manifest = RunManifest(run_id="demo")
    manifest.confirm_constraints()
    manifest.record_model("outputs/demo/source.blend")
    manifest.record_validation("outputs/demo/validation.json", passed=True)
    manifest.confirm_slicer("prusaslicer", "profiles/mk4-pla.ini")
    return manifest


def test_cannot_slice_before_gate_b():
    manifest = RunManifest(run_id="demo")

    with pytest.raises(StageError, match="slicer confirmation"):
        manifest.record_gcode("outputs/demo/model.gcode")


def test_switching_provider_resets_gate_b():
    manifest = ready_manifest()
    manifest.switch_slicer("orcaslicer")

    assert not manifest.slicer_confirmed
    with pytest.raises(StageError, match="slicer confirmation"):
        manifest.record_gcode("outputs/demo/model.gcode")


def test_delivery_requires_final_review():
    manifest = ready_manifest()
    manifest.record_gcode("outputs/demo/model.gcode")

    with pytest.raises(StageError, match="final review"):
        manifest.deliver()
    manifest.confirm_final_review()
    manifest.deliver()
    assert manifest.status == "done"
