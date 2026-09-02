import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "palette.py"
SPEC = importlib.util.spec_from_file_location("bead_pattern_palette", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


PALETTE_PATH = Path(__file__).parents[1] / "data" / "a-m-v1.json"
EXPECTED_SIZES = (24, 48, 72, 96, 120, 144, 221)


def test_a_m_v1_has_exact_fixed_nested_presets():
    palette = MODULE.load_palette(PALETTE_PATH)

    assert palette.palette_id == "a-m-v1"
    assert tuple(palette.presets) == EXPECTED_SIZES
    assert len(palette.colours) == 221
    assert palette.colours["A4"].rgb == (251, 237, 86)
    assert all(len(palette.presets[size]) == size for size in EXPECTED_SIZES)
    assert all(
        set(palette.presets[smaller]) < set(palette.presets[larger])
        for smaller, larger in zip(EXPECTED_SIZES, EXPECTED_SIZES[1:])
    )


def test_nearest_colour_never_uses_a_code_outside_selected_preset():
    palette = MODULE.load_palette(PALETTE_PATH)

    colour_144 = MODULE.nearest_colour(palette, (255, 255, 159), preset_size=144)
    colour_221 = MODULE.nearest_colour(palette, (255, 255, 159), preset_size=221)

    assert colour_144.code in palette.presets[144]
    assert colour_144.code != "A16"
    assert colour_221.code == "A16"
