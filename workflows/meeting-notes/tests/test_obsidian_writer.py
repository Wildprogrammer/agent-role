import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "obsidian_writer.py"
SPEC = importlib.util.spec_from_file_location("meeting_notes_obsidian_writer", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
VaultError = MODULE.VaultError
write_note = MODULE.write_note


def test_new_mode_creates_unique_note(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()

    first = write_note(vault, "Meetings/demo.md", "# First", mode="new")
    second = write_note(vault, "Meetings/demo.md", "# Second", mode="new")

    assert first.name == "demo.md"
    assert second.name == "demo-2.md"


def test_overwrite_requires_explicit_approval(tmp_path: Path):
    vault = tmp_path / "vault"
    target = vault / "Meetings" / "demo.md"
    target.parent.mkdir(parents=True)
    target.write_text("old", encoding="utf-8")

    with pytest.raises(VaultError, match="approval"):
        write_note(vault, "Meetings/demo.md", "new", mode="overwrite")


def test_path_escape_is_rejected(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()

    with pytest.raises(VaultError, match="outside vault"):
        write_note(vault, "../escape.md", "bad", mode="new")
