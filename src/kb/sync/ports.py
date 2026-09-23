"""Ports the sync and upload pipelines depend on.

The pipelines are written against these protocols rather than against
SQLAlchemy, which is what makes the important behaviours testable without a
database: the red-line test for ``last_synced_sha`` advancement has to simulate a
failure *between batches*, and that is far easier to arrange through a port than
through a real connection pool.

Ports also keep the direction of dependency honest: ``kb.sync`` does not import
``kb.db``, so the pipeline cannot accidentally reach around the tenant-scoped
query builder.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from kb.queue.base import JobSpec

# Media source names. `documents` is unique on (user_id, source, source_path)
# (spec §5 ③), so these two channels never collide.
SOURCE_GIT = "git"
SOURCE_UPLOAD = "upload"


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """The part of a ``documents`` row the pipelines reason about."""

    id: uuid.UUID
    user_id: uuid.UUID
    source: str
    source_path: str
    content_sha: str
    converted_sha: str | None = None
    conversion_status: str = "ok"
    size_bytes: int | None = None
    indexed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NewDocument:
    """A document row to create (or update) before its indexing job runs."""

    user_id: uuid.UUID
    source: str
    source_path: str
    content_sha: str
    mime: str | None = None
    size_bytes: int | None = None
    title: str | None = None
    tags: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RepoRef:
    """A repository to sync, decoupled from the ORM object."""

    id: uuid.UUID
    user_id: uuid.UUID
    url: str
    branch: str = "main"
    last_synced_sha: str | None = None
    credential_ref: str | None = None


@runtime_checkable
class VaultStore(Protocol):
    """Tenant-scoped persistence the pipelines need.

    Every method takes ``user_id`` explicitly. The Postgres implementation uses
    it both as the tenant predicate and as the RLS session variable, so a missed
    argument is a type error rather than a cross-tenant read.
    """

    async def find_document(self, *, user_id: uuid.UUID, source: str, source_path: str) -> DocumentRecord | None: ...

    async def list_document_paths(self, *, user_id: uuid.UUID, source: str) -> list[str]: ...

    async def upsert_document(self, document: NewDocument) -> DocumentRecord: ...

    async def delete_document(self, *, user_id: uuid.UUID, source: str, source_path: str) -> bool: ...

    async def rename_document(self, *, user_id: uuid.UUID, source: str, old_path: str, new_path: str) -> bool: ...

    async def set_last_synced_sha(self, *, user_id: uuid.UUID, repo_id: uuid.UUID, sha: str) -> None: ...


@runtime_checkable
class JobEnqueuer(Protocol):
    """Batch enqueue. One transaction per batch, committed before the next."""

    async def enqueue_batch(self, specs: Sequence[JobSpec]) -> int: ...


@runtime_checkable
class UnitOfWorkScope(VaultStore, Protocol):
    """A vault store whose writes share one transaction with ``enqueue``."""

    async def enqueue(self, spec: JobSpec) -> int: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Opens one tenant-bound transaction.

    ``enqueue`` inside the scope joins it, which is the property spec §4 calls
    out: a job and the row it refers to commit together, so no queued work ever
    points at a document that was rolled back.
    """

    def begin(self, user_id: uuid.UUID) -> AbstractAsyncContextManager[UnitOfWorkScope]: ...


@dataclass(frozen=True, slots=True)
class JobPayload:
    """The ``payload`` of a ``doc_index`` job.

    Carrying the hash forward means the indexer does not have to re-derive it,
    and it gives the job a chance to notice that the document changed again
    since the job was queued.
    """

    source: str
    source_path: str
    content_sha: str
    size_bytes: int | None = None
    mime: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_path": self.source_path,
            "content_sha": self.content_sha,
            "size_bytes": self.size_bytes,
            "mime": self.mime,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> JobPayload:
        return cls(
            source=str(payload["source"]),
            source_path=str(payload["source_path"]),
            content_sha=str(payload["content_sha"]),
            size_bytes=payload.get("size_bytes"),
            mime=payload.get("mime"),
        )
