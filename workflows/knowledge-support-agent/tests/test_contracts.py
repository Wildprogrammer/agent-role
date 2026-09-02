from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_workflow_hub.knowledge_support_agent import (
    KnowledgeSupportContractError,
    canonical_json_sha256,
    database_path,
    load_agent_config,
)


def valid_config(tmp_path: Path) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "agent_id": "product-support",
        "display_name": "产品答疑",
        "purpose": "回答内部产品问题",
        "audiences": ["product", "support"],
        "workdir": str((tmp_path / "runtime").resolve()),
        "sources": [
            {
                "id": "product-docs",
                "type": "local-file",
                "path": str((tmp_path / "product.md").resolve()),
            },
            {
                "id": "collected-site",
                "type": "collected-document",
                "path": str((tmp_path / "site.txt").resolve()),
                "origin_url": "https://example.invalid/products/widget",
            },
            {
                "id": "source-code",
                "type": "git",
                "repository": str((tmp_path / "repo").resolve()),
                "include_code": True,
            },
        ],
        "supplemental_skills": [
            {"name": "product-catalog", "purpose": "查询实时商品信息"}
        ],
        "embedding": {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen3-embedding:0.6b",
            "fallback": "fts",
        },
    }


def write_config(tmp_path: Path, value: dict[str, object]) -> Path:
    target = tmp_path / "agent.json"
    target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return target


def test_load_agent_config_and_fixed_database_path(tmp_path: Path) -> None:
    config = load_agent_config(write_config(tmp_path, valid_config(tmp_path)))

    assert config.agent_id == "product-support"
    assert tuple(source.source_id for source in config.sources) == (
        "product-docs",
        "collected-site",
        "source-code",
    )
    assert config.embedding.model == "qwen3-embedding:0.6b"
    assert database_path(config) == (
        tmp_path / "runtime" / "knowledge-support" / "lancedb"
    ).resolve()


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: value.update({"unexpected": True}),
        lambda value: value.update({"database": "C:/elsewhere"}),
        lambda value: value.update({"workdir": "relative/runtime"}),
        lambda value: value["embedding"].update({"token": "secret"}),
        lambda value: value["embedding"].update(
            {"base_url": "http://user:pass@127.0.0.1:11434"}
        ),
        lambda value: value["sources"][0].update({"path": "relative.md"}),
        lambda value: value["sources"][1].update(
            {"origin_url": "https://user:pass@example.invalid/wiki"}
        ),
        lambda value: value["sources"][2].update({"repository": "repo"}),
        lambda value: value["sources"][0].update({"type": "wiki"}),
    ),
)
def test_config_rejects_unsafe_or_unknown_values(tmp_path: Path, mutation) -> None:
    value = valid_config(tmp_path)
    mutation(value)

    with pytest.raises(KnowledgeSupportContractError):
        load_agent_config(write_config(tmp_path, value))


def test_config_rejects_duplicate_source_ids(tmp_path: Path) -> None:
    value = valid_config(tmp_path)
    value["sources"][1]["id"] = value["sources"][0]["id"]

    with pytest.raises(KnowledgeSupportContractError, match="duplicate source id"):
        load_agent_config(write_config(tmp_path, value))


def test_config_rejects_noncanonical_embedding_contract(tmp_path: Path) -> None:
    for key, invalid in (
        ("provider", "cloud"),
        ("model", "another-model"),
        ("fallback", "stop"),
    ):
        value = valid_config(tmp_path)
        value["embedding"][key] = invalid
        with pytest.raises(KnowledgeSupportContractError):
            load_agent_config(write_config(tmp_path, value))


def test_canonical_hash_is_order_independent() -> None:
    assert canonical_json_sha256({"a": 1, "b": 2}) == canonical_json_sha256(
        {"b": 2, "a": 1}
    )
