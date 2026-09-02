from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import agent_workflow_hub.knowledge_support_agent.cli as cli


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "knowledge_support_agent.py"
ROOT = Path(__file__).resolve().parents[3]


def config_file(tmp_path: Path) -> Path:
    document = tmp_path / "product.md"
    document.write_text("# Product\n\nA widget.", encoding="utf-8")
    config = {
        "schema_version": "1.0",
        "agent_id": "product-support",
        "display_name": "Product Support",
        "purpose": "Answer product questions",
        "audiences": ["internal"],
        "workdir": str((tmp_path / "runtime").resolve()),
        "sources": [
            {"id": "product", "type": "local-file", "path": str(document.resolve())}
        ],
        "supplemental_skills": [],
        "embedding": {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model": "qwen3-embedding:0.6b",
            "fallback": "fts",
        },
    }
    target = tmp_path / "agent.json"
    target.write_text(json.dumps(config), encoding="utf-8")
    return target.resolve()


class FakeService:
    def health(self):
        return {"status": "ready"}

    def build(self):
        return {"status": "built", "indexed_chunks": 1}

    def refresh(self, enrichment=()):
        return {"status": "built", "enrichment": len(enrichment)}

    def query(self, text):
        return {"status": "ok", "question": text, "evidence": []}

    def feedback(self, experience):
        return {"status": "stored", "experience_id": experience.experience_id}


def output(capsys) -> dict[str, object]:
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_relative_config_is_json_input_error(capsys) -> None:
    assert cli.main(["health", "--config", "agent.json"]) == 2
    assert output(capsys)["error"] == "input_error"


def test_health_reports_missing_lancedb_without_installing(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: None)

    code = cli.main(["health", "--config", str(config_file(tmp_path))])
    result = output(capsys)

    assert code == 3
    assert result["status"] == "needs_dependency"
    assert result["dependencies"]["lancedb"] is False
    assert result["actions"] == ["prepare the workflow's locked Python runtime"]


def test_health_ignores_host_proxy_for_loopback_model_check(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    options: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"models": [{"name": "qwen3-embedding:0.6b"}]}

    class LocalClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            return Response()

    class HealthyEmbedder:
        def __init__(self, **kwargs):
            pass

        def embed(self, texts):
            return ((1.0,),)

    def build_client(**kwargs):
        options.update(kwargs)
        return LocalClient()

    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(cli.httpx, "Client", build_client)
    monkeypatch.setattr(cli, "OllamaEmbeddingClient", HealthyEmbedder)

    code = cli.main(["health", "--config", str(config_file(tmp_path))])
    result = output(capsys)

    assert code == 0
    assert result["status"] == "ready"
    assert result["embedding"]["available"] is True
    assert options["trust_env"] is False


def test_query_reads_body_only_from_absolute_json_file(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_service_from_config", lambda config: FakeService())
    request = tmp_path / "query.json"
    request.write_text(json.dumps({"text": "产品是什么？"}), encoding="utf-8")

    code = cli.main(
        [
            "query",
            "--config",
            str(config_file(tmp_path)),
            "--request",
            str(request.resolve()),
        ]
    )
    result = output(capsys)

    assert code == 0
    assert result["question"] == "产品是什么？"
    assert "--text" not in cli.build_parser().format_help()


def test_feedback_requires_confirmed_boolean(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli, "_service_from_config", lambda config: FakeService())
    request = tmp_path / "feedback.json"
    request.write_text(
        json.dumps(
            {
                "experience_id": "product-origin",
                "question": "产地？",
                "answer": "中国。",
                "scope": "产品 A",
                "confirmed": False,
                "confirmed_at": "2026-09-01T10:00:00+08:00",
                "supersedes": None,
            }
        ),
        encoding="utf-8",
    )

    code = cli.main(
        [
            "feedback",
            "--config",
            str(config_file(tmp_path)),
            "--request",
            str(request.resolve()),
        ]
    )

    assert code == 2
    assert output(capsys)["error"] == "input_error"


def test_script_can_load_without_installed_hub_package() -> None:
    spec = importlib.util.spec_from_file_location("knowledge_support_script", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert callable(module.main)


def test_cli_emits_utf8_even_when_process_output_encoding_is_gbk() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONIOENCODING"] = "gbk"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from agent_workflow_hub.knowledge_support_agent.cli import _emit; "
                "_emit({'text': '\\u4e2d\\u6587'})"
            ),
        ],
        check=True,
        capture_output=True,
        env=environment,
    )

    assert json.loads(result.stdout.decode("utf-8")) == {"text": "中文"}
