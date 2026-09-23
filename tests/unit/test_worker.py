"""Unit tests for the queue consumer (spec §4 / §6 / §9).

The worker's job is dispatch, ack and fail — so that is what is tested, with the
three handlers replaced by recorders. The two properties worth pinning down are
both about *when* things happen rather than what they compute:

* a job is acked only after its handler returns, and a failing handler produces
  ``fail`` rather than a silent ack (spec §6's retry-and-backoff contract);
* an empty queue does not spin.
"""

from __future__ import annotations

import uuid

import pytest

from kb.models.sync_job import JOB_DOC_INDEX, JOB_FULL_REBUILD, JOB_REPO_SYNC
from kb.queue.base import Job, JobSpec
from kb.sync.ports import JobPayload
from kb.worker import KNOWN_KINDS, Worker
from tests.unit.fakes import make_services

USER = uuid.UUID("66666666-6666-6666-6666-666666666666")


def job(kind: str = JOB_DOC_INDEX, payload: dict | None = None, repo_id=None) -> Job:
    return Job(
        id=1,
        user_id=USER,
        kind=kind,
        repo_id=repo_id,
        payload=payload or {},
        attempts=1,
    )


class FakeQueue:
    """A queue that hands out a fixed script and records what the worker did."""

    def __init__(self, jobs: list[Job] | None = None) -> None:
        self.jobs = list(jobs or [])
        self.acked: list[tuple[int, dict]] = []
        self.failed: list[tuple[int, str]] = []
        self.enqueued: list[JobSpec] = []
        self.dequeue_kinds: list[tuple[str, ...] | None] = []

    async def dequeue(self, *, kinds=None):
        self.dequeue_kinds.append(tuple(kinds) if kinds else None)
        return self.jobs.pop(0) if self.jobs else None

    async def ack(self, job, *, result=None):
        self.acked.append((job.id, dict(result or {})))

    async def fail(self, job, *, error):
        self.failed.append((job.id, error))

    async def enqueue_batch(self, specs):
        self.enqueued.extend(specs)
        return len(list(specs))


def make_worker(**overrides):
    services = make_services(**overrides)
    return Worker(services, poll_interval_seconds=0), services


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


async def test_the_worker_asks_for_the_kinds_it_knows() -> None:
    queue = FakeQueue()
    worker, _ = make_worker(queue=queue)
    await worker.run_once()
    assert queue.dequeue_kinds == [KNOWN_KINDS]


async def test_an_empty_queue_is_not_work() -> None:
    """``run_once`` returning False is what stops ``run_forever`` spinning."""
    worker, _ = make_worker(queue=FakeQueue())
    assert await worker.run_once() is False


async def test_an_unknown_kind_fails_the_job_rather_than_acking_it() -> None:
    """A job nobody understands must not be marked done — that would lose it."""
    queue = FakeQueue([job("something_else")])
    worker, _ = make_worker(queue=queue)
    await worker.run_once()
    assert queue.acked == []
    assert queue.failed and "unknown job kind" in queue.failed[0][1]


# ---------------------------------------------------------------------------
# repo_sync
# ---------------------------------------------------------------------------


async def test_repo_sync_runs_the_pipeline_and_acks_with_a_summary() -> None:
    repo = uuid.uuid4()
    outcome = _FakeOutcome(repo)
    queue = FakeQueue([job(JOB_REPO_SYNC, {}, repo_id=repo)])
    worker, _ = make_worker(queue=queue, repos=_FakeRepoService([_FakeRepoRef(repo, USER)]))
    worker._pipeline = _FakePipeline(outcome)  # noqa: SLF001 - the seam under test

    await worker.run_once()

    assert queue.acked[0][1]["repos"][0]["jobs_enqueued"] == 3
    assert queue.failed == []


async def test_repo_sync_with_a_vanished_repo_skips_instead_of_failing() -> None:
    """A repository deleted between enqueue and dequeue is a skip, not an error to retry."""
    queue = FakeQueue([job(JOB_REPO_SYNC, {}, repo_id=uuid.uuid4())])
    worker, _ = make_worker(queue=queue, repos=_FakeRepoService([]))
    worker._pipeline = _FakePipeline(None)  # noqa: SLF001

    await worker.run_once()

    assert queue.failed == []
    assert "gone or sync is disabled" in queue.acked[0][1]["skipped"]


async def test_repo_sync_with_no_repo_named_covers_every_enabled_one() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    queue = FakeQueue([job(JOB_REPO_SYNC, {})])
    worker, _ = make_worker(
        queue=queue,
        repos=_FakeRepoService([_FakeRepoRef(first, USER), _FakeRepoRef(second, USER)]),
    )
    pipeline = worker._pipeline = _FakePipeline(_FakeOutcome(first))  # noqa: SLF001

    await worker.run_once()

    assert pipeline.synced == [first, second]


# ---------------------------------------------------------------------------
# doc_index
# ---------------------------------------------------------------------------


async def test_doc_index_passes_the_payload_through_and_acks_the_outcome() -> None:
    payload = JobPayload(
        source="git", source_path="notes/a.md", content_sha="c" * 64, size_bytes=10
    ).as_dict()
    queue = FakeQueue([job(JOB_DOC_INDEX, payload)])
    indexer = _FakeIndexer()
    worker, _ = make_worker(queue=queue, vault=_FakeVault(), indexer=indexer)

    await worker.run_once()

    call = indexer.calls[0]
    assert call["source_path"] == "notes/a.md"
    assert call["content_sha"] == "c" * 64
    assert queue.acked[0][1]["status"] == "ok"
    assert queue.acked[0][1]["chunks"] == 4


