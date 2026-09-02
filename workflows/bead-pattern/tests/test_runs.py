import importlib.util
import sys
from pathlib import Path

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
RUNS = load_module("runs")
PALETTE_PATH = Path(__file__).parents[1] / "data" / "a-m-v1.json"


def test_accept_freezes_matrix_even_when_source_changes(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGBA", (2, 2), (251, 237, 86, 255)).save(source)
    candidate = PIPELINE.build_candidate(
        source,
        PALETTE.load_palette(PALETTE_PATH),
        preset_size=24,
        columns=2,
        rows=2,
        board=None,
    )

    candidate_path = RUNS.create_candidate(tmp_path, "demo-1", candidate)
    accepted = RUNS.accept_candidate(tmp_path, "demo-1")
    Image.new("RGBA", (2, 2), (0, 0, 0, 255)).save(source)

    assert candidate_path.is_file()
    assert accepted.matrix == candidate.matrix
    assert RUNS.load_accepted(tmp_path, "demo-1").matrix == candidate.matrix


def test_run_id_is_unique_and_rejects_path_traversal(tmp_path):
    palette = PALETTE.load_palette(PALETTE_PATH)
    candidate = PIPELINE.build_candidate(
        _source(tmp_path), palette, preset_size=24, columns=2, rows=2, board=None
    )

    RUNS.create_candidate(tmp_path, "demo-1", candidate)

    try:
        RUNS.create_candidate(tmp_path, "demo-1", candidate)
    except FileExistsError:
        pass
    else:
        raise AssertionError("duplicate run id must not overwrite a candidate")

    try:
        RUNS.create_candidate(tmp_path, "../outside", candidate)
    except ValueError as error:
        assert "run-id" in str(error)
    else:
        raise AssertionError("path traversal run id must be rejected")


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "run-source.png"
    Image.new("RGBA", (2, 2), (251, 237, 86, 255)).save(source)
    return source
