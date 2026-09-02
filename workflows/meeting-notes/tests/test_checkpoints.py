import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "checkpoints.py"
SPEC = importlib.util.spec_from_file_location("meeting_notes_checkpoints", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
CheckpointStore = MODULE.CheckpointStore
Segment = MODULE.Segment


def test_resume_returns_only_missing_or_failed_segments(tmp_path: Path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    segments = [
        Segment("000000-000030", 0, 30_000),
        Segment("000030-000060", 30_000, 60_000),
        Segment("000060-000090", 60_000, 90_000),
    ]
    store.initialize(source_sha256="abc", segments=segments)
    store.complete("000000-000030", text="hello", output_sha256="one")
    store.fail("000030-000060", error="decoder failed")

    pending = store.pending()

    assert [segment.id for segment in pending] == [
        "000030-000060",
        "000060-000090",
    ]


def test_source_change_invalidates_checkpoint(tmp_path: Path):
    store = CheckpointStore(tmp_path / "checkpoint.json")
    store.initialize("abc", [Segment("a", 0, 10)])

    try:
        store.initialize("different", [Segment("a", 0, 10)])
    except ValueError as exc:
        assert "source fingerprint changed" in str(exc)
    else:
        raise AssertionError("expected source change rejection")
