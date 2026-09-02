import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "knowledge.py"
SPEC = importlib.util.spec_from_file_location("daily_knowledge", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_same_experience_key_replaces_the_old_value(tmp_path: Path):
    store = MODULE.KnowledgeStore(tmp_path / "knowledge.md")
    store.upsert(kind="classification", key="策略验证", value="module=验证平台")
    store.upsert(kind="classification", key="策略验证", value="module=策略平台")

    text = (tmp_path / "knowledge.md").read_text(encoding="utf-8")

    assert text.count("策略验证") == 1
    assert "module=策略平台" in text
    assert "module=验证平台" not in text


def test_knowledge_rejects_daily_logs_and_raw_input(tmp_path: Path):
    store = MODULE.KnowledgeStore(tmp_path / "knowledge.md")

    with pytest.raises(ValueError, match="kind"):
        store.upsert(kind="daily_log", key="2026-07-13", value="完成任务")
    with pytest.raises(ValueError, match="raw"):
        store.upsert(kind="priority", key="raw_input", value="今日任务：策略验证")


def test_knowledge_keeps_only_the_newest_bounded_entries(tmp_path: Path):
    store = MODULE.KnowledgeStore(tmp_path / "knowledge.md")
    for index in range(MODULE.MAX_ENTRIES + 1):
        store.upsert(
            kind="duration",
            key=f"任务-{index}",
            value=f"estimate={index}",
        )

    entries = store.entries()

    assert len(entries) == MODULE.MAX_ENTRIES
    assert entries[0][1] == "任务-1"
    assert entries[-1][1] == f"任务-{MODULE.MAX_ENTRIES}"
