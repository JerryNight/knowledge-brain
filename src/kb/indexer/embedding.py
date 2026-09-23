"""Embedding — provider interface, batching, rate-limit handling, query cache.

Spec §8's requirements, and the reasoning behind each:

* ``embed(texts) -> list[vector]`` is the whole interface. Providers are
  pluggable so the cloud API can be replaced without touching the indexer.
* Batching (64~128) and a concurrency ceiling keep a large rebuild from opening
  hundreds of connections.
* **429 is not an error condition.** Rate limiting is the normal state of a
  cloud embedding API under load, so retries are part of the provider rather
  than something each caller improvises.
* Only *query* vectors are cached. Chunk vectors are computed once by
  construction, so caching them would only consume memory (spec §8).
"""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_MAX_RETRIES = 5
RETRY_BASE_SECONDS = 0.5
RETRY_CAP_SECONDS = 30.0


class EmbeddingError(RuntimeError):
    """The provider could not produce vectors after exhausting retries."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Anything that turns text into vectors."""

    model: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class OpenAICompatibleEmbedder:
    """``POST {api_base}/embeddings`` — the OpenAI wire format, which most
    vendors copy. One implementation covers OpenAI, most Chinese providers, and
    a local server behind the same shape.

    ``sleep`` is injectable so the retry schedule can be tested without waiting.
    """

    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model: str,
        dim: int,
        batch_size: int = 64,
        max_concurrency: int = 8,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.model = model
        self.dim = dim
        self._api_base = api_base.rstrip("/")
        self._api_key = api_key
        self._batch_size = max(1, batch_size)
        self._max_concurrency = max(1, max_concurrency)
        self._max_retries = max_retries
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep

    @classmethod
    def from_settings(cls, **overrides: Any) -> OpenAICompatibleEmbedder:
        from kb.config import get_settings

        settings = get_settings()
        params: dict[str, Any] = {
            "api_base": settings.embedding_api_base,
            "api_key": settings.embedding_api_key,
            "model": settings.embedding_model,
            "dim": settings.embedding_dim,
            "batch_size": settings.embedding_batch_size,
            "max_concurrency": settings.embedding_max_concurrency,
        }
        params.update(overrides)
        if not params["api_key"]:
            raise EmbeddingError("EMBEDDING_API_KEY is not configured")
        return cls(**params)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed every text, in order.

        Batches run concurrently up to ``max_concurrency``, but results are
        reassembled by batch index so callers can rely on positional identity
        with the input — which is what lets the indexer map vectors back to
        chunks without carrying ids through the HTTP layer.
        """
        items = list(texts)
        if not items:
            return []

        batches = [items[start : start + self._batch_size] for start in range(0, len(items), self._batch_size)]
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def run(batch: list[str]) -> list[list[float]]:
            async with semaphore:
                return await self._embed_batch(batch)

        results = await asyncio.gather(*(run(batch) for batch in batches))
        return [vector for batch_result in results for vector in batch_result]

    async def embed_one(self, text: str) -> list[float]:
        vectors = await self.embed([text])
        return vectors[0]

    # -- internals ----------------------------------------------------------

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        # `dimensions` is declared rather than left to the provider's default.
        # Measured on DashScope: the default is 1024 whatever the model, and an
        # unsupported value is *silently coerced* — asking
        # qwen3.7-text-embedding-flash for 1536 answers HTTP 200 with 1024
        # floats. Sending it makes the wire agree with `self.dim`; `_parse`
        # below is the backstop that turns any remaining mismatch into an error
        # instead of a wrongly-shaped vector in the index.
        payload: dict[str, Any] = {"model": self.model, "input": batch, "dimensions": self.dim}
        last_error = "unknown error"

        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._post(payload)
            except httpx.HTTPError as exc:
                # A dropped connection is the same problem as a 503: retry it.
                last_error = f"transport: {exc}"
                if attempt == self._max_retries:
                    break
                await self._sleep(_jittered_backoff(attempt))
                continue

            if response.status_code == 200:
                return self._parse(response.json(), expected=len(batch))

            body = response.text[:300]
            if response.status_code not in RETRYABLE_STATUS:
                raise EmbeddingError(f"embedding request failed with {response.status_code}: {body}")

            last_error = f"{response.status_code}: {body}"
            if attempt == self._max_retries:
                break
            await self._sleep(self._retry_delay(attempt, response))

        raise EmbeddingError(f"embedding request failed after {self._max_retries} attempts ({last_error})")

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        return await self._ensure_client().post(
            f"{self._api_base}/embeddings",
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    def _retry_delay(self, attempt: int, response: httpx.Response) -> float:
        """Prefer the server's own ``Retry-After``; otherwise exponential backoff
        with jitter, so a fleet of workers does not retry in lockstep."""
        header = response.headers.get("retry-after")
        if header:
            try:
                return min(RETRY_CAP_SECONDS, float(header))
            except ValueError:
                pass
        return _jittered_backoff(attempt)

    def _parse(self, body: dict[str, Any], *, expected: int) -> list[list[float]]:
        data = body.get("data")
        if not isinstance(data, list) or len(data) != expected:
            raise EmbeddingError(
                f"expected {expected} embeddings, got {len(data) if isinstance(data, list) else 'none'}"
            )

        # The API does not promise an order; `index` is the contract. Ignoring it
        # would attach the wrong vector to the wrong chunk, which is silent and
        # corrupting.
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors = [list(item["embedding"]) for item in ordered]
        for vector in vectors:
            if len(vector) != self.dim:
                raise EmbeddingError(
                    f"provider returned dimension {len(vector)}, index expects {self.dim}. "
                    "Either the configured model cannot produce EMBEDDING_DIM (providers "
                    "generally fall back to their own default instead of erroring), or the "
                    "database schema was built at a different dimension than the settings say. "
                    "Fix the configuration, then run a full rebuild — never append."
                )
        return vectors


def _jittered_backoff(attempt: int, *, base: float = RETRY_BASE_SECONDS, cap: float = RETRY_CAP_SECONDS) -> float:
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(ceiling / 2, ceiling)


class QueryEmbeddingCache:
    """In-process LRU keyed by ``md5(query)`` (spec §8).

    The highest-return optimisation in the project: a remote embedding round trip
    is hundreds of milliseconds and personal query repetition is high, while the
    memory cost is a few hundred vectors. Only queries go in here.
    """

    def __init__(self, max_entries: int = 1024) -> None:
        self._entries: OrderedDict[str, list[float]] = OrderedDict()
        self._max_entries = max(1, max_entries)
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 — cache key, not a security primitive

    def get(self, text: str) -> list[float] | None:
        key = self.key(text)
        vector = self._entries.get(key)
        if vector is None:
            self.misses += 1
            return None
        self.hits += 1
        self._entries.move_to_end(key)
        return vector

    def put(self, text: str, vector: list[float]) -> None:
        key = self.key(text)
        self._entries[key] = vector
        self._entries.move_to_end(key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


@dataclass(slots=True)
class QueryEmbedder:
    """Embeds queries only, through the LRU cache."""

    provider: EmbeddingProvider
    cache: QueryEmbeddingCache

    async def embed_query(self, query: str) -> list[float]:
        cached = self.cache.get(query)
        if cached is not None:
            return cached
        vector = await self.provider.embed_one(query)
        self.cache.put(query, vector)
        return vector
