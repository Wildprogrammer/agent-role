from __future__ import annotations

from pathlib import Path
import sys

import pytest

from agent_workflow_hub.knowledge_support_agent.documents import (
    DocumentDependencyUnavailable,
    parse_document,
    should_index_git_path,
)


def parse(path: str, content: str):
    return parse_document(
        source_id="docs",
        source_kind="git",
        source_version="a" * 40,
        source_path=path,
        content=content.encode("utf-8"),
    )


def test_markdown_splits_on_headings_and_keeps_provenance() -> None:
    chunks = parse(
        "README.md",
        "# ExamplePortal\n\n项目介绍。\n\n## 登录\n\n使用账号密码登录。\n",
    )

    assert [chunk.section for chunk in chunks] == [
        "ExamplePortal",
        "ExamplePortal / 登录",
    ]
    assert chunks[1].content == "使用账号密码登录。"
    assert chunks[1].provenance["commit_sha"] == "a" * 40
    assert chunks[1].inferred is False


def test_long_unicode_text_uses_character_overlap_and_stable_ids() -> None:
    content = "中文知识。" * 400
    first = parse("docs/long.txt", content)
    second = parse("docs/long.txt", content)

    assert len(first) > 1
    assert max(len(chunk.content) for chunk in first) <= 1200
    assert first[0].content[-150:] == first[1].content[:150]
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]


@pytest.mark.parametrize(
    "path",
    (
        ".env",
        "docs/environment-validated.ini",
        "artifacts/network.har",
        "reports/result.zip",
        "trace/run.trace",
        "screenshots/login.png",
        "logs/agent.log",
        "node_modules/pkg/README.md",
        "__pycache__/module.py",
    ),
)
def test_git_path_filter_rejects_runtime_or_sensitive_content(path: str) -> None:
    assert should_index_git_path(path, include_code=True) is False


def test_git_path_filter_accepts_documents_and_optional_code() -> None:
    assert should_index_git_path("README.md", include_code=False) is True
    assert should_index_git_path("docs/guide.txt", include_code=False) is True
    assert should_index_git_path("docs/manual.docx", include_code=False) is False
    assert should_index_git_path("docs/manual.pdf", include_code=False) is False
    assert should_index_git_path("src/login.py", include_code=False) is False
    assert should_index_git_path("src/login.py", include_code=True) is True


def test_missing_docx_or_pdf_dependency_is_explicit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setitem(sys.modules, "docx", None)
    monkeypatch.setitem(sys.modules, "pypdf", None)
    for suffix in (".docx", ".pdf"):
        with pytest.raises(DocumentDependencyUnavailable):
            parse_document(
                source_id="local",
                source_kind="local-file",
                source_version="v1",
                source_path=str(tmp_path / f"sample{suffix}"),
                content=b"not-a-real-document",
            )
