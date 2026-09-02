import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image


SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_module(name: str):
    script = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"bead_pattern_{name}", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PALETTE = load_module("palette")
PIPELINE = load_module("pipeline")
PALETTE_PATH = Path(__file__).parents[1] / "data" / "a-m-v1.json"


def source_png(tmp_path: Path, size: tuple[int, int], colour=(251, 237, 86)) -> Path:
    path = tmp_path / "source.png"
    Image.new("RGBA", size, (*colour, 255)).save(path)
    return path


def test_contain_centres_source_and_marks_letterbox_as_empty(tmp_path):
    candidate = PIPELINE.build_candidate(
        source_png(tmp_path, size=(4, 2)),
        PALETTE.load_palette(PALETTE_PATH),
        preset_size=24,
        columns=4,
        rows=4,
        board=None,
    )

    assert candidate.matrix[0] == (None, None, None, None)
    assert candidate.matrix[1] == ("A4", "A4", "A4", "A4")
    assert candidate.matrix[2] == ("A4", "A4", "A4", "A4")
    assert candidate.matrix[3] == (None, None, None, None)
    assert candidate.empty_cells == 8
    assert candidate.counts == {"A4": 8}


def test_background_code_replaces_empty_cells(tmp_path):
    candidate = PIPELINE.build_candidate(
        source_png(tmp_path, size=(4, 2)),
        PALETTE.load_palette(PALETTE_PATH),
        preset_size=24,
        columns=4,
        rows=4,
        board=None,
        background_code="H7",
    )

    assert candidate.matrix[0] == ("H7", "H7", "H7", "H7")
    assert candidate.matrix[3] == ("H7", "H7", "H7", "H7")
    assert candidate.empty_cells == 0
    assert candidate.counts == {"A4": 8, "H7": 8}


def test_standard_board_rejects_an_oversized_grid(tmp_path):
    with pytest.raises(ValueError, match="board 52"):
        PIPELINE.build_candidate(
            source_png(tmp_path, size=(4, 2)),
            PALETTE.load_palette(PALETTE_PATH),
            preset_size=24,
            columns=53,
            rows=52,
            board="52",
        )
