"""`sync_jobs` table — spec §5 / §4.

The Postgres-backed queue. No message middleware in phase 1.

Postgres has one advantage Redis cannot offer (spec §4): enqueue happens in the
same transaction as the business write, so there is no dual-write inconsistency.
When migrating to Redis later, that guarantee disappears and must be handled
manually.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from kb.models.base import Base

# job kind
JOB_REPO_SYNC = "repo_sync"
JOB_DOC_INDEX = "doc_index"
JOB_FULL_REBUILD = "full_rebuild"

# job status
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # upload 任务为 null
    repo_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repos.id", ondelete="CASCADE"), nullable=True
    )
    # 'repo_sync' | 'doc_index' | 'full_rebuild'
    kind: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # pending | running | done | failed
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=STATUS_PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 指数退避
    run_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
