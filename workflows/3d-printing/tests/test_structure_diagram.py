import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "structure_diagram.py"
SPEC = importlib.util.spec_from_file_location("workflow_structure_diagram", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


PIECE_IDS = (
    "body",
    "head",
    "arm-left",
    "arm-right",
    "leg-left",
    "leg-right",
)


def test_monthly_cat_exploded_offsets_are_directional():
    offsets = MODULE.exploded_offsets(PIECE_IDS, 40.0)

    assert offsets["body"] == (0.0, 0.0, 0.0)
    assert offsets["head"][2] > 0
    assert offsets["arm-left"][0] < 0 < offsets["arm-right"][0]
    assert offsets["leg-left"][2] < 0 and offsets["leg-right"][2] < 0


def test_two_piece_layout_is_supported_for_formal_head_body_delivery():
    offsets = MODULE.exploded_offsets(("body", "head"), 40.0)

    assert offsets["body"] == (0.0, 0.0, 0.0)
    assert offsets["head"][2] > 0


def test_generic_piece_ids_receive_deterministic_labels_and_offsets():
    piece_ids = ("torso", "tail-fin")

    assert MODULE.exploded_offsets(piece_ids, 40.0) == MODULE.exploded_offsets(
        piece_ids, 40.0
    )
    assert MODULE.piece_label("tail-fin") == "tail-fin\npiece-tail-fin.stl"


def test_cjk_font_prefers_explicit_environment_path(tmp_path, monkeypatch):
    font = tmp_path / "font.ttc"
    font.write_bytes(b"font")
    monkeypatch.setenv("AGENT_ROLE_CJK_FONT", str(font))

    assert MODULE.find_cjk_font() == font


def test_piece_labels_include_chinese_name_and_stl_filename():
    assert MODULE.piece_label("arm-left") == "左臂\npiece-arm-left.stl"
    assert MODULE.piece_label("body") == "身体（含尾巴）\npiece-body.stl"