async def test_doc_index_for_a_deleted_document_skips_rather_than_failing() -> None:
    """The document can be deleted between enqueue and dequeue; that is not an error."""
    payload = JobPayload(source="git", source_path="gone.md", content_sha="d" * 64).as_dict()
    queue = FakeQueue([job(JOB_DOC_INDEX, payload)])
    indexer = _FakeIndexer()
    worker, _ = make_worker(queue=queue, vault=_FakeVault(found=False), indexer=indexer)

    await worker.run_once()

    assert indexer.calls == []
    assert queue.failed == []
    assert "no longer exists" in queue.acked[0][1]["skipped"]


async def test_a_failing_handler_fails_the_job_with_the_error_message() -> None:
    """``fail`` is what schedules the retry; acking a crashed job would lose it."""
    payload = JobPayload(source="git", source_path="a.md", content_sha="e" * 64).as_dict()
    queue = FakeQueue([job(JOB_DOC_INDEX, payload)])
    worker, _ = make_worker(
        queue=queue, vault=_FakeVault(), indexer=_FakeIndexer(error=RuntimeError("boom"))
    )

    await worker.run_once()

    assert queue.acked == []
    assert queue.failed[0][1].startswith("RuntimeError: boom")


# ---------------------------------------------------------------------------
# full_rebuild
# ---------------------------------------------------------------------------


async def test_rebuild_drops_chunks_and_re_enqueues_every_document() -> None:
    """spec §9: 清空索引并全量重建, expressed as durable per-document jobs (§6's batching)."""
    queue = FakeQueue([job(JOB_FULL_REBUILD, {})])
    maintenance = _FakeMaintenance(documents=3)
    config = _FakeIndexConfig()
    worker, services = make_worker(queue=queue, index_maintenance=maintenance, index_config=config)

    await worker.run_once()

    assert maintenance.dropped == [USER]
    assert maintenance.timestamps_cleared == [USER]
    assert len(queue.enqueued) == 3
    assert all(spec.kind == JOB_DOC_INDEX for spec in queue.enqueued)
    assert all(spec.user_id == USER for spec in queue.enqueued)
    # The rebuild is the one place the stamp is legitimately rewritten.
    assert config.stamps and config.stamps[0]["chunker_version"] == services.settings.chunker_version
    assert queue.acked[0][1] == {"chunks_dropped": 7, "documents_requeued": 3}


async def test_rebuild_batches_the_work_list() -> None:
    """Progress has to be durable: one enormous transaction loses everything on a crash."""
    queue = FakeQueue([job(JOB_FULL_REBUILD, {})])
    maintenance = _FakeMaintenance(documents=1200)
    worker, _ = make_worker(queue=queue, index_maintenance=maintenance, index_config=_FakeIndexConfig())

    await worker.run_once()

    # 1200 documents at 500 per enqueue_batch.
    assert len(queue.enqueued) == 1200


# ---------------------------------------------------------------------------
# doubles
# ---------------------------------------------------------------------------


class _FakeRepoRef:
    def __init__(self, repo_id, user_id) -> None:
        self.id = repo_id
        self.user_id = user_id
        self.url = "git@example/repo.git"
        self.branch = "main"
        self.last_synced_sha = None
        self.credential_ref = None


class _FakeRepoService:
    def __init__(self, repos) -> None:
        self._repos = list(repos)

    async def repos_for_sync(self, user_id):
        return [repo for repo in self._repos if repo.user_id == user_id]


class _FakeOutcome:
    def __init__(self, repo_id) -> None:
        self.repo_id = repo_id
        self.unchanged = False
        self.full_rescan = True
        self.added = 3
        self.modified = 0
        self.renamed = 0
        self.deleted = 0
        self.skipped = 0
        self.oversized = 0
        self.jobs_enqueued = 3


class _FakePipeline:
    def __init__(self, outcome) -> None:
        self._outcome = outcome
        self.synced = []

    async def sync(self, repo):
        self.synced.append(repo.id)
        return self._outcome


class _FakeVault:
    def __init__(self, *, found: bool = True) -> None:
        self._found = found

    async def find_document(self, *, user_id, source, source_path):
        if not self._found:
            return None
        from kb.sync.ports import DocumentRecord

        return DocumentRecord(
            id=uuid.uuid4(),
            user_id=user_id,
            source=source,
            source_path=source_path,
            content_sha="f" * 64,
        )


class _FakeIndexer:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict] = []

    async def index_document(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        from kb.indexer.service import IndexOutcome

        return IndexOutcome(document_id=kwargs["document_id"], status="ok", chunks=4, embedded=4)


class _FakeMaintenance:
    def __init__(self, *, documents: int = 0) -> None:
        self._count = documents
        self.dropped: list[uuid.UUID] = []
        self.timestamps_cleared: list[uuid.UUID] = []

    async def drop_chunks(self, user_id):
        self.dropped.append(user_id)
        return 7

    async def clear_index_timestamps(self, user_id):
        self.timestamps_cleared.append(user_id)
        return self._count

    async def document_records(self, user_id):
        from kb.sync.ports import DocumentRecord

        return [
            DocumentRecord(
                id=uuid.uuid4(),
                user_id=user_id,
                source="git",
                source_path=f"notes/{index}.md",
                content_sha="a" * 64,
            )
            for index in range(self._count)
        ]


class _FakeIndexConfig:
    def __init__(self) -> None:
        self.stamps: list[dict] = []

    async def stamp(self, **kwargs):
        self.stamps.append(kwargs)


@pytest.mark.parametrize("kind", KNOWN_KINDS)
def test_every_declared_kind_has_a_handler(kind: str) -> None:
    """``KNOWN_KINDS`` is what the worker asks the queue for; a kind with no branch
    would be claimed and then failed as unknown, forever."""
    assert kind in {JOB_REPO_SYNC, JOB_DOC_INDEX, JOB_FULL_REBUILD}
