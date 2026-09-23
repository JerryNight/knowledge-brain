"""Postgres ``SearchBackend`` — the two retrieval branches (spec §8).

This is a thin mapper: the SQL itself lives in
``kb.retrieval.query_builder`` because that module is the single place allowed to
write a tenant-scoped query. What this class adds is the transaction boundary and
the row→``ChunkCandidate`` translation.

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
from collections.abc import Sequence
from typing import Any

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
        statement = qb.chunk_vector_search(
            user_id, embedding, limit=limit, tags=tags, path_prefix=path_prefix
        )
        return await self._run(statement, user_id, branch=BRANCH_VECTOR)

    async def keyword_search(
        self,
        *,
        user_id: uuid.UUID,
        query: str,
        limit: int,
        tags: Sequence[str] | None = None,
        path_prefix: str | None = None,
    ) -> list[ChunkCandidate]:
        statement = qb.chunk_keyword_search(
            user_id, query, limit=limit, tags=tags, path_prefix=path_prefix
        )
        return await self._run(statement, user_id, branch=BRANCH_KEYWORD)

    async def _run(self, statement, user_id: uuid.UUID, *, branch: str) -> list[ChunkCandidate]:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            rows = (await session.execute(statement)).mappings().all()
        return [_to_candidate(dict(row), branch=branch) for row in rows]


__all__ = ["PostgresSearchBackend"]
