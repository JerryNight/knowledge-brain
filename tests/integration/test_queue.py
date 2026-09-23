"""Queue behaviour that only a real PostgreSQL can show (spec §4 / §6).

The unit suite can pin the *shape* of the claim statement; it cannot tell
whether that statement is valid. It was not: ``dequeue(kinds=...)`` built
``kind = ANY(($4, $5, $6))`` — an expanding bindparam spliced into ``ANY``,
which Postgres rejects with ``WrongObjectTypeError``. Every claim the worker
ever attempted failed, and nothing noticed, because this file did not exist and
the unit guard for the kind filter was a tautology that formatted the template
itself.

These are the tests that would have caught it. They also close the gap the unit
module's docstring used to claim was covered here: concurrency, orphan recovery,
and the retry/give-up path were all untested before.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import text

from kb.models.sync_job import JOB_DOC_INDEX, JOB_FULL_REBUILD, JOB_REPO_SYNC
from kb.queue.base import JobSpec
from kb.queue.postgres import PostgresJobQueue

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

# sync_jobs.user_id has no foreign key on purpose (the worker scans every
# tenant), so a bare UUID is a valid owner here.
USER = uuid.UUID("11111111-1111-1111-1111-111111111111")

KNOWN_KINDS = (JOB_REPO_SYNC, JOB_DOC_INDEX, JOB_FULL_REBUILD)


async def _row(engine: AsyncEngine, job_id: int) -> dict[str, object]:
    """Read a row as the owner, so no RLS or session identity is involved."""
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT status, attempts, locked_at, run_after, last_error, payload FROM sync_jobs WHERE id = :id"),
            {"id": job_id},
        )
        return dict(result.mappings().one())


async def _age(engine: AsyncEngine, job_id: int, *, lock: bool = False) -> None:
    """Push a row's clock into the past, as a dead worker's row would be."""
    column = "locked_at" if lock else "run_after"
    async with engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE sync_jobs SET {column} = now() - make_interval(secs => 3600) WHERE id = :id"),  # noqa: S608
            {"id": job_id},
        )


# -- claiming ---------------------------------------------------------------


async def test_claim_marks_the_job_running_and_counts_the_attempt(
    app_sessionmaker: async_sessionmaker[AsyncSession], admin_engine: AsyncEngine
) -> None:
    queue = PostgresJobQueue(app_sessionmaker)
    job_id = await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC, payload={"a": 1}))

    claimed = await queue.dequeue()

    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.kind == JOB_REPO_SYNC
    assert claimed.attempts == 1, "attempts must include the claim that produced this job"
    assert claimed.payload == {"a": 1}, "payload round-trips through jsonb as a mapping"

    row = await _row(admin_engine, job_id)
    assert row["status"] == "running"
    assert row["locked_at"] is not None
    assert row["attempts"] == 1


async def test_claim_returns_none_when_the_queue_is_empty(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    assert await PostgresJobQueue(app_sessionmaker).dequeue() is None


# -- the regression this file exists for -----------------------------------


async def test_the_kinds_filter_claims_only_the_requested_kinds(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`dequeue(kinds=...)` used to raise before it could ever match a row.

    The filter is built from an expanding bindparam, so the statement has to say
    ``IN``. With ``ANY`` Postgres rejects it outright, and the worker — whose
    every claim passes ``kinds=KNOWN_KINDS`` — could not start at all.
    """
    queue = PostgresJobQueue(app_sessionmaker)
    await queue.enqueue(JobSpec(user_id=USER, kind=JOB_DOC_INDEX))

    assert await queue.dequeue(kinds=(JOB_REPO_SYNC,)) is None, "no repo_sync job exists"

    claimed = await queue.dequeue(kinds=KNOWN_KINDS)
    assert claimed is not None
    assert claimed.kind == JOB_DOC_INDEX


async def test_the_kinds_filter_accepts_a_single_kind(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """One kind is one element in the expanded list — the boundary that broke."""
    queue = PostgresJobQueue(app_sessionmaker)
    job_id = await queue.enqueue(JobSpec(user_id=USER, kind=JOB_FULL_REBUILD))

    claimed = await queue.dequeue(kinds=(JOB_FULL_REBUILD,))

    assert claimed is not None
    assert claimed.id == job_id


async def test_the_kinds_filter_accepts_every_known_kind(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Three kinds — the exact shape the worker uses on every poll."""
    queue = PostgresJobQueue(app_sessionmaker)
    await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))

    assert await queue.dequeue(kinds=KNOWN_KINDS) is not None


# -- scheduling -------------------------------------------------------------


async def test_a_job_scheduled_for_the_future_is_not_claimed(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    queue = PostgresJobQueue(app_sessionmaker)
    await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC, run_after=datetime.now(UTC) + timedelta(hours=1)))

    assert await queue.dequeue() is None


async def test_a_due_job_ahead_of_a_future_one_is_claimed_first(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`run_after NULLS FIRST` — a due job must not queue behind a retry."""
    queue = PostgresJobQueue(app_sessionmaker)
    await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC, run_after=datetime.now(UTC) + timedelta(hours=1)))
    due = await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))

    claimed = await queue.dequeue()

    assert claimed is not None
    assert claimed.id == due


async def test_the_oldest_due_job_goes_first(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    queue = PostgresJobQueue(app_sessionmaker)
    first = await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))
    await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))

    claimed = await queue.dequeue()

    assert claimed is not None
    assert claimed.id == first


