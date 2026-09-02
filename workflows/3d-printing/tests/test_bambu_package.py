import importlib.util
import hashlib
import json
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "bambu_package.py"
SPEC = importlib.util.spec_from_file_location("workflow_bambu_package", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


PROFILE = MODULE.BambuProfile(
    printer_model="Bambu Lab A1 mini",
    nozzle_mm=0.4,
    print_settings_id="0.20mm Standard @BBL A1M",
    build_volume_mm=(180.0, 180.0),
)


def make_package(
    path: Path,
    *,
    gcode: bytes | None = None,
    md5: str | None = None,
    plate_members: tuple[int, ...] = (1,),
    object_names: tuple[str, ...] = ("piece-01",),
    bbox_all: list[float] | None = None,
    nozzle: float = 0.4,
    printer: str = "Bambu Lab A1 mini",
    print_settings: str = "0.20mm Standard @BBL A1M",
):
    gcode = gcode or (
        f"; printer_model = {printer}\n"
        f"; nozzle_diameter = {nozzle}\n"
        f"; print_settings_id = {print_settings}\n"
        "G1 X10 Y10 Z0.2\n"
    ).encode()
    digest = md5 or hashlib.md5(gcode).hexdigest().upper()
    metadata = {
        "nozzle_diameter": nozzle,
        "bbox_all": bbox_all or [10.0, 10.0, 20.0, 20.0],
        "bbox_objects": [{"name": name} for name in object_names],
    }
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for plate in plate_members:
            name = f"Metadata/plate_{plate}.gcode"
            archive.writestr(name, gcode)
            archive.writestr(f"Metadata/plate_{plate}.gcode.md5", digest)
        archive.writestr("Metadata/plate_1.json", json.dumps(metadata))


def test_single_plate_bambu_package_is_verified(tmp_path):
    package = tmp_path / "plate-01.gcode.3mf"
    make_package(package)

    result = MODULE.verify_single_plate_package(
        package,
        expected_piece_ids=("piece-01",),
        profile=PROFILE,
    )

    assert result.valid is True
    assert result.gcode_member == "Metadata/plate_1.gcode"
    assert result.gcode_md5 == result.gcode_md5.upper()
    assert result.object_names == ("piece-01",)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"plate_members": (1, 2)}, "exactly one"),
        ({"md5": "0" * 32}, "MD5"),
        ({"printer": "Bambu Lab P1S"}, "printer_model"),
        ({"nozzle": 0.2}, "nozzle"),
        ({"print_settings": "0.12mm Fine"}, "print_settings_id"),
        ({"bbox_all": [170, 170, 190, 190]}, "build volume"),
        ({"object_names": ("piece-02",)}, "object mapping"),
    ],
)
def test_bambu_package_rejects_invalid_contract(tmp_path, kwargs, message):
    package = tmp_path / "invalid.gcode.3mf"
    make_package(package, **kwargs)

    with pytest.raises(MODULE.BambuPackageError, match=message):
        MODULE.verify_single_plate_package(
            package,
            expected_piece_ids=("piece-01",),
            profile=PROFILE,
        )


def test_bambu_package_rejects_missing_members(tmp_path):
    package = tmp_path / "missing.gcode.3mf"
    with ZipFile(package, "w", ZIP_DEFLATED) as archive:
        archive.writestr("Metadata/plate_1.gcode", b"G1 X1")

    with pytest.raises(MODULE.BambuPackageError, match="md5"):
        MODULE.verify_single_plate_package(
            package,
            expected_piece_ids=("piece-01",),
            profile=PROFILE,
        )


def test_bambu_package_rejects_nonfinite_metadata(tmp_path):
    package = tmp_path / "nonfinite.gcode.3mf"
    make_package(package, bbox_all=[10.0, 10.0, float("nan"), 20.0])

    with pytest.raises(MODULE.BambuPackageError, match="finite"):
        MODULE.verify_single_plate_package(
            package,
            expected_piece_ids=("piece-01",),
            profile=PROFILE,
        )


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_bambu_package_rejects_duplicate_required_members(tmp_path):
    package = tmp_path / "duplicate.gcode.3mf"
    gcode = (
        "; printer_model = Bambu Lab A1 mini\n"
        "; nozzle_diameter = 0.4\n"
        "; print_settings_id = 0.20mm Standard @BBL A1M\n"
    ).encode()
    digest = hashlib.md5(gcode).hexdigest().upper()
    metadata = {
        "nozzle_diameter": 0.4,
        "bbox_all": [10.0, 10.0, 20.0, 20.0],
        "bbox_objects": [{"name": "piece-01"}],
    }
    with ZipFile(package, "w", ZIP_DEFLATED) as archive:
        archive.writestr("Metadata/plate_1.gcode", gcode)
        archive.writestr("Metadata/plate_1.gcode", gcode)
        archive.writestr("Metadata/plate_1.gcode.md5", digest)
        archive.writestr("Metadata/plate_1.json", json.dumps(metadata))

    with pytest.raises(MODULE.BambuPackageError, match="exactly one"):
        MODULE.verify_single_plate_package(
            package,
            expected_piece_ids=("piece-01",),
            profile=PROFILE,
        )
