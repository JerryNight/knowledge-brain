"""Queue — the narrow interface from spec §4.

Four verbs, and a job object that carries no database session:

    enqueue(job) / dequeue() / ack(job, result) / fail(job, error)

Phase 1 ships a Postgres implementation (``kb.queue.postgres``). Swapping in
Redis later means writing one more class implementing ``JobQueue`` — callers do
not change, because they never see SQL.

The one guarantee worth keeping in the interface is the reason Postgres was
chosen (spec §4): ``enqueue`` can join a *caller-supplied transaction*, so a job
and the row it refers to are committed together or not at all. Redis cannot
offer that, which is why the alternative is documented as a migration cost
rather than an implementation detail.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncSession

# Backoff schedule: 30s, 1m, 2m, 4m, 8m, ... capped at an hour. Retries are for
# transient failures (network, rate limits); a permanently broken repo will hit
# the attempt ceiling and stop.
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class JobSpec:
    """A job to be created. ``user_id`` is mandatory: every job is tenant-owned."""

    user_id: uuid.UUID
    kind: str
    repo_id: uuid.UUID | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    run_after: datetime | None = None


@dataclass(frozen=True, slots=True)
class Job:
    """A claimed job. ``attempts`` already includes the claim that produced it."""

    id: int
    user_id: uuid.UUID
    kind: str
    repo_id: uuid.UUID | None
    payload: Mapping[str, Any]
    attempts: int


@runtime_checkable
class JobQueue(Protocol):
    async def enqueue(self, spec: JobSpec, *, session: AsyncSession | None = None) -> int: ...

    async def dequeue(self, *, kinds: Sequence[str] | None = None) -> Job | None: ...

    async def ack(self, job: Job, *, result: Mapping[str, Any] | None = None) -> None: ...

    async def fail(self, job: Job, *, error: str) -> None: ...


def backoff_delay(
    attempts: int,
    *,
    base: float = BACKOFF_BASE_SECONDS,
    cap: float = BACKOFF_CAP_SECONDS,
) -> float:
    """Seconds to wait before retrying after ``attempts`` attempts.

    ``attempts`` counts attempts already made, so the first failure waits
    ``base``, not ``2 * base``. Capped so a long-broken job still gets retried
    eventually instead of drifting into the next century.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    return float(min(cap, base * (2 ** (attempts - 1))))


def should_retry(attempts: int, max_attempts: int) -> bool:
    """Whether a failed job gets another chance.

    ``attempts`` is the count *after* the attempt that just failed, which is why
    this is a ``>=`` comparison against the ceiling.
    """
    return attempts < max_attempts
