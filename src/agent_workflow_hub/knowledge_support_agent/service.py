"""Build, refresh, and query orchestration for a configured knowledge agent."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Literal, Mapping, Protocol, Sequence

from agent_workflow_hub.git_operations import (
    committed_blob_argv,
    committed_tree_argv,
    head_commit_argv,
    validate_commit_sha,
)

from .code_facts import extract_code_facts
from .contracts import AgentConfig, canonical_json_sha256
from .documents import KnowledgeChunk, _chunk, parse_document, should_index_git_path
from .embeddings import EmbeddingUnavailable, OllamaEmbeddingClient
from .feedback import ConfirmedExperience, store_confirmed_experience
from .store import KnowledgeStore


@dataclass(frozen=True, kw_only=True)
class GitTreeEntry:
    mode: str
    object_type: str
    object_id: str
    path: str


class GitReader(Protocol):
    def head_sha(self, repository: Path) -> str: ...

    def list_tree(self, repository: Path, sha: str) -> tuple[GitTreeEntry, ...]: ...

    def show_file(self, repository: Path, sha: str, path: str) -> bytes: ...


class GitCommandReader:
    """Read commits only through git-operations' exact argv constructors."""

    def head_sha(self, repository: Path) -> str:
        output = self._run(repository, head_commit_argv())
        return validate_commit_sha(output.decode("ascii").strip())

    def list_tree(self, repository: Path, sha: str) -> tuple[GitTreeEntry, ...]:
        output = self._run(repository, committed_tree_argv(sha))
        entries: list[GitTreeEntry] = []
        for raw in output.split(b"\x00"):
            if not raw:
                continue
            metadata, separator, path = raw.partition(b"\t")
            fields = metadata.decode("ascii").split()
            if not separator or len(fields) != 3:
                raise RuntimeError("git ls-tree returned malformed output")
            entries.append(
                GitTreeEntry(
                    mode=fields[0],
                    object_type=fields[1],
                    object_id=fields[2],
                    path=path.decode("utf-8", errors="surrogateescape"),
                )
            )
        return tuple(entries)

    def show_file(self, repository: Path, sha: str, path: str) -> bytes:
        return self._run(repository, committed_blob_argv(sha, path))

    @staticmethod
    def _run(repository: Path, command: tuple[str, ...]) -> bytes:
        result = subprocess.run(
            [command[0], "-C", str(repository), *command[1:]],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            diagnostic = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(diagnostic or "git committed snapshot read failed")
        return result.stdout


@dataclass(frozen=True, kw_only=True)
class BuildResult:
    status: Literal["built", "unchanged", "needs_materialization", "failed"]
    source_versions: Mapping[str, str]
    indexed_chunks: int
    skipped_files: int
    degraded: bool
    materialization_sources: tuple[str, ...] = ()
    error: str | None = None


class KnowledgeSupportService:
    def __init__(
        self,
        config: AgentConfig,
        *,
        store: KnowledgeStore,
        embedder: OllamaEmbeddingClient,
        git_reader: GitReader | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._embedder = embedder
        self._git = git_reader or GitCommandReader()

    def health(self) -> Mapping[str, object]:
        return {
            "status": "ready",
            "agent_id": self._config.agent_id,
            "database": str(self._store.database_dir),
            "source_state": list(self._store.source_state()),
            "tables": list(self._store.table_names()),
        }

    def build(self) -> BuildResult:
        return self._refresh(())

    def refresh(
        self,
        enrichment: Sequence[Mapping[str, object]] = (),
    ) -> BuildResult:
        return self._refresh(enrichment)

    def query(self, text: str) -> Mapping[str, object]:
        stale = False
        state = {str(row["source_id"]): str(row["version"]) for row in self._store.source_state()}
        try:
            current = self._current_versions()
            if not state:
                build = self.build()
                stale = build.status not in {"built", "unchanged"}
            elif current != state:
                refresh = self.refresh()
                stale = refresh.status not in {"built", "unchanged"}
        except Exception:
            stale = True
        try:
            query_vector = self._embedder.embed([text])[0]
            embedding_error = None
        except EmbeddingUnavailable as exc:
            query_vector = None
            embedding_error = str(exc)
        result = self._store.search(
            text,
            query_vector=query_vector,
            embedding_error=embedding_error,
        )
        summary_requests = []
        verification_candidates = []
        for evidence in result.evidence:
            provenance = evidence["provenance"]
            if evidence["source_kind"] == "code-fact":
                verification_candidates.append(
                    {
                        "path": evidence["source_path"],
                        "commit_sha": provenance.get("commit_sha"),
                        "symbol": provenance.get("symbol"),
                    }
                )
                if provenance.get("summary_state") == "needed":
                    summary_requests.append(
                        {
                            "source_id": evidence["source_id"],
                            "path": evidence["source_path"],
                            "commit_sha": provenance.get("commit_sha"),
                            "symbol": provenance.get("symbol"),
                            "signature": provenance.get("signature"),
                        }
                    )
        return {
            "status": "ok",
            "mode": result.mode,
            "stale": stale,
            "degraded_reason": result.degraded_reason,
            "evidence": list(result.evidence),
            "verification_candidates": verification_candidates,
            "summary_requests": summary_requests,
            "supplemental_skills": [
                skill.name for skill in self._config.supplemental_skills
            ],
        }

    def feedback(self, experience: ConfirmedExperience) -> Mapping[str, object]:
        record = store_confirmed_experience(self._store, experience)
        return {
            "status": "stored",
            "record_id": record.record_id,
            "version": record.version,
            "supersedes": record.supersedes,
        }

    def _refresh(self, enrichment: Sequence[Mapping[str, object]]) -> BuildResult:
        old_state_rows = [dict(row) for row in self._store.source_state()]
        old_state = {str(row["source_id"]): row for row in old_state_rows}
        old_rows = [dict(row) for row in self._store.knowledge_rows()]
        try:
            snapshots, missing = self._snapshots()
            versions = {source_id: snapshot["version"] for source_id, snapshot in snapshots.items()}
            if missing:
                return BuildResult(
                    status="needs_materialization",
                    source_versions=versions,
                    indexed_chunks=len(old_rows),
                    skipped_files=0,
                    degraded=False,
                    materialization_sources=tuple(missing),
                )
            if (
                not enrichment
                and old_state
                and versions
                == {source_id: str(row["version"]) for source_id, row in old_state.items()}
                and all(
                    row.get("source_kind") == "user-confirmed-experience"
                    or bool(row.get("vector_json"))
                    for row in old_rows
                )
            ):
                return BuildResult(
                    status="unchanged",
                    source_versions=versions,
                    indexed_chunks=len(old_rows),
                    skipped_files=0,
                    degraded=False,
                )
            chunks: list[KnowledgeChunk] = []
            vectors: dict[str, Sequence[float]] = {}
            state_rows: list[dict[str, object]] = []
            skipped = 0
            for source in self._config.sources:
                snapshot = snapshots[source.source_id]
                previous = old_state.get(source.source_id)
                source_rows = [
                    row
                    for row in old_rows
                    if row.get("source_id") == source.source_id
                    and row.get("source_kind") != "generated-code-summary"
                ]
                if previous and str(previous["version"]) == snapshot["version"]:
                    carried = [_row_to_chunk(row) for row in source_rows]
                    chunks.extend(carried)
                    _carry_vectors(source_rows, vectors)
                    state_rows.append(previous)
                    continue
                if source.source_type == "git":
                    produced, reused_vectors, manifest, skipped_count = self._build_git_source(
                        source,
                        snapshot,
                        previous,
                        source_rows,
                    )
                    chunks.extend(produced)
                    vectors.update(reused_vectors)
                    skipped += skipped_count
                    state_rows.append(
                        {
                            "source_id": source.source_id,
                            "version": snapshot["version"],
                            "manifest_json": json.dumps(manifest, sort_keys=True),
                        }
                    )
                else:
                    produced = parse_document(
                        source_id=source.source_id,
                        source_kind=source.source_type,
                        source_version=snapshot["version"],
                        source_path=str(source.path),
                        content=snapshot["content"],
                        origin_url=source.origin_url,
                    )
                    chunks.extend(produced)
                    state_rows.append(
                        {
                            "source_id": source.source_id,
                            "version": snapshot["version"],
                            "manifest_json": "{}",
                        }
                    )
            replacement_keys = {
                (
                    str(value.get("source_id", "")),
                    str(value.get("path", "")),
                    str(value.get("symbol", "")),
                )
                for value in enrichment
            }
            generated_rows = [
                row
                for row in old_rows
                if row.get("source_kind") == "generated-code-summary"
                and versions.get(str(row.get("source_id")))
                == str(row.get("source_version"))
                and (
                    str(row.get("source_id")),
                    str(row.get("source_path")),
                    str(json.loads(str(row.get("provenance_json", "{}"))).get("symbol", "")),
                )
                not in replacement_keys
            ]
            chunks.extend(_row_to_chunk(row) for row in generated_rows)
            _carry_vectors(generated_rows, vectors)
            for value in enrichment:
                chunks.append(self._enrichment_chunk(value, versions))
            pending = [chunk for chunk in chunks if chunk.chunk_id not in vectors]
            degraded = False
            if pending:
                try:
                    embedded = self._embedder.embed([chunk.content for chunk in pending])
                    vectors.update(
                        {chunk.chunk_id: vector for chunk, vector in zip(pending, embedded)}
                    )
                except EmbeddingUnavailable:
                    degraded = True
            generation = canonical_json_sha256(
                {
                    "agent_id": self._config.agent_id,
                    "versions": versions,
                    "chunks": [chunk.chunk_id for chunk in chunks],
                    "vectorized": sorted(vectors),
                }
            )
            self._store.publish_generation(
                chunks,
                vectors=vectors,
                generation=generation,
                source_state=state_rows,
            )
            return BuildResult(
                status="built",
                source_versions=versions,
                indexed_chunks=len(chunks),
                skipped_files=skipped,
                degraded=degraded,
            )
        except Exception as exc:
            return BuildResult(
                status="failed",
                source_versions={
                    str(row["source_id"]): str(row["version"])
                    for row in old_state_rows
                },
                indexed_chunks=len(old_rows),
                skipped_files=0,
                degraded=False,
                error=str(exc),
            )

    def _snapshots(self):
        snapshots: dict[str, dict[str, object]] = {}
        missing: list[str] = []
        for source in self._config.sources:
            if source.source_type == "git":
                assert source.repository is not None
                sha = self._git.head_sha(source.repository)
                entries = self._git.list_tree(source.repository, sha)
                snapshots[source.source_id] = {
                    "version": sha,
                    "entries": entries,
                }
            else:
                assert source.path is not None
                if not source.path.is_file():
                    if source.source_type == "collected-document":
                        missing.append(source.source_id)
                        continue
                    raise FileNotFoundError(source.path)
                content = source.path.read_bytes()
                snapshots[source.source_id] = {
                    "version": hashlib.sha256(content).hexdigest(),
                    "content": content,
                }
        return snapshots, missing

    def _current_versions(self) -> dict[str, str]:
        versions: dict[str, str] = {}
        for source in self._config.sources:
            if source.source_type == "git":
                assert source.repository is not None
                versions[source.source_id] = self._git.head_sha(source.repository)
                continue
            assert source.path is not None
            if not source.path.is_file():
                raise FileNotFoundError(source.path)
            versions[source.source_id] = hashlib.sha256(
                source.path.read_bytes()
            ).hexdigest()
        return versions

    def _build_git_source(self, source, snapshot, previous, old_rows):
        assert source.repository is not None
        sha = str(snapshot["version"])
        entries = tuple(snapshot["entries"])
        old_manifest = (
            json.loads(str(previous.get("manifest_json", "{}"))) if previous else {}
        )
        manifest: dict[str, str] = {}
        chunks: list[KnowledgeChunk] = []
        vectors: dict[str, Sequence[float]] = {}
        skipped = 0
        for entry in entries:
            if (
                entry.object_type != "blob"
                or entry.mode == "120000"
                or not should_index_git_path(entry.path, include_code=source.include_code)
            ):
                skipped += 1
                continue
            manifest[entry.path] = entry.object_id
            matching = [
                row
                for row in old_rows
                if row.get("source_id") == source.source_id
                and row.get("source_path") == entry.path
            ]
            if old_manifest.get(entry.path) == entry.object_id and matching:
                for row in matching:
                    carried = _row_to_chunk(row)
                    provenance = dict(carried.provenance)
                    provenance["commit_sha"] = sha
                    refreshed = _chunk(
                        source_id=carried.source_id,
                        source_kind=carried.source_kind,
                        source_version=sha,
                        source_path=carried.source_path,
                        title=carried.title,
                        section=carried.section,
                        content=carried.content,
                        provenance=provenance,
                        inferred=carried.inferred,
                    )
                    chunks.append(refreshed)
                    raw_vector = str(row.get("vector_json", ""))
                    if raw_vector:
                        vectors[refreshed.chunk_id] = json.loads(raw_vector)
                continue
            content = self._git.show_file(source.repository, sha, entry.path)
            suffix = Path(entry.path).suffix.casefold()
            if suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".vue"}:
                produced = extract_code_facts(
                    source_id=source.source_id,
                    path=entry.path,
                    content=content,
                    commit_sha=sha,
                )
            else:
                produced = parse_document(
                    source_id=source.source_id,
                    source_kind="git",
                    source_version=sha,
                    source_path=entry.path,
                    content=content,
                )
            chunks.extend(
                replace(
                    chunk,
                    provenance={**chunk.provenance, "blob_sha": entry.object_id},
                )
                for chunk in produced
            )
        return tuple(chunks), vectors, manifest, skipped

    def _enrichment_chunk(self, value: Mapping[str, object], versions: Mapping[str, str]):
        required = {"source_id", "path", "symbol", "summary", "commit_sha"}
        if set(value) != required:
            raise ValueError("enrichment fields mismatch")
        source_id = str(value["source_id"])
        sha = str(value["commit_sha"])
        if versions.get(source_id) != sha:
            raise ValueError("enrichment commit does not match current source")
        return _chunk(
            source_id=source_id,
            source_kind="generated-code-summary",
            source_version=sha,
            source_path=str(value["path"]),
            title=str(value["symbol"]),
            section="generated code summary",
            content=str(value["summary"]),
            provenance={
                "commit_sha": sha,
                "source_path": str(value["path"]),
                "symbol": str(value["symbol"]),
                "fact_type": "generated-code-summary",
            },
            inferred=True,
        )


def _row_to_chunk(row: Mapping[str, object]) -> KnowledgeChunk:
    return KnowledgeChunk(
        chunk_id=str(row["chunk_id"]),
        source_id=str(row["source_id"]),
        source_kind=str(row["source_kind"]),
        source_version=str(row["source_version"]),
        source_path=str(row["source_path"]),
        title=str(row["title"]),
        section=str(row["section"]),
        content=str(row["content"]),
        content_sha256=str(row["content_sha256"]),
        provenance=json.loads(str(row["provenance_json"])),
        inferred=bool(row["inferred"]),
    )


def _carry_vectors(
    rows: Sequence[Mapping[str, object]],
    target: dict[str, Sequence[float]],
) -> None:
    for row in rows:
        raw = str(row.get("vector_json", ""))
        if raw:
            target[str(row["chunk_id"])] = json.loads(raw)
