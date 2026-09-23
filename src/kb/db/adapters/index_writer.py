"""Postgres ``IndexWriter`` — the writes one indexing run performs (spec §8).

Three operations, each in its own tenant-bound transaction. They are idempotent
by construction, which is what lets a crash between them cost a retry rather than
correctness:

* ``replace_chunks`` deletes then re-inserts, so a half-written document cannot
  survive as a mixture of old and new chunks;
* ``update_document`` records the outcome last;
* ``existing_chunks`` is the read behind the level-3 short circuit.

Per-call transactions rather than one long-lived session: a document with
thousands of chunks should not hold a transaction open across an embedding call.
The cost is that ``existing_chunks`` and ``replace_chunks`` are not atomic with
respect to each other, which is safe here because jobs are claimed one per
document and the unique constraint ``(document_id, ordinal)`` makes a duplicate
insert fail loudly instead of silently duplicating rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.db.tenant import tenant_transaction
from kb.indexer.ports import ChunkWrite, DocumentUpdate, ExistingChunk
from kb.retrieval import query_builder as qb


class PostgresIndexWriter:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def existing_chunks(
        self, *, user_id: uuid.UUID, document_id: uuid.UUID
    ) -> dict[int, ExistingChunk]:
        """What is already stored, keyed by ordinal.

        The embedding is loaded alongside the text because the level-3 decision
        needs both: identical text reuses the vector, and a chunk that *should*
        have a vector but does not must be re-embedded even though its text
        matches (see ``kb.indexer.service``).
        """
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            rows = (await session.execute(qb.chunks_for_document(user_id, document_id))).scalars().all()
        return {
            row.ordinal: ExistingChunk(
                ordinal=row.ordinal,
                text=row.text,
                embedding=list(row.embedding) if row.embedding is not None else None,
            )
            for row in rows
        }

    async def replace_chunks(
        self, *, user_id: uuid.UUID, document_id: uuid.UUID, chunks: Sequence[ChunkWrite]
    ) -> int:
        """Replace every chunk of one document. Returns the number written."""
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            await session.execute(qb.delete_chunks_for_document(user_id, document_id))
            if chunks:
                await session.execute(
                    qb.insert_chunks(
                        user_id,
                        [
                            {
                                "document_id": document_id,
                                "ordinal": chunk.ordinal,
                                "text": chunk.text,
                                "heading_path": list(chunk.heading_path) or None,
                                "locator": chunk.locator,
                                "token_count": chunk.token_count,
                                "embedding": chunk.embedding,
                            }
                            for chunk in chunks
                        ],
                    )
                )
        return len(chunks)

    async def update_document(
        self, *, user_id: uuid.UUID, document_id: uuid.UUID, update: DocumentUpdate
    ) -> None:
        """Record the document-level outcome.

        ``conversion_status`` and ``conversion_error`` are always written as a
        pair. That matters for the success path, where the indexer passes
        ``conversion_error=None`` to mean "clear the error from the previous
        failed attempt" — treating ``None`` as "leave alone" would strand a stale
        error message on a document that is now perfectly fine.

        The remaining fields are written only when present: the failure path
        supplies no ``converted_sha`` or ``title``, and it should not blank the
        metadata of a document whose content did not change.
        """
        values: dict = {}
        if update.conversion_status is not None:
            values["conversion_status"] = update.conversion_status
            values["conversion_error"] = update.conversion_error
        for key, value in {
            "converted_sha": update.converted_sha,
            "title": update.title,
            "tags": list(update.tags) if update.tags is not None else None,
            "indexed_at": update.indexed_at,
        }.items():
            if value is not None:
                values[key] = value
        if not values:
            return
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            await session.execute(qb.update_document_conversion(user_id, document_id, values=values))


__all__ = ["PostgresIndexWriter"]
