"""Unit tests for the embedding provider, retry schedule, and query cache.

The provider is exercised against ``httpx.MockTransport``, so batching order,
429 handling and dimension checks are all verifiable without a network or an API
key. Sleeps are injected, so the retry path runs instantly.
"""

from __future__ import annotations

import json

import httpx
import pytest

from kb.indexer.embedding import (
    EmbeddingError,
    OpenAICompatibleEmbedder,
    QueryEmbedder,
    QueryEmbeddingCache,
)

DIM = 4


def vector(seed: int) -> list[float]:
    return [float(seed)] * DIM


def make_embedder(handler, **kwargs) -> OpenAICompatibleEmbedder:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    params = {
        "api_base": "https://example.test/v1",
        "api_key": "secret",
        "model": "test-model",
        "dim": DIM,
        "batch_size": 2,
        "sleep": _no_sleep,
    }
    params.update(kwargs)
    return OpenAICompatibleEmbedder(client=client, **params)


async def _no_sleep(seconds: float) -> None:
    return None


def ok_response(vectors: list[list[float]]) -> httpx.Response:
    return httpx.Response(200, json={"data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)]})


def echo_handler(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    return ok_response([vector(len(text)) for text in payload["input"]])


# ---------------------------------------------------------------------------
# batching and ordering
# ---------------------------------------------------------------------------


async def test_empty_input_makes_no_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return ok_response([])

    embedder = make_embedder(handler)
    assert await embedder.embed([]) == []
    assert calls == []


async def test_batches_are_sized_and_results_keep_input_order() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        payload = json.loads(request.content)
        return ok_response([vector(len(text)) for text in payload["input"]])

    embedder = make_embedder(handler, batch_size=2)
    vectors = await embedder.embed(["aa", "bbb", "c", "dddd", "e"])

    assert request_count == 3  # 2 + 2 + 1
    # Positional identity with the input is what lets the indexer map vectors
    # back onto chunks; a reordered response would silently mis-attach them.
    assert vectors == [vector(2), vector(3), vector(1), vector(4), vector(1)]


async def test_out_of_order_api_response_is_reassembled_by_index() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": vector(2)},
                    {"index": 0, "embedding": vector(1)},
                ]
            },
        )

    embedder = make_embedder(handler, batch_size=2)
    assert await embedder.embed(["one", "two"]) == [vector(1), vector(2)]


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------


async def test_429_is_retried_and_succeeds() -> None:
    """Rate limiting is normal traffic, not a fatal error (spec §8)."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, text="slow down")
        return ok_response([vector(1)])

    embedder = make_embedder(handler)
    assert await embedder.embed(["hi"]) == [vector(1)]
    assert attempts == 2


async def test_server_errors_are_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="unavailable") if attempts < 3 else ok_response([vector(1)])

    embedder = make_embedder(handler)
    assert await embedder.embed(["hi"]) == [vector(1)]
    assert attempts == 3


async def test_persistent_rate_limiting_gives_up_with_a_clear_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="nope")

    embedder = make_embedder(handler, max_retries=3)
    with pytest.raises(EmbeddingError, match="after 3 attempts"):
        await embedder.embed(["hi"])


async def test_client_errors_are_not_retried() -> None:
    """A 400 means the request is wrong; retrying it just wastes time."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(400, text="bad input")

    embedder = make_embedder(handler)
    with pytest.raises(EmbeddingError, match="400"):
        await embedder.embed(["hi"])
    assert attempts == 1


async def test_transport_failures_are_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection dropped")
        return ok_response([vector(1)])

    embedder = make_embedder(handler)
    assert await embedder.embed(["hi"]) == [vector(1)]


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


async def test_dimension_mismatch_is_rejected() -> None:
    """A silently wrong dimension would corrupt the vector column."""

    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response([[0.0, 1.0]])

    embedder = make_embedder(handler)
    with pytest.raises(EmbeddingError, match="dimension"):
        await embedder.embed(["hi"])


async def test_missing_vectors_are_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return ok_response([])

    embedder = make_embedder(handler)
    with pytest.raises(EmbeddingError, match="expected 1"):
        await embedder.embed(["hi"])


# ---------------------------------------------------------------------------
# query cache
# ---------------------------------------------------------------------------


def test_cache_returns_nothing_on_a_miss() -> None:
    cache = QueryEmbeddingCache()
    assert cache.get("怎么防止任务重复执行") is None
    assert cache.misses == 1


def test_cache_round_trips_a_vector() -> None:
    cache = QueryEmbeddingCache()
    cache.put("q", [1.0, 2.0])
    assert cache.get("q") == [1.0, 2.0]
    assert cache.get("other") is None
    assert cache.hit_rate == 0.5


def test_cache_evicts_the_least_recently_used_entry() -> None:
    cache = QueryEmbeddingCache(max_entries=2)
    cache.put("a", [1.0])
    cache.put("b", [2.0])
    cache.get("a")  # 'a' becomes the most recent
    cache.put("c", [3.0])

    assert cache.get("b") is None
    assert cache.get("a") == [1.0]
    assert cache.get("c") == [3.0]


def test_cache_key_is_a_stable_md5_of_the_query() -> None:
    assert QueryEmbeddingCache.key("q") == QueryEmbeddingCache.key("q")
    assert len(QueryEmbeddingCache.key("q")) == 32


async def test_query_embedder_serves_repeats_from_the_cache() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return ok_response([vector(1)])

    provider = make_embedder(handler)
    embedder = QueryEmbedder(provider=provider, cache=QueryEmbeddingCache())

    await embedder.embed_query("重复的问题")
    await embedder.embed_query("重复的问题")

    assert calls == 1
