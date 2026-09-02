import importlib.util
import sys
import zipfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "three_mf_import.py"


def load_module():
    assert SCRIPT.is_file(), "streaming 3MF importer is required"
    spec = importlib.util.spec_from_file_location("workflow_three_mf_import", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_component_package(
    path: Path,
    *,
    unit="millimeter",
    component_object_id="1",
    include_build=True,
):
    component_transform = "1 0 0 0 1 0 0 0 1 1 2 3"
    build_transform = "2 0 0 0 2 0 0 0 2 10 20 30"
    build = (
        f'<build><item objectid="2" transform="{build_transform}"/></build>'
        if include_build
        else "<build/>"
    )
    model = f"""<?xml version="1.0" encoding="UTF-8"?>
    <model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
      <resources>
        <object id="1" type="model"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>
          <vertex x="0" y="1" z="0"/><vertex x="0" y="0" z="1"/>
        </vertices><triangles>
          <triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/>
          <triangle v1="0" v2="3" v3="2"/><triangle v1="1" v2="2" v3="3"/>
        </triangles></mesh></object>
        <object id="2" type="model"><components>
          <component objectid="{component_object_id}" transform="{component_transform}"/>
        </components></object>
      </resources>
      {build}
    </model>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Target="/3D/3dmodel.model" Id="rel-1"
        Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
    </Relationships>"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("3D/3dmodel.model", model)


def vertices(payload):
    values = tuple(payload.vertices)
    return tuple(tuple(values[index : index + 3]) for index in range(0, len(values), 3))


def test_streaming_import_resolves_component_and_build_transforms(tmp_path, monkeypatch):
    module = load_module()
    path = tmp_path / "component.3mf"
    write_component_package(path)
    original_read = zipfile.ZipFile.read

    def guarded_read(archive, name, *args, **kwargs):
        if str(name).replace("\\", "/").endswith("3D/3dmodel.model"):
            raise AssertionError("model XML must be streamed")
        return original_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", guarded_read)
    payload = module.load_3mf_mesh(path)

    assert payload.vertex_count == 4
    assert payload.triangle_count == 4
    assert vertices(payload)[0] == pytest.approx((12.0, 24.0, 36.0))
    assert vertices(payload)[1] == pytest.approx((14.0, 24.0, 36.0))
    assert tuple(payload.triangles[:3]) == (0, 2, 1)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"unit": "inch"}, "millimeter"),
        ({"component_object_id": "99"}, "unknown object"),
        ({"include_build": False}, "build item"),
    ],
)
def test_streaming_import_rejects_unsupported_or_incomplete_graph(
    tmp_path, options, message
):
    module = load_module()
    path = tmp_path / "broken.3mf"
    write_component_package(path, **options)

    with pytest.raises(module.ThreeMFImportError, match=message):
        module.load_3mf_mesh(path)
