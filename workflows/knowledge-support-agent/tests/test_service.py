from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from agent_workflow_hub.knowledge_support_agent.contracts import (
    AgentConfig,
    EmbeddingConfig,
    KnowledgeSource,
    SupplementalSkill,
)
from agent_workflow_hub.knowledge_support_agent.embeddings import EmbeddingUnavailable
from agent_workflow_hub.knowledge_support_agent.service import (
    GitTreeEntry,
    KnowledgeSupportService,
)
from agent_workflow_hub.knowledge_support_agent.store import (
    InMemoryTableBackend,
    KnowledgeStore,
)


class FakeGitReader:
    def __init__(self) -> None:
        self.sha = "a" * 40
        self.entries = (
            GitTreeEntry(mode="100644", object_type="blob", object_id="1" * 40, path="README.md"),
            GitTreeEntry(mode="100644", object_type="blob", object_id="2" * 40, path="src/login.py"),
            GitTreeEntry(mode="100644", object_type="blob", object_id="3" * 40, path=".env"),
        )
        self.blobs = {
            "README.md": b"# Login\n\nLogin failures are documented here.",
            "src/login.py": b"def authenticate(user):\n    pass\n",
            ".env": b"PASSWORD=secret",
        }
        self.show_calls: list[tuple[str, str]] = []
        self.fail = False

    def head_sha(self, repository: Path) -> str:
        if self.fail:
            raise RuntimeError("git unavailable")
        return self.sha

    def list_tree(self, repository: Path, sha: str):
        if self.fail:
            raise RuntimeError("git unavailable")
        return self.entries

    def show_file(self, repository: Path, sha: str, path: str) -> bytes:
        if self.fail:
            raise RuntimeError("git unavailable")
        self.show_calls.append((sha, path))
        return self.blobs[path]


class FakeEmbedder:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable

    def embed(self, texts):
        if self.unavailable:
            raise EmbeddingUnavailable("ollama unavailable")
        return tuple((1.0, float(index + 1)) for index, _ in enumerate(texts))


class FailingStateBackend(InMemoryTableBackend):
    def __init__(self) -> None:
        super().__init__()
        self.fail_source_state = False

    def replace(self, name, rows) -> None:
        if name == "source_state" and self.fail_source_state:
            raise RuntimeError("source state write failed")
        super().replace(name, rows)


def config(tmp_path: Path, *, collected=False) -> AgentConfig:
    source = (
        KnowledgeSource(
            source_id="wiki",
            source_type="collected-document",
            path=(tmp_path / "missing-wiki.txt").resolve(),
            origin_url="https://example.invalid/wiki",
        )
        if collected
        else KnowledgeSource(
            source_id="repo",
            source_type="git",
            repository=(tmp_path / "repo").resolve(),
            include_code=True,
        )
    )
    return AgentConfig(
        schema_version="1.0",
        agent_id="support",
        display_name="Support",
        purpose="Answer questions",
        audiences=("internal",),
        workdir=(tmp_path / "runtime").resolve(),
        sources=(source,),
        supplemental_skills=(
            SupplementalSkill(name="jenkins-operations", purpose="查询流水线"),
        ),
        embedding=EmbeddingConfig(
            provider="ollama",
            base_url="http://127.0.0.1:11434",
            model="qwen3-embedding:0.6b",
            fallback="fts",
        ),
    )


def service(
    tmp_path: Path,
    reader: FakeGitReader,
    *,
    unavailable=False,
    collected=False,
    backend=None,
):
    cfg = config(tmp_path, collected=collected)
    target = KnowledgeStore(
        (cfg.workdir / "knowledge-support" / "lancedb").resolve(),
        backend=backend or InMemoryTableBackend(),
    )
    return KnowledgeSupportService(
        cfg,
        store=target,
        embedder=FakeEmbedder(unavailable=unavailable),
        git_reader=reader,
    ), target


def test_git_build_reads_only_exact_commit_blobs_and_skips_secrets(tmp_path: Path) -> None:
    reader = FakeGitReader()
    subject, target = service(tmp_path, reader)

    result = subject.build()

    assert result.status == "built"
    assert reader.show_calls == [
        ("a" * 40, "README.md"),
        ("a" * 40, "src/login.py"),
    ]
    assert all(row["source_path"] != ".env" for row in target.knowledge_rows())
    assert result.source_versions == {"repo": "a" * 40}


def test_unchanged_build_does_not_read_blobs_again(tmp_path: Path) -> None:
    reader = FakeGitReader()
    subject, _ = service(tmp_path, reader)
    assert subject.build().status == "built"
    calls = list(reader.show_calls)

    result = subject.refresh()

    assert result.status == "unchanged"
    assert reader.show_calls == calls


