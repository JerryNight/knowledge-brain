"""Unit tests for the queue's arithmetic and SQL shape.

Concurrency (two workers, one row) and orphan recovery need a real Postgres and
live in the integration suite. What can be pinned down here is the retry
schedule, the decision to give up, and the presence of the lock hints that make
the Postgres implementation safe in the first place.
"""

from __future__ import annotations

import pytest

from kb.queue import base as queue_base
from kb.queue.base import BACKOFF_CAP_SECONDS, Job, JobQueue, JobSpec, backoff_delay, should_retry
from kb.queue.postgres import _DEQUEUE_TEMPLATE, PostgresJobQueue


def test_backoff_starts_at_base_and_doubles() -> None:
    assert backoff_delay(1) == queue_base.BACKOFF_BASE_SECONDS
    assert backoff_delay(2) == queue_base.BACKOFF_BASE_SECONDS * 2
    assert backoff_delay(3) == queue_base.BACKOFF_BASE_SECONDS * 4


def test_backoff_is_capped() -> None:
    assert backoff_delay(20) == BACKOFF_CAP_SECONDS


def test_backoff_rejects_nonsense_attempt_counts() -> None:
    with pytest.raises(ValueError):
        backoff_delay(0)


def test_retry_until_the_attempt_ceiling_then_stop() -> None:
    assert should_retry(1, 5)
    assert should_retry(4, 5)
    assert not should_retry(5, 5)
    assert not should_retry(6, 5)


def test_job_spec_defaults_to_an_immediately_due_job() -> None:
    spec = JobSpec(user_id="11111111-1111-1111-1111-111111111111", kind="repo_sync")
    assert spec.run_after is None
    assert spec.repo_id is None
    assert dict(spec.payload) == {}


def test_dequeue_sql_uses_skip_locked() -> None:
    """Without SKIP LOCKED, concurrent workers serialise behind one another."""
    assert "FOR UPDATE SKIP LOCKED" in _DEQUEUE_TEMPLATE


def test_dequeue_sql_has_an_orphan_recovery_branch() -> None:
    """A row stuck in `running` past the lock timeout must become claimable."""
    sql = _DEQUEUE_TEMPLATE.format(kind_filter="")
    assert "locked_at < now() - make_interval" in sql
    assert ":running" in sql


def test_dequeue_sql_ignores_jobs_scheduled_for_the_future() -> None:
    assert "run_after IS NULL OR run_after <= now()" in _DEQUEUE_TEMPLATE


def test_kind_filter_is_only_added_when_requested() -> None:
    assert "ANY(:kinds)" not in _DEQUEUE_TEMPLATE.format(kind_filter="")
    assert "kind = ANY(:kinds)" in _DEQUEUE_TEMPLATE.format(kind_filter="AND kind = ANY(:kinds)")


def test_postgres_queue_satisfies_the_interface() -> None:
    assert isinstance(PostgresJobQueue(sessionmaker=None), JobQueue)  # type: ignore[arg-type]


def test_job_carries_the_attempt_count_including_its_own_claim() -> None:
    job = Job(id=7, user_id="u", kind="doc_index", repo_id=None, payload={"a": 1}, attempts=2)
    assert job.attempts == 2
    assert job.payload == {"a": 1}
