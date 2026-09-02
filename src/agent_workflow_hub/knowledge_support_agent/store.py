"""LanceDB persistence with deterministic local BM25 and vector fusion."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import os
import re
from typing import Mapping, Protocol, Sequence

from .documents import KnowledgeChunk


class StoreDependencyUnavailable(RuntimeError):
    """Raised when the required LanceDB runtime is absent."""


class StorePathUnsupported(RuntimeError):
    """Raised before LanceDB writes when Windows cannot persist its child files."""


class TableBackend(Protocol):
    def replace(self, name: str, rows: Sequence[Mapping[str, object]]) -> None: ...

    def append(self, name: str, rows: Sequence[Mapping[str, object]]) -> None: ...

    def read(self, name: str) -> list[dict[str, object]]: ...

    def table_names(self) -> tuple[str, ...]: ...


class InMemoryTableBackend:
    """Deterministic test backend; production never selects it implicitly."""

    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, object]]] = {}

    def replace(self, name: str, rows: Sequence[Mapping[str, object]]) -> None:
        self._tables[name] = [dict(row) for row in rows]

    def append(self, name: str, rows: Sequence[Mapping[str, object]]) -> None:
        self._tables.setdefault(name, []).extend(dict(row) for row in rows)

    def read(self, name: str) -> list[dict[str, object]]:
        return [dict(row) for row in self._tables.get(name, ())]

    def table_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tables))


class _LanceTableBackend:
    def __init__(self, directory: Path) -> None:
        module = _import_lancedb()
        if module is None:
            raise StoreDependencyUnavailable(
                "lancedb is required; use the workflow's locked runtime"
            )
        directory.mkdir(parents=True, exist_ok=True)
        self._database = module.connect(str(directory))

    def replace(self, name: str, rows: Sequence[Mapping[str, object]]) -> None:
        materialized = [dict(row) for row in rows]
        if not materialized:
            try:
                self._database.drop_table(name, ignore_missing=True)
            except TypeError:
                try:
                    self._database.drop_table(name)
                except Exception:
                    pass
            return
        self._database.create_table(name, data=materialized, mode="overwrite")

    def append(self, name: str, rows: Sequence[Mapping[str, object]]) -> None:
        materialized = [dict(row) for row in rows]
        if not materialized:
            return
        if name in self.table_names():
            self._database.open_table(name).add(materialized)
            return
        self._database.create_table(name, data=materialized)

    def read(self, name: str) -> list[dict[str, object]]:
        try:
            table = self._database.open_table(name)
        except Exception:
            return []
        return [dict(row) for row in table.to_arrow().to_pylist()]

    def table_names(self) -> tuple[str, ...]:
        try:
            names = self._database.table_names()
        except AttributeError:
            listing = self._database.list_tables()
            names = getattr(listing, "tables", listing)
        return tuple(sorted(str(name) for name in names))


def _import_lancedb():
    try:
        import lancedb
    except ImportError:
        return None
    return lancedb


@dataclass(frozen=True, kw_only=True)
class SearchResult:
    mode: str
    evidence: tuple[Mapping[str, object], ...]
    degraded_reason: str | None = None


class KnowledgeStore:
    _PUBLICATION_SENTINEL = "__knowledge_publication__"

    def __init__(
        self,
        database_dir: Path,
        *,
        backend: TableBackend | None = None,
    ) -> None:
        target = Path(database_dir)
        if not target.is_absolute():
            raise ValueError("LanceDB directory must be absolute")
        self.database_dir = target.resolve()
        if os.name == "nt" and len(str(self.database_dir)) > 170:
            raise StorePathUnsupported(
                "Windows LanceDB directory must be at most 170 characters so "
                "internal data files remain below MAX_PATH"
            )
        self._backend = backend or _LanceTableBackend(self.database_dir)

    def table_names(self) -> tuple[str, ...]:
        return self._backend.table_names()

    def replace_knowledge(
        self,
        chunks: Sequence[KnowledgeChunk],
        *,
        vectors: Mapping[str, Sequence[float]],
        generation: str,
    ) -> None:
        rows = [
            _chunk_row(
                chunk,
                generation=generation,
                vector=vectors.get(chunk.chunk_id),
            )
            for chunk in chunks
        ]
        feedback_rows = [
            row
            for row in self._backend.read("knowledge")
            if row.get("source_kind") == "user-confirmed-experience"
        ]
        self._backend.replace("knowledge", [*rows, *feedback_rows])

    def replace_source_state(self, rows: Sequence[Mapping[str, object]]) -> None:
        self._backend.replace("source_state", rows)

    def publish_generation(
        self,
        chunks: Sequence[KnowledgeChunk],
        *,
        vectors: Mapping[str, Sequence[float]],
        generation: str,
        source_state: Sequence[Mapping[str, object]],
    ) -> None:
        """Append a complete generation, then atomically make it active.

        The source-state pointer is written last.  If that write fails, readers
        continue filtering on the previous generation and the newly appended
        rows remain harmless, inactive recovery data.
        """
        if not self._backend.read("source_state"):
            self._backend.replace(
                "source_state",
                [
                    {
                        "source_id": self._PUBLICATION_SENTINEL,
                        "version": "",
                        "manifest_json": "{}",
                        "generation": "",
                    }
                ],
            )
        rows = [
            _chunk_row(
                chunk,
                generation=generation,
                vector=vectors.get(chunk.chunk_id),
            )
            for chunk in chunks
        ]
        existing_keys = {
            (str(row.get("generation", "")), str(row.get("chunk_id", "")))
            for row in self._backend.read("knowledge")
        }
        pending = [
            row
            for row in rows
            if (str(row["generation"]), str(row["chunk_id"])) not in existing_keys
        ]
        self._backend.append("knowledge", pending)
        active_state = [dict(row, generation=generation) for row in source_state]
        self._backend.replace("source_state", active_state)

    def source_state(self) -> tuple[Mapping[str, object], ...]:
        return tuple(
            row
            for row in self._backend.read("source_state")
            if row.get("source_id") != self._PUBLICATION_SENTINEL
        )

    def knowledge_rows(self) -> tuple[Mapping[str, object], ...]:
        return tuple(self._active_knowledge_rows())

    def experience_rows(self) -> tuple[Mapping[str, object], ...]:
        return tuple(self._backend.read("experience"))

    def replace_experiences(self, rows: Sequence[Mapping[str, object]]) -> None:
        self._backend.replace("experience", rows)

    def search(
        self,
        query: str,
        *,
        query_vector: Sequence[float] | None,
        limit: int = 8,
        embedding_error: str | None = None,
    ) -> SearchResult:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be non-empty text")
        if limit < 1:
            raise ValueError("limit must be positive")
        rows = self._active_knowledge_rows()
        lexical = _bm25_rank(query, rows)
        vector = _vector_rank(query_vector, rows) if query_vector is not None else []
        scores: dict[str, dict[str, float]] = {}
        by_id = {str(row["chunk_id"]): row for row in rows}
        for rank, (identifier, raw_score) in enumerate(lexical[:20], start=1):
            entry = scores.setdefault(identifier, {})
            entry["fts"] = raw_score
            entry["rrf"] = entry.get("rrf", 0.0) + 1.0 / (60 + rank)
        for rank, (identifier, raw_score) in enumerate(vector[:20], start=1):
            entry = scores.setdefault(identifier, {})
            entry["vector"] = raw_score
            entry["rrf"] = entry.get("rrf", 0.0) + 1.0 / (60 + rank)
        ordered = sorted(
            scores,
            key=lambda identifier: (-scores[identifier]["rrf"], identifier),
        )[:limit]
        evidence = tuple(
            {
                "chunk_id": identifier,
                "source_id": by_id[identifier]["source_id"],
                "source_kind": by_id[identifier]["source_kind"],
                "source_version": by_id[identifier]["source_version"],
                "source_path": by_id[identifier]["source_path"],
                "title": by_id[identifier]["title"],
                "section": by_id[identifier]["section"],
                "content": by_id[identifier]["content"],
                "inferred": bool(by_id[identifier]["inferred"]),
                "provenance": json.loads(str(by_id[identifier]["provenance_json"])),
                "scores": scores[identifier],
            }
            for identifier in ordered
        )
        if query_vector is None:
            return SearchResult(
                mode="fts_degraded",
                evidence=evidence,
                degraded_reason=embedding_error or "embedding unavailable",
            )
        return SearchResult(mode="hybrid", evidence=evidence)

    def _active_knowledge_rows(self) -> list[dict[str, object]]:
        rows = self._backend.read("knowledge")
        experience_rows = [
            _experience_search_row(row)
            for row in self._backend.read("experience")
            if row.get("active")
        ]
        current_experience_sources = {
            str(row["source_id"]) for row in experience_rows
        }
        raw_state = self._backend.read("source_state")
        real_state = [
            row
            for row in raw_state
            if row.get("source_id") != self._PUBLICATION_SENTINEL
        ]
        generations = {
            str(row["generation"])
            for row in real_state
            if row.get("generation")
        }
        if generations:
            active_rows = [
                row
                for row in rows
                if str(row.get("generation", "")) in generations
                or row.get("source_kind") == "user-confirmed-experience"
            ]
        elif real_state:
            # Legacy databases predate generation pointers.  Match their active
            # source versions so a failed upgrade cannot expose appended rows.
            active_versions = {
                (str(row.get("source_id", "")), str(row.get("version", "")))
                for row in real_state
            }
            active_rows = [
                row
                for row in rows
                if row.get("source_kind") == "user-confirmed-experience"
                or (
                    str(row.get("source_id", "")),
                    str(row.get("source_version", "")),
                )
                in active_versions
            ]
        elif not raw_state:
            # Direct store callers without source state use their supplied rows.
            active_rows = rows
        else:
            # A sentinel-only state means the first generation was never activated.
            active_rows = [
                row
                for row in rows
                if row.get("source_kind") == "user-confirmed-experience"
            ]
        active_rows = [
            row
            for row in active_rows
            if not (
                row.get("source_kind") == "user-confirmed-experience"
                and str(row.get("source_id", "")) in current_experience_sources
            )
        ]
        return [*active_rows, *experience_rows]


_LATIN_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK_RUN = re.compile(r"[\u3400-\u9fff]+")


def _tokens(text: str) -> list[str]:
    lowered = text.casefold()
    tokens = _LATIN_WORD.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        tokens.extend(
            run
            if len(run) == 1
            else (run[i : i + 2] for i in range(len(run) - 1))
        )
    return tokens


def _bm25_rank(
    query: str,
    rows: Sequence[Mapping[str, object]],
) -> list[tuple[str, float]]:
    query_terms = set(_tokens(query))
    documents = [
        _tokens(
            " ".join(
                (
                    str(row.get("title", "")),
                    str(row.get("section", "")),
                    str(row.get("content", "")),
                )
            )
        )
        for row in rows
    ]
    if not query_terms or not documents:
        return []
    document_frequency = {
        term: sum(term in document for document in documents) for term in query_terms
    }
    average_length = sum(len(document) for document in documents) / len(documents) or 1.0
    ranked: list[tuple[str, float]] = []
    for row, document in zip(rows, documents):
        counts = Counter(document)
        score = 0.0
        for term in query_terms:
            frequency = counts[term]
            if frequency == 0:
                continue
            inverse = math.log(
                1.0 + (len(documents) - document_frequency[term] + 0.5)
                / (document_frequency[term] + 0.5)
            )
            denominator = frequency + 1.5 * (
                1.0 - 0.75 + 0.75 * len(document) / average_length
            )
            score += inverse * frequency * 2.5 / denominator
        if score > 0:
            ranked.append((str(row["chunk_id"]), score))
    return sorted(ranked, key=lambda item: (-item[1], item[0]))


def _vector_rank(
    query_vector: Sequence[float],
    rows: Sequence[Mapping[str, object]],
) -> list[tuple[str, float]]:
    ranked: list[tuple[str, float]] = []
    for row in rows:
        raw = str(row.get("vector_json", ""))
        if not raw:
            continue
        try:
            vector = [float(value) for value in json.loads(raw)]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        similarity = _cosine(query_vector, vector)
        if similarity > 0:
            ranked.append((str(row["chunk_id"]), similarity))
    return sorted(ranked, key=lambda item: (-item[1], item[0]))


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(float(a) * float(b) for a, b in zip(left, right))
    left_norm = math.sqrt(sum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(sum(float(value) ** 2 for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _chunk_row(
    chunk: KnowledgeChunk,
    *,
    generation: str,
    vector: Sequence[float] | None,
) -> dict[str, object]:
    return {
        "chunk_id": chunk.chunk_id,
        "source_id": chunk.source_id,
        "source_kind": chunk.source_kind,
        "source_version": chunk.source_version,
        "source_path": chunk.source_path,
        "title": chunk.title,
        "section": chunk.section,
        "content": chunk.content,
        "content_sha256": chunk.content_sha256,
        "provenance_json": json.dumps(
            dict(chunk.provenance),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "inferred": chunk.inferred,
        "generation": generation,
        "vector_json": json.dumps(list(vector)) if vector is not None else "",
    }


def _experience_search_row(row: Mapping[str, object]) -> dict[str, object]:
    experience_id = str(row["experience_id"])
    record_id = str(row["record_id"])
    content = str(row["answer"])
    identity = json.dumps(
        {
            "record_id": record_id,
            "question": str(row["question"]),
            "answer": content,
            "scope": str(row["scope"]),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "chunk_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "source_id": f"experience:{experience_id}",
        "source_kind": "user-confirmed-experience",
        "source_version": record_id,
        "source_path": f"experience/{experience_id}",
        "title": str(row["question"]),
        "section": str(row["scope"]),
        "content": content,
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "provenance_json": json.dumps(
            {
                "experience_id": experience_id,
                "experience_version": int(row["version"]),
                "record_id": record_id,
                "confirmed_at": str(row["confirmed_at"]),
                "supersedes": row.get("supersedes"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        "inferred": False,
        "generation": "feedback",
        "vector_json": "",
    }