def test_new_head_only_reads_changed_blob(tmp_path: Path) -> None:
    reader = FakeGitReader()
    subject, _ = service(tmp_path, reader)
    subject.build()
    reader.show_calls.clear()
    reader.sha = "b" * 40
    reader.entries = (
        replace(reader.entries[0]),
        GitTreeEntry(mode="100644", object_type="blob", object_id="4" * 40, path="src/login.py"),
    )
    reader.blobs["src/login.py"] = b'def authenticate(user):\n    """Check login."""\n'

    result = subject.refresh()

    assert result.status == "built"
    assert reader.show_calls == [("b" * 40, "src/login.py")]


def test_incremental_reuse_never_copies_same_path_from_another_source(
    tmp_path: Path,
) -> None:
    reader = FakeGitReader()
    subject, target = service(tmp_path, reader)
    assert subject.build().status == "built"
    repo_readme = next(
        dict(row)
        for row in target.knowledge_rows()
        if row["source_path"] == "README.md"
    )
    other_readme = {
        **repo_readme,
        "chunk_id": "other-source-readme",
        "source_id": "repo-two",
        "content": "Repository two login notes.",
    }
    second_source = replace(
        config(tmp_path).sources[0],
        source_id="repo-two",
        include_code=False,
    )

    chunks, _, _, _ = subject._build_git_source(
        second_source,
        {"version": "b" * 40, "entries": reader.entries},
        {
            "manifest_json": '{"README.md": "1111111111111111111111111111111111111111"}'
        },
        [repo_readme, other_readme],
    )

    assert len(chunks) == 1
    assert chunks[0].source_id == "repo-two"
    assert chunks[0].content == "Repository two login notes."


def test_query_refresh_failure_uses_old_index_and_marks_stale(tmp_path: Path) -> None:
    reader = FakeGitReader()
    subject, target = service(tmp_path, reader)
    subject.build()
    old_rows = target.knowledge_rows()
    reader.fail = True

    result = subject.query("Login failures")

    assert result["stale"] is True
    assert result["evidence"]
    assert target.knowledge_rows() == old_rows


def test_state_publish_failure_keeps_previous_generation_active(tmp_path: Path) -> None:
    reader = FakeGitReader()
    backend = FailingStateBackend()
    subject, target = service(tmp_path, reader, backend=backend)
    assert subject.build().status == "built"
    old_rows = target.knowledge_rows()

    reader.sha = "b" * 40
    reader.entries = (
        replace(reader.entries[0]),
        GitTreeEntry(
            mode="100644",
            object_type="blob",
            object_id="4" * 40,
            path="src/login.py",
        ),
    )
    reader.blobs["src/login.py"] = b"def authenticate_v2(user):\n    pass\n"
    backend.fail_source_state = True

    result = subject.refresh()

    assert result.status == "failed"
    assert result.source_versions == {"repo": "a" * 40}
    assert target.knowledge_rows() == old_rows


def test_first_publish_failure_exposes_no_unactivated_generation(
    tmp_path: Path,
) -> None:
    reader = FakeGitReader()
    backend = FailingStateBackend()
    backend.fail_source_state = True
    subject, target = service(tmp_path, reader, backend=backend)

    result = subject.build()

    assert result.status == "failed"
    assert target.source_state() == ()
    assert target.knowledge_rows() == ()


def test_embedding_failure_returns_fts_and_supplemental_suggestion(tmp_path: Path) -> None:
    reader = FakeGitReader()
    subject, _ = service(tmp_path, reader, unavailable=True)
    assert subject.build().degraded is True

    result = subject.query("authenticate")

    assert result["mode"] == "fts_degraded"
    assert result["evidence"]
    assert result["supplemental_skills"] == ["jenkins-operations"]
    assert result["summary_requests"][0]["symbol"] == "authenticate"


def test_missing_collected_material_requests_information_collection(tmp_path: Path) -> None:
    reader = FakeGitReader()
    subject, target = service(tmp_path, reader, collected=True)

    result = subject.build()

    assert result.status == "needs_materialization"
    assert result.materialization_sources == ("wiki",)
    assert target.knowledge_rows() == ()


def test_enrichment_is_inferred_and_bound_to_commit(tmp_path: Path) -> None:
    reader = FakeGitReader()
    subject, target = service(tmp_path, reader)
    subject.build()

    result = subject.refresh(
        enrichment=(
            {
                "source_id": "repo",
                "path": "src/login.py",
                "symbol": "authenticate",
                "summary": "验证用户登录信息。",
                "commit_sha": "a" * 40,
            },
        )
    )

    assert result.status == "built"
    enriched = next(
        row for row in target.knowledge_rows()
        if row["source_kind"] == "generated-code-summary"
    )
    assert enriched["inferred"] is True
    assert enriched["source_version"] == "a" * 40


def test_degraded_build_retries_vectors_when_source_is_unchanged(tmp_path: Path) -> None:
    reader = FakeGitReader()
    subject, target = service(tmp_path, reader, unavailable=True)
    assert subject.build().degraded is True
    subject._embedder.unavailable = False

    result = subject.refresh()

    assert result.status == "built"
    assert result.degraded is False
    assert all(row["vector_json"] for row in target.knowledge_rows())
