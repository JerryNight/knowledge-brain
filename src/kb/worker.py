"""Worker — the queue consumer (spec §4 / §6 / §9).

One loop: claim a job, dispatch on its kind, ack or fail. Three kinds, and the
reason each one is shaped the way it is:

* ``repo_sync`` — runs the sync pipeline for one repository. The pipeline is
  where the ``last_synced_sha`` red line lives, so this handler does nothing
  except hand it a repository and let it run; it must not advance any marker
  itself.
* ``doc_index`` — converts, chunks and embeds one document. ``ack`` happens only
  after all the writes commit, so a crash leaves the job claimable again and the
  level-3 short circuit makes the retry nearly free.
* ``full_rebuild`` — drops a tenant's chunks and re-enqueues every document.
  Deliberately expressed as *enqueue*, not as an inline loop over documents: a
  rebuild of a large vault is thousands of individually-retryable jobs (spec §6's
  batching argument), and a single giant job would lose all of that on a crash.

**Failure handling mirrors spec §6 exactly.** A failed job goes back to the queue
with exponential backoff; the queue's attempt ceiling stops a permanently broken
repository from retrying forever. Nothing in this module catches broadly enough to
swallow a programming error into a silent retry — that would turn a bug into a
stuck job, which is worse than a traceback.
"""

from __future__ import annotations

import asyncio
import logging

from kb.db.adapters.blobs import default_git_factory
from kb.models.sync_job import JOB_DOC_INDEX, JOB_FULL_REBUILD, JOB_REPO_SYNC
from kb.queue.base import Job, JobSpec
from kb.sync.pipeline import SyncPipeline
from kb.sync.ports import JobPayload
from kb.wiring import Services, build_services

LOGGER = logging.getLogger(__name__)

# Job kinds this worker knows how to handle.
KNOWN_KINDS = (JOB_REPO_SYNC, JOB_DOC_INDEX, JOB_FULL_REBUILD)

# Jobs enqueued per transaction when a rebuild fans its work out (spec §6's
# 500-file batch size, reused).
REBUILD_BATCH_SIZE = 500