# -- concurrency ------------------------------------------------------------


async def test_concurrent_claims_never_return_the_same_job(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`FOR UPDATE SKIP LOCKED`: eight workers, one row, exactly one winner."""
    queue = PostgresJobQueue(app_sessionmaker)
    job_id = await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))

    claimed = await asyncio.gather(*(queue.dequeue(kinds=KNOWN_KINDS) for _ in range(8)))
    ids = [job.id for job in claimed if job is not None]

    assert ids == [job_id], f"exactly one claim should have won, got {ids}"


async def test_each_job_is_claimed_once(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    queue = PostgresJobQueue(app_sessionmaker)
    enqueued = {await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC)) for _ in range(3)}

    claimed = await asyncio.gather(*(queue.dequeue(kinds=KNOWN_KINDS) for _ in range(3)))
    ids = [job.id for job in claimed if job is not None]

    assert len(ids) == 3
    assert set(ids) == enqueued


# -- crash recovery ---------------------------------------------------------


async def test_an_orphaned_running_job_becomes_claimable_again(
    app_sessionmaker: async_sessionmaker[AsyncSession], admin_engine: AsyncEngine
) -> None:
    """A worker that dies mid-job leaves `running` and a stale `locked_at`."""
    queue = PostgresJobQueue(app_sessionmaker, lock_timeout_seconds=900)
    job_id = await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))

    assert (await queue.dequeue()) is not None
    assert await queue.dequeue() is None, "the row is held by the live claim"

    await _age(admin_engine, job_id, lock=True)

    reclaimed = await queue.dequeue()
    assert reclaimed is not None
    assert reclaimed.id == job_id
    assert reclaimed.attempts == 2


async def test_a_recently_locked_job_is_left_alone(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Orphan recovery must not steal work from a worker that is still alive."""
    queue = PostgresJobQueue(app_sessionmaker, lock_timeout_seconds=900)
    await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))
    assert (await queue.dequeue()) is not None

    assert await queue.dequeue() is None


# -- completion and failure -------------------------------------------------


async def test_ack_marks_the_job_done_and_records_the_result(
    app_sessionmaker: async_sessionmaker[AsyncSession], admin_engine: AsyncEngine
) -> None:
    queue = PostgresJobQueue(app_sessionmaker)
    job_id = await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))
    job = await queue.dequeue()
    assert job is not None

    await queue.ack(job, result={"chunks": 3})

    row = await _row(admin_engine, job_id)
    assert row["status"] == "done"
    assert row["locked_at"] is None
    assert row["payload"] == {"chunks": 3}


async def test_ack_without_a_result_keeps_the_original_payload(
    app_sessionmaker: async_sessionmaker[AsyncSession], admin_engine: AsyncEngine
) -> None:
    """`COALESCE(NULL, payload)` — an ack must not erase what the job carried."""
    queue = PostgresJobQueue(app_sessionmaker)
    job_id = await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC, payload={"repo": "x"}))
    job = await queue.dequeue()
    assert job is not None

    await queue.ack(job)

    assert (await _row(admin_engine, job_id))["payload"] == {"repo": "x"}


async def test_failure_reschedules_with_backoff_then_gives_up(
    app_sessionmaker: async_sessionmaker[AsyncSession], admin_engine: AsyncEngine
) -> None:
    """Attempt 1 of 2 retries; attempt 2 hits the ceiling and is marked failed."""
    queue = PostgresJobQueue(app_sessionmaker, max_attempts=2)
    job_id = await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))

    first = await queue.dequeue()
    assert first is not None
    await queue.fail(first, error="boom")

    row = await _row(admin_engine, job_id)
    assert row["status"] == "pending", "a transient failure is retried, not buried"
    assert row["locked_at"] is None
    assert row["last_error"] == "boom"
    assert row["run_after"] is not None, "backoff pushes the retry into the future"
    assert await queue.dequeue() is None, "the retry is not due yet"

    await _age(admin_engine, job_id)
    second = await queue.dequeue()
    assert second is not None
    assert second.attempts == 2

    await queue.fail(second, error="boom again")

    row = await _row(admin_engine, job_id)
    assert row["status"] == "failed"
    assert row["last_error"] == "boom again"
    assert row["locked_at"] is None


# -- introspection ----------------------------------------------------------


async def test_counts_are_reported_per_status_and_per_tenant(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    other = uuid.UUID("22222222-2222-2222-2222-222222222222")
    queue = PostgresJobQueue(app_sessionmaker)
    await queue.enqueue(JobSpec(user_id=USER, kind=JOB_REPO_SYNC))
    await queue.enqueue(JobSpec(user_id=USER, kind=JOB_DOC_INDEX))
    await queue.enqueue(JobSpec(user_id=other, kind=JOB_REPO_SYNC))

    assert await queue.counts() == {"pending": 3}
    assert await queue.counts_for_user(USER) == {"pending": 2}
    assert await queue.counts_for_user(other) == {"pending": 1}


async def test_enqueue_batch_inserts_every_spec_in_one_transaction(
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    queue = PostgresJobQueue(app_sessionmaker)
    specs = [JobSpec(user_id=USER, kind=JOB_DOC_INDEX, payload={"i": i}) for i in range(5)]

    assert await queue.enqueue_batch(specs) == 5
    assert await queue.enqueue_batch([]) == 0

    assert await queue.counts() == {"pending": 5}
