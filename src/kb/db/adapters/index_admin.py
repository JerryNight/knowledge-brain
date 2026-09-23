"""Index maintenance — the operations behind ``POST /api/admin/rebuild``.

Spec §5's founding property is *the index is a derivative*: every chunk is
recomputable from a git repository plus the stored uploads. This module is where
that property is actually exercised, and the shape of the operations follows from
it directly.

**Rebuild deletes chunks, not documents.** ``documents`` rows carry the
``source_path`` and ``content_sha`` needed to re-derive everything, and
``conversion_cache`` means a PDF is not parsed a second time. Throwing the rows
away would work too, but it would turn a cheap rebuild into a full re-walk of
every repository, and it would lose the ``failed`` / ``no_text`` record of files
that cannot be indexed at all — the only evidence those files exist.

**``last_synced_sha`` is deliberately left alone.** A rebuild is about the
*index*, not about the *diff*. The tree has not changed, so resetting the sync
position would only buy a redundant full comparison. It also means this path
touches nothing on the ``last_synced_sha`` red line (spec §11.1 #2).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.db.adapters.vault import to_record
from kb.db.tenant import tenant_transaction
from kb.retrieval import query_builder as qb
from kb.sync.ports import DocumentRecord


class PostgresIndexMaintenance:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def drop_chunks(self, user_id: uuid.UUID) -> int:
        """Delete every chunk of one tenant. Returns the number removed."""
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            before = int((await session.execute(qb.chunk_count(user_id))).scalar_one())
            await session.execute(qb.delete_all_chunks(user_id))
        return before

    async def count_chunks(self, user_id: uuid.UUID) -> int:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            return int((await session.execute(qb.chunk_count(user_id))).scalar_one())

    async def document_records(self, user_id: uuid.UUID) -> list[DocumentRecord]:
        """Every document of one tenant, as the payloads a rebuild needs.

        Ordered by ``source`` then path so a rebuild's job list is deterministic:
        two rebuilds of an identical vault enqueue in the same order, which makes
        the resulting chunk layout reproducible.
        """
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            rows = (await session.execute(qb.documents_for_source(user_id))).scalars().all()
        return [to_record(row) for row in rows]

    async def clear_index_timestamps(self, user_id: uuid.UUID) -> int:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            result = await session.execute(qb.clear_indexed_at(user_id))
        return result.rowcount


__all__ = ["PostgresIndexMaintenance"]
