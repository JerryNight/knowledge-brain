"""Retrieval ports.

``SearchBackend`` is the two branches; everything above it (fusion, capping,
snippet shaping, the optional rerank hook) is pure and lives in
``kb.retrieval.service`` and ``kb.retrieval.rrf``. That split is what lets the
ranking behaviour be tested without a database.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from kb.retrieval.types import ChunkCandidate, SearchHit


@runtime_checkable
class SearchBackend(Protocol):
    """The two retrieval branches, both already tenant-filtered."""

    async def vector_search(
        self,
        *,
        user_id: uuid.UUID,
        embedding: Sequence[float],
        limit: int,
        tags: Sequence[str] | None = None,
        path_prefix: str | None = None,
    ) -> list[ChunkCandidate]: ...

    async def keyword_search(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        limit: int,
        tags: Sequence[str] | None = None,
        path_prefix: str | None = None,
    ) -> list[ChunkCandidate]: ...


@runtime_checkable
class Reranker(Protocol):
    """Pipeline-end hook (spec §8).

    Phase 1 does not implement one — a cross-encoder needs a model choice, an
    inference budget and a latency budget, none of which are worth paying yet.
    The interface exists so that adding one is a single registration rather than
    a refactor of the retrieval path.
    """

    async def rerank(self, query: str, hits: Sequence[SearchHit]) -> list[SearchHit]: ...


class NoopReranker:
    """Pass-through reranker. The default."""

    async def rerank(self, query: str, hits: Sequence[SearchHit]) -> list[SearchHit]:
        return list(hits)
