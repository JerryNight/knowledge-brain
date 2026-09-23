"""Postgres ``SearchBackend`` — the two retrieval branches (spec §8).

This is a thin mapper: the SQL itself lives in
``kb.retrieval.query_builder`` because that module is the single place allowed to
write a tenant-scoped query. What this class adds is the transaction boundary,
the metadata-filter resolution, and the row→``ChunkCandidate`` translation.

Both branches return ``ChunkCandidate`` objects with a ``branch`` tag, which is
what lets the retrieval service fuse two ranked lists whose scores are on
incomparable scales (cosine distance vs ``ts_rank``) without ever normalising
them — RRF only reads positions.

The vector branch's ``score`` is deliberately left as raw cosine distance
(smaller is closer). Nothing downstream compares scores across branches, and
converting to a similarity would invite exactly that mistake.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.db.tenant import tenant_transaction
from kb.retrieval import query_builder as qb
from kb.retrieval.types import BRANCH_KEYWORD, BRANCH_VECTOR, ChunkCandidate


def _to_candidate(row: dict[str, Any], *, branch: str) -> ChunkCandidate:
    heading = row.get("heading_path")
    return ChunkCandidate(
        chunk_id=int(row["chunk_id"]),
        document_id=row["document_id"],
        text=row["text"],
        source=row["source"],
        source_path=row["source_path"],
        title=row.get("title"),
        heading_path=tuple(heading) if heading else (),
        locator=row.get("locator"),
        score=float(row["score"]) if row.get("score") is not None else 0.0,
        branch=branch,
    )


class PostgresSearchBackend:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def vector_search(
        self,
        *,
        user_id: uuid.UUID,
        embedding: Sequence[float],
        limit: int,
        tags: Sequence[str] | None = None,
        path_prefix: str | None = None,
    ) -> list[ChunkCandidate]:
        def build(document_ids: list[uuid.UUID] | None) -> Select:
            return qb.chunk_vector_search(user_id, embedding, limit=limit, document_ids=document_ids)

        return await self._candidates(
            user_id, build=build, branch=BRANCH_VECTOR, tags=tags, path_prefix=path_prefix
        )

    async def keyword_search(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        limit: int,
        tags: Sequence[str] | None = None,
        path_prefix: str | None = None,
    ) -> list[ChunkCandidate]:
        def build(document_ids: list[uuid.UUID] | None) -> Select:
            return qb.chunk_keyword_search(user_id, query, limit=limit, document_ids=document_ids)

        return await self._candidates(
            user_id, build=build, branch=BRANCH_KEYWORD, tags=tags, path_prefix=path_prefix
        )

    async def _candidates(
        self,
        user_id: uuid.UUID,
        *,
        build: Callable[[list[uuid.UUID] | None], Select],
        branch: str,
        tags: Sequence[str] | None,
        path_prefix: str | None,
    ) -> list[ChunkCandidate]:
        """Resolve the metadata filters, then run one branch.

        Both statements share one transaction, so the allow-list and the
        candidates are always computed against the same snapshot.

        Resolving the filter first is what keeps the index: the metadata lives
        on ``documents``, and joining to it in the scoring stage defeats HNSW.
        No filter means no second round trip.
        """
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            document_ids: list[uuid.UUID] | None = None
            if tags or path_prefix:
                resolved = await session.execute(
                    qb.documents_matching_filters(user_id, tags=tags, path_prefix=path_prefix)
                )
                document_ids = list(resolved.scalars().all())
                if not document_ids:
                    # Nothing carries these tags; asking the branch would be a
                    # full index walk for a guaranteed-empty answer.
                    return []
            rows = (await session.execute(build(document_ids))).mappings().all()
        return [_to_candidate(dict(row), branch=branch) for row in rows]


__all__ = ["PostgresSearchBackend"]
