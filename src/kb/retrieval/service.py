"""Retrieval service — hybrid search with fusion, capping and a rerank hook.

Shape of a query (spec §8)::

    query ──┬─ embed (cached) ──► vector branch  (HNSW, top 40)
            └─ websearch_to_tsquery ──► keyword branch (GIN, top 40)
                              │
                    RRF fusion (k = RRF_K)
                              │
                  per-document cap (default 3)
                              │
                    wide recall (default 25, max 50)
                              │
                    optional rerank hook

Two deliberate behaviours worth naming:

* **One branch failing is not a failed query.** If the embedding provider is
  rate-limited or unreachable, keyword search still answers, and the result says
  which branch was missing. The alternative — erroring out — would take the
  whole tool offline because of a transient 429.
* **Wide recall is the point.** The caller is an agent with human-level judgement
  and a ``read_note`` tool; returning five chunks would mean a weak ranker making
  a decision the caller is better at (spec §8).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass

from kb.indexer.embedding import EmbeddingError, QueryEmbedder
from kb.retrieval.ports import NoopReranker, Reranker, SearchBackend
from kb.retrieval.rrf import DEFAULT_RRF_K, cap_per_document, reciprocal_rank_fusion
from kb.retrieval.types import (
    BRANCH_KEYWORD,
    BRANCH_VECTOR,
    ChunkCandidate,
    SearchQuery,
    SearchResult,
    hits_from_candidates,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_LIMIT = 25
MAX_LIMIT = 50
DEFAULT_PER_DOCUMENT_LIMIT = 3
DEFAULT_BRANCH_LIMIT = 40


@dataclass(frozen=True, slots=True)
class RetrievalSettings:
    """Tunables, all of which the evaluation set is the only honest way to change."""

    default_limit: int = DEFAULT_LIMIT
    max_limit: int = MAX_LIMIT
    per_document_limit: int = DEFAULT_PER_DOCUMENT_LIMIT
    branch_limit: int = DEFAULT_BRANCH_LIMIT
    rrf_k: int = DEFAULT_RRF_K

    @classmethod
    def from_settings(cls) -> RetrievalSettings:
        from kb.config import get_settings

        settings = get_settings()
        return cls(
            default_limit=settings.retrieval_default_limit,
            max_limit=settings.retrieval_max_limit,
            per_document_limit=settings.per_document_chunk_limit,
            rrf_k=settings.rrf_k,
        )


class RetrievalService:
    def __init__(
        self,
        *,
        backend: SearchBackend,
        query_embedder: QueryEmbedder | None = None,
        settings: RetrievalSettings | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self._backend = backend
        self._query_embedder = query_embedder
        self._settings = settings or RetrievalSettings()
        self._reranker = reranker or NoopReranker()

    async def search(self, query: SearchQuery, *, user_id: uuid.UUID) -> SearchResult:
        text = query.query.strip()
        if not text:
            return SearchResult(query=query.query)

        limit = _clamp(query.limit, self._settings.default_limit, self._settings.max_limit)
        tags = list(query.tags) or None

        branches: dict[str, list[ChunkCandidate]] = {BRANCH_KEYWORD: [], BRANCH_VECTOR: []}
        degraded: list[str] = []

        tasks: dict[str, object] = {
            BRANCH_KEYWORD: self._backend.keyword_search(
                user_id=user_id,
                query=text,
                limit=self._settings.branch_limit,
                tags=tags,
                path_prefix=query.path_prefix,
            )
        }
        if self._query_embedder is not None:
            tasks[BRANCH_VECTOR] = self._vector_candidates(
                user_id=user_id,
                text=text,
                limit=self._settings.branch_limit,
                tags=tags,
                path_prefix=query.path_prefix,
            )

        for name, outcome in zip(tasks.keys(), await asyncio.gather(*tasks.values(), return_exceptions=True), strict=False):
            if isinstance(outcome, BaseException):
                LOGGER.warning("retrieval branch %s failed: %s", name, outcome)
                degraded.append(name)
                continue
            branches[name] = outcome

        vector_hits = branches[BRANCH_VECTOR]
        keyword_hits = branches[BRANCH_KEYWORD]

        by_id: dict[int, ChunkCandidate] = {}
        for candidate in (*vector_hits, *keyword_hits):
            by_id.setdefault(candidate.chunk_id, candidate)

        fused = reciprocal_rank_fusion(
            [[candidate.chunk_id for candidate in vector_hits], [candidate.chunk_id for candidate in keyword_hits]],
            k=self._settings.rrf_k,
        )
        hits = hits_from_candidates(fused, by_id)
        hits = cap_per_document(
            hits,
            per_document_limit=self._settings.per_document_limit,
            limit=limit,
            document_of=lambda hit: hit.document_id,
        )
        if self._reranker is not None:
            hits = await self._reranker.rerank(text, hits)

        return SearchResult(
            query=query.query,
            hits=tuple(hits),
            branches={name: len(candidates) for name, candidates in branches.items()},
            degraded=tuple(degraded),
        )

    async def _vector_candidates(
        self,
        *,
        user_id: uuid.UUID,
        text: str,
        limit: int,
        tags,
        path_prefix,
    ) -> list[ChunkCandidate]:
        """Embed the query (through the LRU cache) and run the vector branch."""
        assert self._query_embedder is not None
        try:
            embedding = await self._query_embedder.embed_query(text)
        except EmbeddingError as exc:
            raise EmbeddingError(f"query embedding failed: {exc}") from exc
        return await self._backend.vector_search(
            user_id=user_id,
            embedding=embedding,
            limit=limit,
            tags=tags,
            path_prefix=path_prefix,
        )


def _clamp(value: int, default: int, maximum: int) -> int:
    """Apply the wide-recall window: default 25, never more than 50 (spec §8)."""
    if value is None or value <= 0:
        return default
    return min(value, maximum)


__all__ = ["RetrievalService", "RetrievalSettings"]
