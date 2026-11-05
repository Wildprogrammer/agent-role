from __future__ import annotations

from pathlib import Path
import os

import pytest

from agent_workflow_hub.knowledge_support_agent.documents import KnowledgeChunk
from agent_workflow_hub.knowledge_support_agent.store import (
    InMemoryTableBackend,
    KnowledgeStore,
    StoreDependencyUnavailable,
    StorePathUnsupported,
)


def chunk(identifier: str, content: str, path: str) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=identifier,
        source_id="repo",
        source_kind="git",
        source_version="a" * 40,
        source_path=path,
        title=Path(path).name,
        section="登录",
        content=content,
        content_sha256=identifier.rjust(64, "0"),
        provenance={"commit_sha": "a" * 40, "source_path": path},
        inferred=False,
    )


def store(tmp_path: Path) -> KnowledgeStore:
    return KnowledgeStore(
        tmp_path / "agent-a" / "knowledge-support" / "lancedb",
        backend=InMemoryTableBackend(),
    )


class FailingStateBackend(InMemoryTableBackend):
    def __init__(self) -> None:
        super().__init__()
        self.fail_source_state = False

    def replace(self, name, rows) -> None:
        if name == "source_state" and self.fail_source_state:
            raise RuntimeError("source state write failed")
        super().replace(name, rows)


class RejectFullKnowledgeReadBackend(InMemoryTableBackend):
    def __init__(self) -> None:
        super().__init__()
        self.reject_full_knowledge_read = False

    def read(self, name):
        if name == "knowledge" and self.reject_full_knowledge_read:
            raise AssertionError("active generation must be filtered by the backend")
        return super().read(name)


def test_active_generation_does_not_read_inactive_rows_into_memory(
    tmp_path: Path,
) -> None:
    backend = RejectFullKnowledgeReadBackend()
    target = KnowledgeStore(
        tmp_path / "agent-a" / "knowledge-support" / "lancedb",
        backend=backend,
    )
    target.replace_knowledge(
        [chunk("active", "当前登录说明", "docs/login.md")],
        vectors={},
        generation="g1",
    )
    backend.append(
        "knowledge",
        [
            {
                **backend.read("knowledge")[0],
                "chunk_id": "inactive",
                "generation": "g0",
            }
        ],
    )
    target.replace_source_state(
        [
            {
                "source_id": "repo",
                "version": "a" * 40,
                "manifest_json": "{}",
                "generation": "g1",
            }
        ]
    )
    backend.reject_full_knowledge_read = True

    assert [row["chunk_id"] for row in target.knowledge_rows()] == ["active"]


def test_successful_publish_reclaims_inactive_generations(tmp_path: Path) -> None:
    backend = InMemoryTableBackend()
    target = KnowledgeStore(
        tmp_path / "agent-a" / "knowledge-support" / "lancedb",
        backend=backend,
    )
    target.publish_generation(
        [chunk("old", "旧登录说明", "docs/login.md")],
        vectors={},
        generation="g1",
        source_state=[
            {"source_id": "repo", "version": "a" * 40, "manifest_json": "{}"}
        ],
    )

    target.publish_generation(
        [chunk("new", "新登录说明", "docs/new-login.md")],
        vectors={},
        generation="g2",
        source_state=[
            {"source_id": "repo", "version": "b" * 40, "manifest_json": "{}"}
        ],
    )

    rows = backend.read("knowledge")
    assert [row["chunk_id"] for row in rows] == ["new"]
    assert {row["generation"] for row in rows} == {"g2"}


def test_generation_retention_preserves_confirmed_experience(tmp_path: Path) -> None:
    backend = InMemoryTableBackend()
    target = KnowledgeStore(tmp_path / "database", backend=backend)
    target.replace_knowledge(
        [chunk("experience", "用户确认的说明", "experience/login")],
        vectors={},
        generation="feedback",
    )
    feedback = backend.read("knowledge")[0]
    feedback["source_kind"] = "user-confirmed-experience"
    backend.replace("knowledge", [feedback])

    for generation in ("g1", "g2", "g2"):
        target.publish_generation(
            [chunk("current", "登录说明", "docs/login.md")],
            vectors={},
            generation=generation,
            source_state=[{"source_id": "repo", "version": "a" * 40}],
        )

    assert len(backend.read("knowledge")) == 2
    assert {row["chunk_id"] for row in target.knowledge_rows()} == {
        "current", "experience"
    }


def test_real_lance_filter_and_retention(tmp_path: Path) -> None:
    pytest.importorskip("lancedb")
    target = KnowledgeStore(tmp_path / "database")
    state = [{"source_id": "repo", "version": "a" * 40}]
    old_chunks = [chunk(f"old-{i}", "旧说明", f"docs/{i}.md") for i in range(25)]
    target.publish_generation(old_chunks, vectors={}, generation="g1", source_state=state)
    current = [chunk(f"new-{i}", "新说明", f"docs/{i}.md") for i in range(25)]
    target.publish_generation(
        current, vectors={}, generation="g2'quoted", source_state=state
    )

    # More than the query builder's default limit; quoted values stay literals.
    assert len(target.knowledge_rows()) == 25
    assert len(target._backend.read("knowledge")) == 25
    assert {row["generation"] for row in target.knowledge_rows()} == {"g2'quoted"}