class Worker:
    def __init__(self, services: Services, *, poll_interval_seconds: float | None = None) -> None:
        self._services = services
        settings = services.settings
        self._poll_interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else float(settings.sync_poll_interval_seconds)
        )
        # Built from ``services.settings``, not from ``get_settings()``: the
        # process already has one settings object and re-reading the environment
        # would let the worker's batch size and the API's disagree.
        # ``unit_of_work``, not the raw queue: the pipeline writes a document row
        # and its indexing job in one transaction, and only the scope can do
        # both. Handing it the queue instead would let a row land without a job,
        # and the next sync would then short-circuit that file as unchanged.
        self._pipeline = SyncPipeline(
            git_factory=default_git_factory(workdir_root=settings.git_workdir_root),
            store=services.vault,
            unit_of_work=services.unit_of_work,
            batch_size=settings.sync_batch_size,
            max_file_size_bytes=settings.max_file_size_bytes,
        )

    # -- main loop ----------------------------------------------------------

    async def run_forever(self) -> None:
        """Consume until cancelled. Sleeps only when the queue came back empty."""
        LOGGER.info("worker started; consuming %s", ", ".join(KNOWN_KINDS))
        while True:
            try:
                worked = await self.run_once()
            except asyncio.CancelledError:
                LOGGER.info("worker cancelled; shutting down")
                raise
            if not worked:
                await asyncio.sleep(self._poll_interval)

    async def run_once(self) -> bool:
        """Claim and process at most one job. Returns whether anything was claimed.

        Separated from the loop so tests can drive the worker deterministically —
        one job per call, no sleeping.
        """
        job = await self._services.queue.dequeue(kinds=KNOWN_KINDS)
        if job is None:
            return False
        try:
            result = await self._dispatch(job)
        except Exception as exc:  # noqa: BLE001 — any failure is the queue's business
            LOGGER.exception("job %s (%s) failed", job.id, job.kind)
            await self._services.queue.fail(job, error=f"{type(exc).__name__}: {exc}")
            return True
        await self._services.queue.ack(job, result=result)
        return True

    async def _dispatch(self, job: Job) -> dict:
        if job.kind == JOB_REPO_SYNC:
            return await self._sync_repos(job)
        if job.kind == JOB_DOC_INDEX:
            return await self._index_document(job)
        if job.kind == JOB_FULL_REBUILD:
            return await self._rebuild(job)
        raise ValueError(f"unknown job kind {job.kind!r}")

    # -- handlers -----------------------------------------------------------

    async def _sync_repos(self, job: Job) -> dict:
        """Sync the job's repository, or every enabled one when it names none."""
        repos = await self._services.repos.repos_for_sync(job.user_id)
        if job.repo_id is not None:
            repos = [repo for repo in repos if repo.id == job.repo_id]
            if not repos:
                return {"skipped": "repository is gone or sync is disabled"}

        outcomes = []
        for repo in repos:
            outcome = await self._pipeline.sync(repo)
            outcomes.append(
                {
                    "repo_id": str(outcome.repo_id),
                    "unchanged": outcome.unchanged,
                    "full_rescan": outcome.full_rescan,
                    "added": outcome.added,
                    "modified": outcome.modified,
                    "renamed": outcome.renamed,
                    "deleted": outcome.deleted,
                    "skipped": outcome.skipped,
                    "oversized": outcome.oversized,
                    "jobs_enqueued": outcome.jobs_enqueued,
                }
            )
        return {"repos": outcomes}

    async def _index_document(self, job: Job) -> dict:
        """Index the document a job refers to.

        The job carries ``content_sha`` rather than the bytes: the indexer re-reads
        the source and compares, so a job that was queued for a version that has
        since changed is skipped instead of writing stale chunks. That is what
        makes a retry safe and a double-click cheap (spec §6).
        """
        payload = JobPayload.from_mapping(job.payload)
        record = await self._services.vault.find_document(
            user_id=job.user_id, source=payload.source, source_path=payload.source_path
        )
        if record is None:
            return {"skipped": "document no longer exists"}

        outcome = await self._services.indexer.index_document(
            user_id=job.user_id,
            document_id=record.id,
            source=payload.source,
            source_path=payload.source_path,
            content_sha=payload.content_sha,
            mime=payload.mime,
        )
        return {
            "document_id": str(outcome.document_id) if outcome.document_id else None,
            "status": outcome.status,
            "chunks": outcome.chunks,
            "embedded": outcome.embedded,
            "reused": outcome.reused,
            "skipped": outcome.skipped,
            "reason": outcome.reason,
        }

    async def _rebuild(self, job: Job) -> dict:
        """Drop the tenant's chunks and re-enqueue every document (spec §9).

        Also refreshes the ``index_config`` stamp. A rebuild is the operation that
        makes the configured model and the stored index agree again, so it is the
        one place the stamp is legitimately rewritten.
        """
        services = self._services
        removed = await services.index_maintenance.drop_chunks(job.user_id)
        await services.index_maintenance.clear_index_timestamps(job.user_id)
        records = await services.index_maintenance.document_records(job.user_id)

        enqueued = 0
        for start in range(0, len(records), REBUILD_BATCH_SIZE):
            batch = records[start : start + REBUILD_BATCH_SIZE]
            enqueued += await services.queue.enqueue_batch(
                [
                    JobSpec(
                        user_id=job.user_id,
                        kind=JOB_DOC_INDEX,
                        repo_id=None,
                        payload=JobPayload(
                            source=record.source,
                            source_path=record.source_path,
                            content_sha=record.content_sha,
                            size_bytes=record.size_bytes,
                        ).as_dict(),
                    )
                    for record in batch
                ]
            )

        settings = services.settings
        await services.index_config.stamp(
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            chunker_version=settings.chunker_version,
        )
        LOGGER.info("rebuild: dropped %d chunks and re-enqueued %d documents", removed, enqueued)
        return {"chunks_dropped": removed, "documents_requeued": enqueued}


__all__ = ["KNOWN_KINDS", "REBUILD_BATCH_SIZE", "Worker"]


def main() -> None:
    """``python -m kb.worker`` — the worker's process entry point.

    Equivalent to ``kb worker``; both exist because a container image and a
    developer's shell tend to want different spellings of the same thing.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(Worker(build_services()).run_forever())


if __name__ == "__main__":  # pragma: no cover - process entry
    main()