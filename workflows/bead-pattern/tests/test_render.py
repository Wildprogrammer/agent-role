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
RENDER = load_module("render")
PALETTE_PATH = Path(__file__).parents[1] / "data" / "a-m-v1.json"


def accepted_pattern_with_matrix(
    matrix: tuple[tuple[str | None, ...], ...], *, preset_size: int = 24
):
    palette = PALETTE.load_palette(PALETTE_PATH)
    counts = {}
    for row in matrix:
        for code in row:
            if code is not None:
                counts[code] = counts.get(code, 0) + 1
    return PIPELINE.Candidate(
        palette_id=palette.palette_id,
        palette_digest=palette.digest,
        preset_size=preset_size,
        columns=len(matrix[0]),
        rows=len(matrix),
        board=None,
        background_code=None,
        source_sha256="a" * 64,
        source_size=(len(matrix[0]), len(matrix)),
        matrix=matrix,
        counts=counts,
        empty_cells=sum(code is None for row in matrix for code in row),
    )


def test_rendered_pattern_has_one_png_and_conserved_counts(tmp_path):
    palette = PALETTE.load_palette(PALETTE_PATH)
    accepted = accepted_pattern_with_matrix((("A4", None), ("H7", "A4")))

    output = RENDER.render_final_png(
        accepted, palette=palette, hub_root=tmp_path, run_id="demo-1"
    )

    assert output.name == "pattern.png"
    assert output.parent == tmp_path / "workflows" / "bead-pattern" / "outputs" / "demo-1"
    assert output.is_file()
    assert accepted.counts == {"A4": 2, "H7": 1}
    with Image.open(output) as image:
        assert image.format == "PNG"
        assert image.width > 100
        assert image.height > 100


def test_render_smoke_supports_a_104_by_104_grid(tmp_path):
    palette = PALETTE.load_palette(PALETTE_PATH)
    matrix = tuple(tuple("A4" for _ in range(104)) for _ in range(104))
    accepted = accepted_pattern_with_matrix(matrix)

    output = RENDER.render_final_png(
        accepted, palette=palette, hub_root=tmp_path, run_id="grid-104"
    )

    with Image.open(output) as image:
        assert image.width >= 104 * RENDER.MIN_CELL_PIXELS
        assert image.height >= 104 * RENDER.MIN_CELL_PIXELS


def test_render_supports_a_104_by_104_grid_using_the_full_144_colour_preset(tmp_path):
    palette = PALETTE.load_palette(PALETTE_PATH)
    codes = palette.presets[144]
    matrix = tuple(
        tuple(codes[(row * 104 + column) % len(codes)] for column in range(104))
        for row in range(104)
    )
    accepted = accepted_pattern_with_matrix(matrix, preset_size=144)

    output = RENDER.render_final_png(
        accepted, palette=palette, hub_root=tmp_path, run_id="grid-104-full-palette"
    )

    assert output.is_file()


def test_render_refuses_an_unreadable_or_oversized_canvas(tmp_path):
    palette = PALETTE.load_palette(PALETTE_PATH)
    matrix = tuple(tuple("A4" for _ in range(200)) for _ in range(200))
    accepted = accepted_pattern_with_matrix(matrix)

    with pytest.raises(ValueError, match="readability|canvas"):
        RENDER.render_final_png(
            accepted, palette=palette, hub_root=tmp_path, run_id="oversized-1"
        )