def test_store_keeps_one_database_path_and_two_core_tables(tmp_path: Path) -> None:
    target = store(tmp_path)
    target.replace_knowledge(
        [chunk("1", "登录失败时检查账号密码。", "docs/login.md")],
        vectors={"1": [1.0, 0.0]},
        generation="g1",
    )
    target.replace_source_state([{"source_id": "repo", "version": "a" * 40}])

    assert target.database_dir == (
        tmp_path / "agent-a" / "knowledge-support" / "lancedb"
    ).resolve()
    assert set(target.table_names()) == {"knowledge", "source_state"}


def test_legacy_state_filters_out_failed_unactivated_upgrade(tmp_path: Path) -> None:
    backend = FailingStateBackend()
    target = KnowledgeStore(
        tmp_path / "agent-a" / "knowledge-support" / "lancedb",
        backend=backend,
    )
    old = chunk("old", "旧登录说明", "docs/login.md")
    target.replace_knowledge([old], vectors={}, generation="legacy")
    target.replace_source_state(
        [{"source_id": "repo", "version": "a" * 40, "manifest_json": "{}"}]
    )
    newer = KnowledgeChunk(
        **{
            **old.__dict__,
            "chunk_id": "new",
            "source_version": "b" * 40,
            "content": "新登录说明",
        }
    )
    backend.fail_source_state = True

    with pytest.raises(RuntimeError, match="source state write failed"):
        target.publish_generation(
            [newer],
            vectors={},
            generation="new-generation",
            source_state=[
                {"source_id": "repo", "version": "b" * 40, "manifest_json": "{}"}
            ],
        )

    assert [row["chunk_id"] for row in target.knowledge_rows()] == ["old"]


def test_chinese_full_text_and_vector_results_are_rrf_merged(tmp_path: Path) -> None:
    target = store(tmp_path)
    target.replace_knowledge(
        [
            chunk("login", "登录失败时检查账号密码。", "docs/login.md"),
            chunk("reset", "重置密码后重新认证。", "docs/reset.md"),
            chunk("other", "商品支持七天退货。", "docs/product.md"),
        ],
        vectors={
            "login": [1.0, 0.0],
            "reset": [0.9, 0.1],
            "other": [0.0, 1.0],
        },
        generation="g1",
    )

    result = target.search("登录失败", query_vector=[1.0, 0.0], limit=3)

    assert result.mode == "hybrid"
    assert result.evidence[0]["chunk_id"] == "login"
    assert len({item["chunk_id"] for item in result.evidence}) == len(result.evidence)
    assert result.evidence[0]["provenance"]["commit_sha"] == "a" * 40
    assert "scores" in result.evidence[0]


def test_embedding_failure_falls_back_to_fts_with_results(tmp_path: Path) -> None:
    target = store(tmp_path)
    target.replace_knowledge(
        [chunk("login", "登录失败时检查账号密码。", "docs/login.md")],
        vectors={},
        generation="g1",
    )

    result = target.search(
        "登录失败",
        query_vector=None,
        embedding_error="ollama unavailable",
    )

    assert result.mode == "fts_degraded"
    assert result.degraded_reason == "ollama unavailable"
    assert result.evidence[0]["chunk_id"] == "login"


def test_each_retrieval_channel_contributes_at_most_twenty_candidates(
    tmp_path: Path,
) -> None:
    target = store(tmp_path)
    chunks = [
        chunk(f"item-{index:02d}", "登录帮助", f"docs/{index:02d}.md")
        for index in range(30)
    ]
    target.replace_knowledge(
        chunks,
        vectors={value.chunk_id: [1.0] for value in chunks},
        generation="g1",
    )

    result = target.search("登录", query_vector=[1.0], limit=30)

    assert len(result.evidence) == 20


def test_missing_lancedb_dependency_is_explicit(tmp_path: Path, monkeypatch) -> None:
    import agent_workflow_hub.knowledge_support_agent.store as module

    monkeypatch.setattr(module, "_import_lancedb", lambda: None)
    try:
        KnowledgeStore(tmp_path / "lancedb")
    except StoreDependencyUnavailable as exc:
        assert "lancedb" in str(exc)
    else:
        raise AssertionError("missing LanceDB must not silently use another database")


@pytest.mark.skipif(os.name != "nt", reason="Windows MAX_PATH coverage")
def test_windows_store_rejects_too_deep_database_before_backend_write(
    tmp_path: Path,
) -> None:
    target = tmp_path / ("deep" * 50) / "lancedb"

    with pytest.raises(StorePathUnsupported, match="170"):
        KnowledgeStore(target.resolve(), backend=InMemoryTableBackend())
