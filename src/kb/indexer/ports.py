"""Ports the indexer depends on.

Same reasoning as ``kb.sync.ports``: the level-3 short circuit (identical chunk
text at the same ordinal means no re-embedding) is the expensive-to-get-wrong
behaviour, and it is only testable if "what already exists" can be arranged
directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ExistingChunk:
    """What is already stored for one ``(document_id, ordinal)``."""

    ordinal: int
    text: str
    embedding: list[float] | None = None


@dataclass(frozen=True, slots=True)
class ChunkWrite:
    """A chunk ready to be persisted."""

    ordinal: int
    text: str
    heading_path: Sequence[str] = field(default_factory=tuple)
    locator: dict | None = None
    token_count: int = 0
    # None is a real state, not a missing value: a chunk outside the embedding
    # window lives in the full-text index only (spec §8).
    embedding: list[float] | None = None


@dataclass(frozen=True, slots=True)
class DocumentUpdate:
    """The document-level outcome of an indexing run."""

    converted_sha: str | None = None
    conversion_status: str | None = None
    conversion_error: str | None = None
    title: str | None = None
    tags: Sequence[str] | None = None
    indexed_at: datetime | None = None


@runtime_checkable
class BlobSource(Protocol):
    """Where a document's original bytes come from.

    Git and upload have separate implementations; the indexer never learns which
    one it is talking to (spec §4).
    """

    async def read(self, *, user_id: uuid.UUID, source: str, source_path: str) -> bytes | None: ...


@runtime_checkable
class IndexWriter(Protocol):
    """The writes an indexing run performs, in one tenant."""

    async def existing_chunks(
        self, *, user_id: uuid.UUID, document_id: uuid.UUID
    ) -> dict[int, ExistingChunk]: ...

    async def replace_chunks(
        self, *, user_id: uuid.UUID, document_id: uuid.UUID, chunks: Sequence[ChunkWrite]
    ) -> int: ...

    async def update_document(
        self, *, user_id: uuid.UUID, document_id: uuid.UUID, update: DocumentUpdate
    ) -> None: ...
