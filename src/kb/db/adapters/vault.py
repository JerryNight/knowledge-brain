"""Postgres ``VaultStore`` and ``UnitOfWork`` (spec §4 / §5 ③).

``VaultStore`` is what the sync pipeline and the upload API use to read and write
``documents``. The two configurations differ in transaction scope, and that
difference is the whole point of having both:

* ``PostgresVaultStore`` — **one transaction per call**. The sync pipeline writes
  documents as it walks a diff; batching those into one transaction would hold a
  lock for the length of a full sync.
* ``PostgresUnitOfWork`` — **one transaction per upload**, with ``enqueue``
  joining it. This is the guarantee spec §4 singles out: the document row and its
  ``doc_index`` job commit together, so a queued job never points at a document
  that was rolled back.

Every transaction is tenant-bound via ``set_tenant`` before the first query, so
the RLS policy has something to match on. The query builder applies its own
``user_id`` predicate as well — the two layers are independent on purpose.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.db.tenant import tenant_transaction
from kb.models import Document
from kb.queue.base import JobSpec
from kb.queue.postgres import PostgresJobQueue
from kb.retrieval import query_builder as qb
from kb.sync.ports import (
    DocumentRecord,
    NewDocument,
)


def to_record(row: Document) -> DocumentRecord:
    return DocumentRecord(
        id=row.id,
        user_id=row.user_id,
        source=row.source,
        source_path=row.source_path,
        content_sha=row.content_sha,
        converted_sha=row.converted_sha,
        conversion_status=row.conversion_status,
        size_bytes=row.size_bytes,
        indexed_at=row.indexed_at,
    )


class _VaultOps:
    """The ``VaultStore`` SQL, over a caller-supplied session.

    Split out so the two transaction scopes below share one implementation and
    differ only in which session they hand over. The session is expected to
    already be inside a transaction with ``app.user_id`` set.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_document(self, *, user_id: uuid.UUID, source: str, source_path: str) -> DocumentRecord | None:
        result = await self._session.execute(qb.document_by_path(user_id, source, source_path))
        row = result.scalars().one_or_none()
        return to_record(row) if row is not None else None

    async def list_document_paths(self, *, user_id: uuid.UUID, source: str) -> list[str]:
        result = await self._session.execute(qb.document_paths(user_id, source))
        return [str(path) for path in result.scalars().all()]

    async def upsert_document(self, document: NewDocument) -> DocumentRecord:
        await self._session.execute(
            qb.upsert_document(
                document.user_id,
                source=document.source,
                source_path=document.source_path,
                content_sha=document.content_sha,
                mime=document.mime,
                size_bytes=document.size_bytes,
                title=document.title,
                tags=document.tags,
            )
        )
        record = await self.find_document(
            user_id=document.user_id, source=document.source, source_path=document.source_path
        )
        assert record is not None, "upsert_document did not produce a row"
        return record

    async def delete_document(self, *, user_id: uuid.UUID, source: str, source_path: str) -> bool:
        result = await self._session.execute(qb.delete_document_by_path(user_id, source, source_path))
        return result.rowcount > 0

    async def rename_document(self, *, user_id: uuid.UUID, source: str, old_path: str, new_path: str) -> bool:
        """Re-path a document without touching its content.

        Returns ``False`` when there is nothing at ``old_path`` — a rename whose
        source row is already gone is indistinguishable from a touch, and the
        caller counts it as "skipped" rather than an error.
        """
        existing = await self.find_document(user_id=user_id, source=source, source_path=old_path)
        if existing is None:
            return False
        await self._session.execute(qb.update_document_path(user_id, existing.id, new_path))
        return True

    async def set_last_synced_sha(self, *, user_id: uuid.UUID, repo_id: uuid.UUID, sha: str) -> None:
        """Advance the diff starting point (spec §6 — only after every batch commits)."""
        await self._session.execute(qb.update_repo_synced_sha(user_id, repo_id, sha))


class _Scope(_VaultOps):
    """A ``UnitOfWorkScope``: vault writes plus an in-transaction ``enqueue``."""

    def __init__(self, session: AsyncSession, user_id: uuid.UUID, queue: PostgresJobQueue) -> None:
        super().__init__(session)
        self._user_id = user_id
        self._queue = queue

    async def enqueue(self, spec: JobSpec) -> int:
        """Enqueue inside the caller's transaction (spec §4)."""
        return await self._queue.enqueue(spec, session=self._session)


class PostgresVaultStore:
    """``VaultStore`` where each call is its own tenant-bound transaction."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def find_document(self, *, user_id: uuid.UUID, source: str, source_path: str) -> DocumentRecord | None:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            return await _VaultOps(session).find_document(user_id=user_id, source=source, source_path=source_path)

    async def list_document_paths(self, *, user_id: uuid.UUID, source: str) -> list[str]:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            return await _VaultOps(session).list_document_paths(user_id=user_id, source=source)

    async def upsert_document(self, document: NewDocument) -> DocumentRecord:
        async with tenant_transaction(self._sessionmaker, document.user_id) as session:
            return await _VaultOps(session).upsert_document(document)

    async def delete_document(self, *, user_id: uuid.UUID, source: str, source_path: str) -> bool:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            return await _VaultOps(session).delete_document(user_id=user_id, source=source, source_path=source_path)

    async def rename_document(self, *, user_id: uuid.UUID, source: str, old_path: str, new_path: str) -> bool:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            return await _VaultOps(session).rename_document(
                user_id=user_id, source=source, old_path=old_path, new_path=new_path
            )

    async def set_last_synced_sha(self, *, user_id: uuid.UUID, repo_id: uuid.UUID, sha: str) -> None:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            await _VaultOps(session).set_last_synced_sha(user_id=user_id, repo_id=repo_id, sha=sha)


class PostgresUnitOfWork:
    """Opens one tenant-bound transaction that also carries the queue insert."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], queue: PostgresJobQueue) -> None:
        self._sessionmaker = sessionmaker
        self._queue = queue

    @asynccontextmanager
    async def begin(self, user_id: uuid.UUID) -> AsyncIterator[_Scope]:
        """``async with uow.begin(user_id) as scope: ...``

        The decorator makes this return an async context manager, which is what
        ``kb.sync.ports.UnitOfWork`` declares. Everything inside the ``async
        with`` runs in one transaction with ``app.user_id`` already set.
        """
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            yield _Scope(session, user_id, self._queue)


__all__ = ["PostgresUnitOfWork", "PostgresVaultStore", "to_record"]
