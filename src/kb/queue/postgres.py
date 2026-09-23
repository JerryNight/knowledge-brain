"""Postgres-backed queue — ``SELECT ... FOR UPDATE SKIP LOCKED`` (spec §4 / §6).

Three properties this implementation is responsible for:

* **No double claims.** ``FOR UPDATE SKIP LOCKED`` lets N workers pull from the
  same table concurrently without a lock convoy and without two of them getting
  the same row.
* **Crash recovery.** A claimed job carries ``locked_at``. A job still marked
  ``running`` after the lock timeout (15 minutes, spec §6) is treated as an
  orphan from a dead worker and becomes claimable again.
* **Exponential backoff.** ``fail`` either reschedules with a future
  ``run_after`` or, once the attempt ceiling is reached, marks the job failed
  for good.

The SQL lives here rather than in the query builder because ``sync_jobs`` is not
a tenant-scoped table: it deliberately has no RLS policy, since the worker must
be able to scan across tenants to find work.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.models.sync_job import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    SyncJob,
)
from kb.queue.base import Job, JobSpec, backoff_delay, should_retry

# `run_after NULLS FIRST` puts jobs that are due immediately ahead of retries.
# The orphan branch is the crash-recovery path: a `running` row whose lock is
# older than the timeout is reclaimed rather than stuck forever.
_DEQUEUE_TEMPLATE = """
WITH candidate AS (
    SELECT id
      FROM sync_jobs
     WHERE (
             status = :pending
             OR (status = :running AND locked_at IS NOT NULL
                 AND locked_at < now() - make_interval(secs => :lock_timeout))
           )
       AND (run_after IS NULL OR run_after <= now())
       {kind_filter}
     ORDER BY run_after NULLS FIRST, id
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE sync_jobs AS j
   SET status = :running, locked_at = now(), attempts = j.attempts + 1
  FROM candidate AS c
 WHERE j.id = c.id
RETURNING j.id, j.user_id, j.repo_id, j.kind, j.payload, j.attempts
"""

ACK_SQL = text(
    """
    UPDATE sync_jobs
       SET status = :done, locked_at = NULL, last_error = NULL,
           payload = COALESCE(CAST(:result AS jsonb), payload)
     WHERE id = :id
    """
)

RETRY_SQL = text(
    """
    UPDATE sync_jobs
       SET status = :pending, locked_at = NULL, last_error = :error,
           run_after = now() + make_interval(secs => :delay)
     WHERE id = :id
    """
)

GIVE_UP_SQL = text(
    """
    UPDATE sync_jobs
       SET status = :failed, locked_at = NULL, last_error = :error
     WHERE id = :id
    """
)

COUNTS_SQL = text("SELECT status, count(*) AS total FROM sync_jobs GROUP BY status")

# Tenant-scoped variant. ``sync_jobs`` has no RLS policy (the worker must scan
# across tenants), so the predicate has to be explicit here — otherwise the
# admin status endpoint would report the whole installation's queue depth to
# every caller.
COUNTS_FOR_USER_SQL = text(
    "SELECT status, count(*) AS total FROM sync_jobs WHERE user_id = :user_id GROUP BY status"
)


class PostgresJobQueue:
    """``JobQueue`` implemented on ``sync_jobs``."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        lock_timeout_seconds: int = 900,
        max_attempts: int = 5,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._lock_timeout_seconds = lock_timeout_seconds
        self._max_attempts = max_attempts

    @classmethod
    def from_settings(cls, sessionmaker: async_sessionmaker[AsyncSession]) -> PostgresJobQueue:
        from kb.config import get_settings

        settings = get_settings()
        return cls(
            sessionmaker,
            lock_timeout_seconds=settings.queue_lock_timeout_seconds,
            max_attempts=settings.queue_max_attempts,
        )

    # -- producer -----------------------------------------------------------

    async def enqueue(self, spec: JobSpec, *, session: AsyncSession | None = None) -> int:
        """Create a job and return its id.

        Pass ``session`` to enlist in a caller's transaction (spec §4): the job
        then commits exactly when the business write does, so there is no window
        where work is queued for a row that never landed.
        """
        if session is None:
            async with self._sessionmaker() as own_session:
                async with own_session.begin():
                    return await self._insert(own_session, spec)
        return await self._insert(session, spec)

    async def _insert(self, session: AsyncSession, spec: JobSpec) -> int:
        job = SyncJob(
            user_id=spec.user_id,
            repo_id=spec.repo_id,
            kind=spec.kind,
            payload=dict(spec.payload),
            status=STATUS_PENDING,
            run_after=spec.run_after,
        )
        session.add(job)
        await session.flush()
        return int(job.id)

    async def enqueue_batch(self, specs: Sequence[JobSpec]) -> int:
        """Insert many jobs in **one** transaction and commit before returning.

        This satisfies ``kb.sync.ports.JobEnqueuer``, which the sync pipeline
        calls per 500-file batch (spec §6): the commit boundary is what makes a
        mid-sync crash resumable, and it is also what ``last_synced_sha`` waits
        for before advancing.
        """
        if not specs:
            return 0
        async with self._sessionmaker() as session:
            async with session.begin():
                for spec in specs:
                    await self._insert(session, spec)
        return len(specs)

    # -- consumer -----------------------------------------------------------

    async def dequeue(self, *, kinds: Sequence[str] | None = None) -> Job | None:
        """Claim the oldest due job, or return ``None`` when there is nothing to do."""
        if kinds:
            statement = text(_DEQUEUE_TEMPLATE.format(kind_filter="AND kind = ANY(:kinds)")).bindparams(
                bindparam("kinds", expanding=True)
            )
        else:
            statement = text(_DEQUEUE_TEMPLATE.format(kind_filter=""))

        params: dict[str, object] = {
            "pending": STATUS_PENDING,
            "running": STATUS_RUNNING,
            "lock_timeout": self._lock_timeout_seconds,
        }
        if kinds:
            params["kinds"] = list(kinds)

        async with self._sessionmaker() as session:
            async with session.begin():
                row = (await session.execute(statement, params)).mappings().one_or_none()
        return _to_job(row) if row else None

    async def ack(self, job: Job, *, result: Mapping[str, object] | None = None) -> None:
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    ACK_SQL,
                    {
                        "id": job.id,
                        "done": STATUS_DONE,
                        "result": json.dumps(dict(result), ensure_ascii=False) if result else None,
                    },
                )

    async def fail(self, job: Job, *, error: str) -> None:
        """Reschedule with backoff, or give up once ``max_attempts`` is reached."""
        retry = should_retry(job.attempts, self._max_attempts)
        delay = backoff_delay(job.attempts) if retry else 0.0
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    RETRY_SQL if retry else GIVE_UP_SQL,
                    {
                        "id": job.id,
                        "pending": STATUS_PENDING,
                        "failed": STATUS_FAILED,
                        "error": error[:4000],
                        "delay": delay,
                    },
                )

    # -- introspection ------------------------------------------------------

    async def counts(self) -> dict[str, int]:
        """Job counts per status, **across all tenants**.

        Only for the worker's own logging and for maintenance — the REST status
        endpoint uses ``counts_for_user`` so it does not disclose queue depth
        belonging to other tenants.
        """
        async with self._sessionmaker() as session:
            rows = (await session.execute(COUNTS_SQL)).all()
        return {row[0]: int(row[1]) for row in rows}

    async def counts_for_user(self, user_id: uuid.UUID) -> dict[str, int]:
        """Job counts per status for one tenant — what ``GET /api/admin/status`` reports."""
        async with self._sessionmaker() as session:
            rows = (await session.execute(COUNTS_FOR_USER_SQL, {"user_id": user_id})).all()
        return {row[0]: int(row[1]) for row in rows}


def _to_job(row: Mapping[str, object]) -> Job:
    return Job(
        id=int(row["id"]),  # type: ignore[arg-type]
        user_id=row["user_id"],  # type: ignore[arg-type]
        kind=str(row["kind"]),
        repo_id=row["repo_id"],  # type: ignore[arg-type]
        payload=row["payload"] or {},  # type: ignore[arg-type]
        attempts=int(row["attempts"]),  # type: ignore[arg-type]
    )
