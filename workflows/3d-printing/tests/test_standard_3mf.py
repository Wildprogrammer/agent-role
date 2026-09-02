import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "standard_3mf.py"
SPEC = importlib.util.spec_from_file_location("workflow_standard_3mf", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

IDENTITY = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)


def write_package(
    path: Path,
    *,
    names=("body", "head"),
    transforms=None,
    include_gcode=False,
    empty_mesh=False,
):
    transforms = transforms or {name: IDENTITY for name in names}
    objects = []
    build_items = []
    for index, name in enumerate(names, start=1):
        mesh = (
            "<mesh><vertices></vertices><triangles></triangles></mesh>"
            if empty_mesh and index == 1
            else """<mesh><vertices>
              <vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>
              <vertex x="0" y="1" z="0"/><vertex x="0" y="0" z="1"/>
            </vertices><triangles>
              <triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/>
              <triangle v1="0" v2="3" v3="2"/><triangle v1="1" v2="2" v3="3"/>
            </triangles></mesh>"""
        )
        objects.append(f'<object id="{index}" name="{name}" type="model">{mesh}</object>')
        transform = " ".join(str(value) for value in transforms[name])
        build_items.append(f'<item objectid="{index}" transform="{transform}"/>')
    model = f"""<?xml version="1.0" encoding="UTF-8"?>
    <model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
      <resources>{''.join(objects)}</resources>
      <build>{''.join(build_items)}</build>
    </model>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Target="/3D/3dmodel.model" Id="rel-1"
        Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
    </Relationships>"""
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
    <Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
      <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
      <Override PartName="/3D/3dmodel.model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
    </Types>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("3D/3dmodel.model", model)
        if include_gcode:
            archive.writestr("Metadata/plate_1.gcode", "G1 X1 Y1")


def test_standard_3mf_accepts_exact_named_objects_and_transforms(tmp_path):
    path = tmp_path / "assembly.3mf"
    write_package(path)

    result = MODULE.verify_standard_3mf(
        path,
        expected_piece_ids=("body", "head"),
        expected_transforms={"body": IDENTITY, "head": IDENTITY},
    )

    assert result.valid is True
    assert result.object_names == ("body", "head")
    assert result.gcode_members == ()
    assert result.transforms["body"] == IDENTITY
    assert result.sha256 == MODULE.sha256_file(path)


def test_standard_3mf_writer_round_trips_multiple_meshes_without_gcode(tmp_path):
    path = tmp_path / "written.3mf"
    tetrahedron = MODULE.MeshPayload(
        vertices=((0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1)),
        triangles=((0, 2, 1), (0, 1, 3), (0, 3, 2), (1, 2, 3)),
        transform=IDENTITY,
    )

    MODULE.write_standard_3mf(
        path,
        {"body": tetrahedron, "head": tetrahedron},
    )
    result = MODULE.verify_standard_3mf(
        path,
        expected_piece_ids=("body", "head"),
        expected_transforms={"body": IDENTITY, "head": IDENTITY},
    )

    assert result.valid is True
    assert result.gcode_members == ()
    with zipfile.ZipFile(path) as archive:
        assert "3D/3dmodel.model" in archive.namelist()


def test_standard_3mf_verifier_streams_the_model_member(tmp_path, monkeypatch):
    path = tmp_path / "streamed.3mf"
    write_package(path)
    original_read = zipfile.ZipFile.read

    def guarded_read(archive, name, *args, **kwargs):
        if str(name).replace("\\", "/").endswith("3D/3dmodel.model"):
            raise AssertionError("model XML must not be loaded as one byte string")
        return original_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", guarded_read)

    result = MODULE.verify_standard_3mf(
        path,
        expected_piece_ids=("body", "head"),
        expected_transforms={"body": IDENTITY, "head": IDENTITY},
    )

    assert result.valid is True


@pytest.mark.parametrize(
    ("package_options", "expected_names", "message"),
    [
        ({"names": ("body",)}, ("body", "head"), "object names"),
        ({"names": ("body", "wrong")}, ("body", "head"), "object names"),
        ({"include_gcode": True}, ("body", "head"), "G-code"),
        ({"empty_mesh": True}, ("body", "head"), "empty mesh"),
    ],
)
def test_standard_3mf_rejects_wrong_structure(
    tmp_path, package_options, expected_names, message
):
    path = tmp_path / "broken.3mf"
    write_package(path, **package_options)

    with pytest.raises(MODULE.Standard3MFError, match=message):
        MODULE.verify_standard_3mf(
            path,
            expected_piece_ids=expected_names,
            expected_transforms={name: IDENTITY for name in expected_names},
        )


def test_standard_3mf_rejects_transform_mismatch(tmp_path):
    path = tmp_path / "moved.3mf"
    moved = (*IDENTITY[:9], 2.0, 0.0, 0.0)
    write_package(path, transforms={"body": IDENTITY, "head": moved})

    with pytest.raises(MODULE.Standard3MFError, match="transform"):
        MODULE.verify_standard_3mf(
            path,
            expected_piece_ids=("body", "head"),
            expected_transforms={"body": IDENTITY, "head": IDENTITY},
        )


def test_standard_3mf_rejects_hash_mismatch(tmp_path):
    path = tmp_path / "assembly.3mf"
    write_package(path)

    with pytest.raises(MODULE.Standard3MFError, match="hash"):
        MODULE.verify_standard_3mf(
            path,
            expected_piece_ids=("body", "head"),
            expected_transforms={"body": IDENTITY, "head": IDENTITY},
            expected_sha256="0" * 64,
        )


def test_standard_3mf_rejects_corrupt_zip(tmp_path):
    path = tmp_path / "corrupt.3mf"
    path.write_bytes(b"not a zip")

    with pytest.raises(MODULE.Standard3MFError, match="cannot read"):
        MODULE.verify_standard_3mf(
            path,
            expected_piece_ids=("body", "head"),
            expected_transforms={"body": IDENTITY, "head": IDENTITY},
        )
