from __future__ import annotations

import json

import httpx
import pytest

from agent_workflow_hub.knowledge_support_agent.embeddings import (
    EmbeddingUnavailable,
    OllamaEmbeddingClient,
)


def client(handler) -> OllamaEmbeddingClient:
    transport = httpx.MockTransport(handler)
    return OllamaEmbeddingClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3-embedding:0.6b",
        client=httpx.Client(transport=transport),
    )


def test_ollama_embed_uses_local_api_without_pull() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2, 0.3]]})

    result = client(handler).embed(["登录失败"])

    assert result == ((0.1, 0.2, 0.3),)
    assert requests[0].url.path == "/api/embed"
    assert requests[0].read().decode("utf-8").count("qwen3-embedding:0.6b") == 1
    assert all("pull" not in request.url.path for request in requests)


@pytest.mark.parametrize("status", (404, 500))
def test_missing_model_or_server_error_is_degradable(status: int) -> None:
    embedder = client(lambda request: httpx.Response(status, json={"error": "missing"}))

    with pytest.raises(EmbeddingUnavailable):
        embedder.embed(["query"])


def test_transport_failure_is_degradable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    with pytest.raises(EmbeddingUnavailable, match="unavailable"):
        client(handler).embed(["query"])


def test_nontransport_http_failure_is_degradable_without_retry() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.DecodingError("invalid response encoding", request=request)

    with pytest.raises(EmbeddingUnavailable, match="unavailable"):
        client(handler).embed(["query"])

    assert attempts == 1


def test_transient_server_error_retries_batch_once() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(500, json={"error": "transient"})
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]]})

    result = client(handler).embed(["query"])

    assert result == ((1.0, 0.0),)
    assert attempts == 2


def test_dimension_drift_is_rejected() -> None:
    responses = iter(
        (
            httpx.Response(200, json={"embeddings": [[1.0, 0.0]]}),
            httpx.Response(200, json={"embeddings": [[1.0, 0.0, 0.0]]}),
        )
    )
    embedder = client(lambda request: next(responses))
    embedder.embed(["first"])

    with pytest.raises(EmbeddingUnavailable, match="dimension"):
        embedder.embed(["second"])


def test_large_embedding_input_is_batched_without_reordering() -> None:
    batch_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        values = json.loads(request.read())["input"]
        batch_sizes.append(len(values))
        return httpx.Response(
            200,
            json={"embeddings": [[float(value)] for value in values]},
        )

    result = client(handler).embed([str(index) for index in range(130)])

    assert batch_sizes == [64, 64, 2]
    assert result[0] == (0.0,)
    assert result[-1] == (129.0,)


def test_non_loopback_embedding_endpoint_is_rejected() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OllamaEmbeddingClient(
            base_url="https://api.example.invalid",
            model="qwen3-embedding:0.6b",
        )


def test_internal_loopback_client_ignores_host_proxy_settings(monkeypatch) -> None:
    import agent_workflow_hub.knowledge_support_agent.embeddings as module

    options: dict[str, object] = {}

    def build_client(**kwargs):
        options.update(kwargs)
        return object()

    monkeypatch.setattr(module.httpx, "Client", build_client)

    OllamaEmbeddingClient(
        base_url="http://127.0.0.1:11434",
        model="qwen3-embedding:0.6b",
    )

    assert options["trust_env"] is False
