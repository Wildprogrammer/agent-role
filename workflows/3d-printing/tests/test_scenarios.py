import json
from pathlib import Path


def test_fixture_expectations_are_explicit():
    root = Path("workflows/3d-printing/tests/fixtures")
    valid = json.loads((root / "valid-cube.json").read_text(encoding="utf-8"))
    broken = json.loads(
        (root / "non-manifold.json").read_text(encoding="utf-8")
    )

    assert valid["expected"]["printable"] is True
    assert broken["expected"]["printable"] is False
    assert "non_manifold_edges" in broken["expected"]["failures"]


def test_keyed_connector_reference_keeps_schema_and_assembly_semantics_explicit():
    body = Path(
        "workflows/3d-printing/references/split-and-slice-bambu.md"
    ).read_text(encoding="utf-8")

    assert "integrated-keyed-pin" in body
    assert "center_mm" in body
    assert "key_direction" in body
    assert "clearance_per_side_mm" in body
    assert "minimum_wall_mm" in body
    assert "minimum_edge_margin_mm" in body
    assert "assembly 是逻辑装配 plate" in body
    assert "不是打印分盘" in body
    assert "标准多对象 3MF" in body
    assert "无 G-code" in body


def test_keyed_connector_reference_defines_two_stage_inspection_and_stop_states():
    body = Path(
        "workflows/3d-printing/references/split-and-slice-bambu.md"
    ).read_text(encoding="utf-8")

    assert "基础六视图" in body
    assert "候选计划复测" in body
    assert "needs_geometry_redesign" in body
    assert "needs_geometry_repair" in body
    assert "GUI 可辅助用户批准的预览、定位或诊断" in body
    assert "界面状态单独宣布成功" in body


def test_keyed_connector_reference_defines_component_safe_diagram_delivery():
    body = Path(
        "workflows/3d-printing/references/split-and-slice-bambu.md"
    ).read_text(encoding="utf-8")

    assert "双向 BMesh" in body
    assert "returned_components" in body
    assert "局部有界 cutter" not in body
    assert "needs_user_component_assignment" in body
    assert "每个最终 STL 必须恰好一个连通体" in body
    assert "structure_diagram_filename" in body
    assert "STL + 结构图" in body
    assert "identity transform" in body
    assert "三种交付格式互斥" in body


def test_split_plan_requests_stl_diagram_without_multi_object_3mf():
    plan = json.loads(
        Path(
            "workflows/3d-printing/tests/fixtures/stl-diagram-split-plan.json"
        ).read_text(encoding="utf-8")
    )

    assert "assembly_filename" not in plan
    assert plan["structure_diagram_filename"] == "structure-diagram.png"
