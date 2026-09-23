"""Indexing service — one document, from bytes to stored chunks.

The pipeline for a ``doc_index`` job (spec §6 / §7 / §8):

1. read the original bytes;
2. convert (cache-aware, size-capped) — a failure is recorded, not raised;
3. chunk the markdown, carrying page/sheet locators through;
4. **level-3 short circuit**: chunks whose ``(ordinal, text)`` are unchanged keep
   their existing vectors and are not embedded again;
5. write the chunks, then the document row.

Why the last two steps are ordered that way: both writes are idempotent, so a
crash between them costs a retry rather than correctness — and writing chunks
first means the retry takes the level-3 path instead of paying for embeddings a
second time.

The embedding window (spec §8) is applied here rather than in the chunker because
this is where the cost is incurred: a chunk under 10 or over 8000 tokens is stored
with ``embedding = None`` and is reachable through the keyword branch only.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from kb.converter.service import ConversionService
from kb.hashing import sha256_hex, sha256_text
from kb.indexer.chunker import ChunkDraft, ChunkOptions, chunk_markdown
from kb.indexer.embedding import EmbeddingError, EmbeddingProvider
from kb.indexer.frontmatter import derive_title, parse_frontmatter
from kb.indexer.ports import BlobSource, ChunkWrite, DocumentUpdate, ExistingChunk, IndexWriter
from kb.models.document import CONVERSION_STATUSES

LOGGER = logging.getLogger(__name__)

# Markdown sources carry frontmatter; converted formats do not.
MARKDOWN_SUFFIXES = (".md", ".markdown")


class IndexConfigMismatch(RuntimeError):
    """The stored index was built with different settings (spec §5 ④).

    Raised instead of writing: one vector column cannot hold two dimensions, so
    a mismatch means the index must be rebuilt, not appended to.
    """


@dataclass(slots=True)
class IndexOutcome:
    """What one indexing run did."""

    document_id: uuid.UUID | None = None
    status: str = "ok"
    chunks: int = 0
    embedded: int = 0
    reused: int = 0
    skipped: bool = False
    reason: str | None = None

    @property
    def indexed(self) -> bool:
        return not self.skipped and self.status == "ok"


class IndexingService:
    def __init__(
        self,
        *,
        source: BlobSource,
        converter: ConversionService,
        writer: IndexWriter,
        embedder: EmbeddingProvider | None = None,
        options: ChunkOptions | None = None,
        verify_index=None,
    ) -> None:
        self._source = source
        self._converter = converter
        self._writer = writer
        self._embedder = embedder
        self._options = options or ChunkOptions()
        self._verify_index = verify_index

    @classmethod
    def from_settings(cls, *, source, converter, writer, embedder=None, verify_index=None) -> IndexingService:
        return cls(
            source=source,
            converter=converter,
            writer=writer,
            embedder=embedder,
            options=ChunkOptions.from_settings(),
            verify_index=verify_index,
        )

    async def index_document(
        self,
        *,
        user_id: uuid.UUID,
        document_id: uuid.UUID,
        source: str,
        source_path: str,
        content_sha: str,
        mime: str | None = None,
    ) -> IndexOutcome:
        if self._verify_index is not None:
            await self._verify_index()

        blob = await self._source.read(user_id=user_id, source=source, source_path=source_path)
        if blob is None:
            return IndexOutcome(document_id=document_id, skipped=True, reason="source bytes not found")

        # The job carries the hash it was queued for. If the file has changed
        # again since then, a newer job is already on its way and this one would
        # only write stale content.
        if sha256_hex(blob) != content_sha:
            return IndexOutcome(document_id=document_id, skipped=True, reason="content changed since the job was queued")

        conversion = await self._converter.convert(
            blob, path=source_path, content_sha=content_sha, mime=mime
        )
        if not conversion.ok:
            # Failure is isolated to this document (spec §7 约束 3): stale chunks
            # go, the status is recorded, the batch continues.
            await self._writer.replace_chunks(user_id=user_id, document_id=document_id, chunks=())
            await self._writer.update_document(
                user_id=user_id,
                document_id=document_id,
                update=DocumentUpdate(
                    conversion_status=_safe_status(conversion.status),
                    conversion_error=conversion.error,
                    indexed_at=datetime.now(UTC),
                ),
            )
            return IndexOutcome(document_id=document_id, status=conversion.status, reason=conversion.error)

        markdown, title, tags = _extract_metadata(conversion.markdown, source_path)
        drafts = chunk_markdown(markdown, self._options)

        existing = await self._writer.existing_chunks(user_id=user_id, document_id=document_id)
        writes, embedded, reused = await self._build_writes(user_id=user_id, drafts=drafts, existing=existing)

        await self._writer.replace_chunks(user_id=user_id, document_id=document_id, chunks=writes)
        await self._writer.update_document(
            user_id=user_id,
            document_id=document_id,
            update=DocumentUpdate(
                converted_sha=sha256_text(conversion.markdown),
                conversion_status="ok",
                conversion_error=None,
                title=title,
                tags=tags,
                indexed_at=datetime.now(UTC),
            ),
        )

        LOGGER.info(
            "indexed document=%s chunks=%d embedded=%d reused=%d", document_id, len(writes), embedded, reused
        )
        return IndexOutcome(
            document_id=document_id, status="ok", chunks=len(writes), embedded=embedded, reused=reused
        )

    # -- level 3 ------------------------------------------------------------

    async def _build_writes(
        self,
        *,
        user_id: uuid.UUID,
        drafts: list[ChunkDraft],
        existing: dict[int, ExistingChunk],
    ) -> tuple[list[ChunkWrite], int, int]:
        """Decide what actually needs embedding (spec §6, level 3).

        A chunk is re-embedded only when it is new, when its text changed, or
        when it should have a vector and does not. Everything else reuses the
        stored vector, which is what makes re-running a job — or clicking sync
        twice — free.
        """
        stale: list[ChunkDraft] = []
        for draft in drafts:
            previous = existing.get(draft.ordinal)
            if previous is None or previous.text != draft.text or (
                draft.needs_embedding(self._options) and previous.embedding is None
            ):
                stale.append(draft)

        vectors: dict[int, list[float]] = {}
        to_embed = [draft for draft in stale if draft.needs_embedding(self._options)]
        if to_embed and self._embedder is not None:
            try:
                results = await self._embedder.embed([draft.embed_text for draft in to_embed])
            except EmbeddingError:
                # The keyword branch still works, so the document is indexed
                # without vectors rather than failing outright. The mismatch is
                # visible in the next run: those chunks have embedding IS NULL.
                LOGGER.warning("embedding failed for document chunks; storing them unembedded")
                results = []
            vectors = {draft.ordinal: vector for draft, vector in zip(to_embed, results, strict=False)}

        writes: list[ChunkWrite] = []
        reused = 0
        for draft in drafts:
            previous = existing.get(draft.ordinal)
            embedding = vectors.get(draft.ordinal)
            if embedding is None and previous is not None and previous.text == draft.text:
                embedding = previous.embedding
                if embedding is not None:
                    reused += 1
            writes.append(
                ChunkWrite(
                    ordinal=draft.ordinal,
                    text=draft.text,
                    heading_path=draft.heading_path,
                    locator=draft.locator,
                    token_count=draft.token_count,
                    embedding=embedding,
                )
            )
        return writes, len(vectors), reused


def _extract_metadata(markdown: str, source_path: str) -> tuple[str, str | None, list[str]]:
    """Return ``(body_for_chunking, title, tags)``.

    Frontmatter is metadata, not content: it becomes columns on ``documents``
    and is stripped before chunking, so ``title: Q3 复盘`` does not end up as a
    chunk of its own. Markdown's own syntax (wikilinks, callouts, dataview
    blocks) is never touched (spec §6).
    """
    if not source_path.lower().endswith(MARKDOWN_SUFFIXES):
        return markdown, None, []
    parsed = parse_frontmatter(markdown)
    title = parsed.title or derive_title(parsed.body)
    return parsed.body, title, parsed.tags


def _safe_status(status: str) -> str:
    return status if status in CONVERSION_STATUSES else "failed"


__all__ = ["IndexConfigMismatch", "IndexOutcome", "IndexingService"]
