"""Document reading for the MCP tools — ``list_notes`` and ``read_note`` (spec §9).

Both tools are read-only and both must answer a question the retrieval path
cannot: *what exists* and *what does this file actually say*.

**How full text is reassembled.** ``documents`` stores no converted text — only
chunks do. ``read_note`` therefore concatenates the document's chunks in
``ordinal`` order. For markdown that is the note minus its frontmatter; for a
converted attachment it is the converter's output. That is exactly the tradeoff
spec §7 约束 4 accepts, and the response says which of the two the caller is
holding, because a model that believes it is reading the original layout of a
PDF will draw the wrong conclusions from scrambled tables.

Full text is fetched one chunk at a time only to be reassembled, so the read is
bounded by the same token ceiling the chunker enforces per chunk, not by the file
size. A caller that wants less should use ``search_notes``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.db.tenant import tenant_transaction
from kb.retrieval import query_builder as qb

# Suffixes whose stored chunks are the note itself rather than a conversion of it.
NATIVE_MARKDOWN_SUFFIXES = (".md", ".markdown")


@dataclass(frozen=True, slots=True)
class DocumentSummary:
    id: uuid.UUID
    source: str
    source_path: str
    title: str | None
    conversion_status: str
    conversion_error: str | None
    size_bytes: int | None
    tags: tuple[str, ...] = ()

    @property
    def converted(self) -> bool:
        """Whether the text on offer is a conversion product (spec §9)."""
        lowered = self.source_path.lower()
        return not lowered.endswith(NATIVE_MARKDOWN_SUFFIXES)

    def as_dict(self) -> dict:
        return {
            "path": self.source_path,
            "title": self.title,
            "source": self.source,
            "conversion_status": self.conversion_status,
            "size_bytes": self.size_bytes,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class DocumentContent:
    document: DocumentSummary
    text: str
    chunks: int

    def as_dict(self) -> dict:
        payload = dict(self.document.as_dict())
        payload["chunks"] = self.chunks
        payload["text"] = self.text
        if self.document.converted:
            # Spelled out rather than implied: a model reading a scrambled table
            # needs to know it is looking at a conversion, not the original.
            payload["note"] = (
                f"这段文本是 `{self.document.source_path}` 经服务端自动转换后的产物"
                f"（conversion_status={self.document.conversion_status}），"
                "排版与原件不完全一致，表格、公式、双栏可能错乱。"
            )
        if self.document.conversion_error:
            payload["conversion_error"] = self.document.conversion_error
        return payload


class PostgresDocumentReader:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def browse(self, *, user_id: uuid.UUID, prefix: str | None = None, limit: int = 100) -> list[DocumentSummary]:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            rows = (
                await session.execute(qb.browse_documents(user_id, prefix=prefix, limit=limit))
            ).scalars().all()
        return [_to_summary(row) for row in rows]

    async def find(self, *, user_id: uuid.UUID, path: str) -> DocumentSummary | None:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            row = (await session.execute(qb.documents_by_path(user_id, path))).scalars().first()
        return _to_summary(row) if row is not None else None

    async def by_status(
        self, *, user_id: uuid.UUID, statuses: tuple[str, ...] = ("failed", "no_text"), limit: int = 200
    ) -> list[DocumentSummary]:
        """Documents in given conversion states — the ``failed``/``no_text`` report.

        Spec §7: these are the files that are *in* the knowledge base but not
        searchable, and they need to be visible. A silently dropped attachment is
        indistinguishable from one that was never uploaded, and only this listing
        tells the two apart.
        """
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            rows = (
                await session.execute(qb.documents_by_status(user_id, list(statuses)))
            ).scalars().all()
        return [_to_summary(row) for row in rows[:limit]]

    async def all_documents(self, *, user_id: uuid.UUID) -> list[DocumentSummary]:
        """Every document of one tenant, both channels. The rebuild work list."""
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            rows = (await session.execute(qb.documents_for_source(user_id))).scalars().all()
        return [_to_summary(row) for row in rows]

    async def full_text(self, *, user_id: uuid.UUID, document_id: uuid.UUID) -> tuple[str, int]:
        """Reassembled text plus the number of chunks it came from."""
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            rows = (await session.execute(qb.chunks_for_document(user_id, document_id))).scalars().all()
        return "\n\n".join(row.text for row in rows), len(rows)

    async def read(self, *, user_id: uuid.UUID, path: str) -> DocumentContent | None:
        summary = await self.find(user_id=user_id, path=path)
        if summary is None:
            return None
        text, chunks = await self.full_text(user_id=user_id, document_id=summary.id)
        return DocumentContent(document=summary, text=text, chunks=chunks)


def _to_summary(row) -> DocumentSummary:
    return DocumentSummary(
        id=row.id,
        source=row.source,
        source_path=row.source_path,
        title=row.title,
        conversion_status=row.conversion_status,
        conversion_error=row.conversion_error,
        size_bytes=row.size_bytes,
        tags=tuple(row.tags) if row.tags else (),
    )


__all__ = ["DocumentContent", "DocumentSummary", "PostgresDocumentReader"]
