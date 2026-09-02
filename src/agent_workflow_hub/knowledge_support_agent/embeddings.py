"""Local-only Ollama embedding client with an explicit degraded failure mode."""

from __future__ import annotations

import ipaddress
from typing import Sequence
from urllib.parse import urlsplit

import httpx


class EmbeddingUnavailable(RuntimeError):
    """Raised when local embedding cannot serve the current request."""


class OllamaEmbeddingClient:
    _MAX_BATCH_SIZE = 64

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        client: httpx.Client | None = None,
        timeout_seconds: float = 15.0,
    ) -> None:
        parsed = urlsplit(base_url)
        try:
            loopback = parsed.hostname == "localhost" or ipaddress.ip_address(
                parsed.hostname or ""
            ).is_loopback
        except ValueError:
            loopback = False
        if parsed.scheme not in {"http", "https"} or not loopback:
            raise ValueError("embedding endpoint must be an HTTP loopback URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("embedding endpoint must not contain credentials")
        if model != "qwen3-embedding:0.6b":
            raise ValueError("unsupported embedding model")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            trust_env=False,
        )
        self._dimension: int | None = None

    @property
    def dimension(self) -> int | None:
        return self._dimension

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts or any(not isinstance(text, str) or not text for text in texts):
            raise EmbeddingUnavailable("embedding input must contain non-empty text")
        vectors: list[tuple[float, ...]] = []
        for start in range(0, len(texts), self._MAX_BATCH_SIZE):
            vectors.extend(
                self._embed_batch(texts[start : start + self._MAX_BATCH_SIZE])
            )
        return tuple(vectors)

    def _embed_batch(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        payload = None
        for attempt in range(2):
            try:
                response = self._client.post(
                    f"{self._base_url}/api/embed",
                    json={"model": self._model, "input": list(texts)},
                )
                response.raise_for_status()
                payload = response.json()
                break
            except httpx.HTTPStatusError as exc:
                if attempt == 0 and exc.response.status_code >= 500:
                    continue
                raise EmbeddingUnavailable(
                    f"local embedding unavailable: {exc}"
                ) from None
            except httpx.TransportError as exc:
                if attempt == 0:
                    continue
                raise EmbeddingUnavailable(
                    f"local embedding unavailable: {exc}"
                ) from None
            except httpx.HTTPError as exc:
                raise EmbeddingUnavailable(
                    f"local embedding unavailable: {exc}"
                ) from None
            except ValueError as exc:
                raise EmbeddingUnavailable(
                    f"local embedding unavailable: {exc}"
                ) from None
        raw = payload.get("embeddings") if isinstance(payload, dict) else None
        if not isinstance(raw, list) or len(raw) != len(texts):
            raise EmbeddingUnavailable("local embedding returned an invalid response")
        vectors: list[tuple[float, ...]] = []
        for item in raw:
            if not isinstance(item, list) or not item:
                raise EmbeddingUnavailable("local embedding returned an invalid vector")
            try:
                vector = tuple(float(value) for value in item)
            except (TypeError, ValueError):
                raise EmbeddingUnavailable("local embedding returned a nonnumeric vector") from None
            if self._dimension is None:
                self._dimension = len(vector)
            if len(vector) != self._dimension:
                raise EmbeddingUnavailable("local embedding dimension changed")
            vectors.append(vector)
        return tuple(vectors)
