"""Single-object JSON CLI for the knowledge support agent core."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
import importlib.util
import json
from pathlib import Path
import sys
from typing import Mapping, Sequence

import httpx

from .contracts import (
    KnowledgeSupportContractError,
    database_path,
    load_agent_config,
)
from .embeddings import EmbeddingUnavailable, OllamaEmbeddingClient
from .feedback import ConfirmedExperience, FeedbackError
from .service import KnowledgeSupportService
from .store import KnowledgeStore, StoreDependencyUnavailable


class CliInputError(ValueError):
    pass


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliInputError(message)


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path.resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(prog="knowledge_support_agent.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("health", "build"):
        item = subparsers.add_parser(command)
        item.add_argument("--config", required=True, type=_absolute_path)
    query = subparsers.add_parser("query")
    query.add_argument("--config", required=True, type=_absolute_path)
    query.add_argument("--request", required=True, type=_absolute_path)
    refresh = subparsers.add_parser("refresh")
    refresh.add_argument("--config", required=True, type=_absolute_path)
    refresh.add_argument("--enrichment", type=_absolute_path)
    feedback = subparsers.add_parser("feedback")
    feedback.add_argument("--config", required=True, type=_absolute_path)
    feedback.add_argument("--request", required=True, type=_absolute_path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        config = load_agent_config(args.config)
        if args.command == "health":
            result = _dependency_health(config)
            _emit(result)
            return 3 if result["status"] == "needs_dependency" else 0
        service = _service_from_config(config)
        if args.command == "build":
            result = _jsonable(service.build())
        elif args.command == "query":
            request = _read_object(args.request)
            if set(request) != {"text"} or not isinstance(request["text"], str):
                raise CliInputError("query request must contain only non-empty text")
            if not request["text"].strip():
                raise CliInputError("query request must contain only non-empty text")
            result = service.query(request["text"])
        elif args.command == "refresh":
            enrichment = ()
            if args.enrichment is not None:
                raw = _read_json(args.enrichment)
                if not isinstance(raw, list) or not all(
                    isinstance(item, dict) for item in raw
                ):
                    raise CliInputError("enrichment must be a JSON array of objects")
                enrichment = tuple(raw)
            result = _jsonable(service.refresh(enrichment=enrichment))
        elif args.command == "feedback":
            request = _read_object(args.request)
            required = {
                "experience_id",
                "question",
                "answer",
                "scope",
                "confirmed",
                "confirmed_at",
                "supersedes",
            }
            if set(request) != required or request["confirmed"] is not True:
                raise CliInputError(
                    "feedback must match the confirmed experience contract"
                )
            result = service.feedback(ConfirmedExperience(**request))
        else:
            raise CliInputError("unsupported command")
        _emit(result)
        status = result.get("status") if isinstance(result, Mapping) else None
        if status in {"needs_dependency", "needs_materialization"}:
            return 3
        if status == "failed":
            return 4
        return 0
    except (CliInputError, KnowledgeSupportContractError, FeedbackError) as exc:
        _emit({"status": "failed", "error": "input_error", "message": str(exc)})
        return 2
    except StoreDependencyUnavailable as exc:
        _emit(
            {
                "status": "needs_dependency",
                "error": "lancedb_unavailable",
                "message": str(exc),
            }
        )
        return 3
    except Exception as exc:
        _emit({"status": "failed", "error": "runtime_error", "message": str(exc)})
        return 4


def _service_from_config(config) -> KnowledgeSupportService:
    store = KnowledgeStore(database_path(config))
    embedder = OllamaEmbeddingClient(
        base_url=config.embedding.base_url,
        model=config.embedding.model,
    )
    return KnowledgeSupportService(config, store=store, embedder=embedder)


def _dependency_health(config) -> dict[str, object]:
    dependencies = {
        "lancedb": importlib.util.find_spec("lancedb") is not None,
        "python_docx": importlib.util.find_spec("docx") is not None,
        "pypdf": importlib.util.find_spec("pypdf") is not None,
    }
    result: dict[str, object] = {
        "status": "ready",
        "agent_id": config.agent_id,
        "database": str(database_path(config)),
        "dependencies": dependencies,
        "embedding": {
            "endpoint": config.embedding.base_url,
            "model": config.embedding.model,
            "available": False,
            "fallback": "fts",
        },
        "actions": [],
    }
    if not dependencies["lancedb"]:
        result["status"] = "needs_dependency"
        result["actions"] = ["prepare the workflow's locked Python runtime"]
        return result
    try:
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            response = client.get(f"{config.embedding.base_url}/api/tags")
        response.raise_for_status()
        payload = response.json()
        models = {
            item.get("name")
            for item in payload.get("models", [])
            if isinstance(item, dict)
        }
        model_present = config.embedding.model in models
        if model_present:
            OllamaEmbeddingClient(
                base_url=config.embedding.base_url,
                model=config.embedding.model,
                timeout_seconds=3.0,
            ).embed(["health check"])
        result["embedding"] = {
            "endpoint": config.embedding.base_url,
            "model": config.embedding.model,
            "available": model_present,
            "fallback": "fts",
        }
        if not model_present:
            result["status"] = "ready_degraded"
    except (httpx.HTTPError, ValueError, EmbeddingUnavailable):
        result["status"] = "ready_degraded"
    return result


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliInputError(f"cannot read JSON request: {exc}") from None


def _read_object(path: Path) -> dict[str, object]:
    value = _read_json(path)
    if not isinstance(value, dict):
        raise CliInputError("request must be a JSON object")
    return value


def _jsonable(value):
    if is_dataclass(value):
        return asdict(value)
    return value


def _emit(value: object) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
