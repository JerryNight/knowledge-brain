"""Sync pipeline — the three-level short circuit and the ``last_synced_sha`` rule.

The flow for one repository (spec §6):

1. fetch, resolve the branch head; an unchanged SHA ends the run immediately;
2. first sync (no ``last_synced_sha``) or a ``.kbignore`` edit → full comparison
   against the tree, otherwise a diff between the two commits;
3. per change: apply the short circuits, enqueue a ``doc_index`` job for what is
   genuinely new;
4. **advance ``last_synced_sha`` only after every batch has been enqueued.**

Step 4 is the second red line in spec §11.1. Advancing early is the classic
silent data-loss bug in a sync system: the next diff starts from a commit whose
files were never processed, and they are never seen again. It is safe to advance
once the jobs are *durable* rather than *done*, because each job carries its own
path and payload and is retried independently — a crashed worker resumes from the
queue, not from the diff.

The three short circuits, and what each one saves:

======================  ==========================================
``content_sha`` unchanged   the whole file, including conversion — this is
                            what makes a ``touch`` or a pure rename free
rename with equal blob ids  the blob fetch entirely: git already proved the
                            content did not change
``converted_sha`` cached    conversion only (applied by the indexer)
======================  ==========================================
The fourth level — same ``(document_id, ordinal)`` text means no re-embedding —
belongs to the indexer, which is the only code that can see chunk text.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from kb.hashing import sha256_hex
from kb.models.sync_job import JOB_DOC_INDEX
from kb.queue.base import JobSpec
from kb.sync.diff import (
    STATUS_ADDED,
    STATUS_DELETED,
    STATUS_MODIFIED,
    STATUS_RENAMED,
    STATUS_TYPECHANGE,
    ChangeEvent,
    additions_from_paths,
)
from kb.sync.git import GitClient
from kb.sync.ignore import KBIGNORE_FILENAME, IgnoreRules, needs_full_rescan, parse_kbignore
from kb.sync.ports import SOURCE_GIT, JobEnqueuer, JobPayload, RepoRef, VaultStore

LOGGER = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500


@dataclass(slots=True)
class SyncOutcome:
    """What one sync run did. Returned rather than logged, so tests can assert."""

    repo_id: uuid.UUID
    unchanged: bool = False
    full_rescan: bool = False
    previous_sha: str | None = None
    new_sha: str | None = None
    added: int = 0
    modified: int = 0
    renamed: int = 0
    deleted: int = 0
    skipped: int = 0
    oversized: int = 0
    jobs_enqueued: int = 0
    batches: int = 0

    @property
    def changed(self) -> int:
        return self.added + self.modified + self.renamed + self.deleted


class SyncPipeline:
    def __init__(
        self,
        *,
        git_factory,
        store: VaultStore,
        enqueuer: JobEnqueuer,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_file_size_bytes: int | None = None,
    ) -> None:
        self._git_factory = git_factory
        self._store = store
        self._enqueuer = enqueuer
        self._batch_size = max(1, batch_size)
        self._max_file_size_bytes = max_file_size_bytes

    @classmethod
    def from_settings(cls, *, git_factory, store, enqueuer) -> SyncPipeline:
        from kb.config import get_settings

        settings = get_settings()
        return cls(
            git_factory=git_factory,
            store=store,
            enqueuer=enqueuer,
            batch_size=settings.sync_batch_size,
            max_file_size_bytes=settings.max_file_size_bytes,
        )

    async def sync(self, repo: RepoRef) -> SyncOutcome:
        git: GitClient = self._git_factory(repo)
        await git.ensure_repo()
        await git.fetch()

        new_sha = await git.remote_head()
        outcome = SyncOutcome(repo_id=repo.id, previous_sha=repo.last_synced_sha, new_sha=new_sha)

        if repo.last_synced_sha == new_sha:
            outcome.unchanged = True
            return outcome

        rules = await self._load_rules(git, new_sha)
        events = await self._collect_events(git, repo, rules, outcome)

        pending: list[JobSpec] = []
        for event in events:
            await self._apply_event(git, repo, rules, event, new_sha, pending, outcome)

        outcome.jobs_enqueued, outcome.batches = await self._enqueue(repo, pending)

        # Red line: reached only when every batch above has committed.
        await self._store.set_last_synced_sha(user_id=repo.user_id, repo_id=repo.id, sha=new_sha)
        LOGGER.info(
            "sync complete repo=%s sha=%s added=%d modified=%d renamed=%d deleted=%d skipped=%d jobs=%d",
            repo.id,
            new_sha[:12],
            outcome.added,
            outcome.modified,
            outcome.renamed,
            outcome.deleted,
            outcome.skipped,
            outcome.jobs_enqueued,
        )
        return outcome

    # -- change collection --------------------------------------------------

    async def _collect_events(
        self,
        git: GitClient,
        repo: RepoRef,
        rules: IgnoreRules,
        outcome: SyncOutcome,
    ) -> list[ChangeEvent]:
        if repo.last_synced_sha is None:
            outcome.full_rescan = True
            return additions_from_paths(rules.keep(await git.list_files(await git.remote_head())))

        events = await git.diff(repo.last_synced_sha, await git.remote_head())
        if not needs_full_rescan(events):
            return events

        # .kbignore changed: what is indexed has to be recomputed against the new
        # rules, in both directions (spec §6 边界情况).
        outcome.full_rescan = True
        return await self._rescan(git, repo, rules)

    async def _rescan(self, git: GitClient, repo: RepoRef, rules: IgnoreRules) -> list[ChangeEvent]:
        head = await git.remote_head()
        wanted = set(rules.keep(await git.list_files(head)))
        existing = set(await self._store.list_document_paths(user_id=repo.user_id, source=SOURCE_GIT))

        events = [ChangeEvent(status=STATUS_ADDED, path=path) for path in sorted(wanted - existing)]
        events.extend(ChangeEvent(status=STATUS_DELETED, path=path) for path in sorted(existing - wanted))
        return events

    # -- per-change processing ---------------------------------------------

    async def _apply_event(
        self,
        git: GitClient,
        repo: RepoRef,
        rules: IgnoreRules,
        event: ChangeEvent,
        revision: str,
        pending: list[JobSpec],
        outcome: SyncOutcome,
    ) -> None:
        if event.path == KBIGNORE_FILENAME:
            # A config file, not content.
            outcome.skipped += 1
            return

        if event.status == STATUS_DELETED:
            await self._store.delete_document(user_id=repo.user_id, source=SOURCE_GIT, source_path=event.path)
            outcome.deleted += 1
            return

        if rules.ignores(event.path):
            # Newly excluded (or always was): make sure nothing stale remains.
            await self._store.delete_document(user_id=repo.user_id, source=SOURCE_GIT, source_path=event.path)
            outcome.skipped += 1
            return

        if event.status == STATUS_RENAMED and event.content_unchanged:
            # git proved the blobs are identical: path update only, no fetch, no
            # conversion, no embedding. This is the case that makes renaming a
            # directory cheap (spec §6).
            renamed = await self._store.rename_document(
                user_id=repo.user_id, source=SOURCE_GIT, old_path=event.old_path or "", new_path=event.path
            )
            outcome.renamed += 1 if renamed else 0
            outcome.skipped += 0 if renamed else 1
            return

        size = await git.blob_size(revision, event.path)
        if size is None:
            outcome.skipped += 1
            return
        if self._max_file_size_bytes is not None and size > self._max_file_size_bytes:
            # Skipped at the source: no point fetching a blob we will refuse.
            outcome.oversized += 1
            return

        blob = await git.read_blob(revision, event.path)
        content_sha = sha256_hex(blob)
        existing = await self._store.find_document(user_id=repo.user_id, source=SOURCE_GIT, source_path=event.path)

        if existing is not None and existing.content_sha == content_sha:
            # Level 1: content identical — rename, touch or whitespace-only
            # change. Nothing to do at all.
            outcome.skipped += 1
            return

        if existing is None and event.old_path:
            # Rename plus edit: the old row is stale and must not survive
            # alongside the new one.
            await self._store.delete_document(user_id=repo.user_id, source=SOURCE_GIT, source_path=event.old_path)

        pending.append(
            JobSpec(
                user_id=repo.user_id,
                kind=JOB_DOC_INDEX,
                repo_id=repo.id,
                payload=JobPayload(
                    source=SOURCE_GIT,
                    source_path=event.path,
                    content_sha=content_sha,
                    size_bytes=size,
                ).as_dict(),
            )
        )
        if existing is None:
            outcome.added += 1
        else:
            outcome.modified += 1

    # -- enqueueing ---------------------------------------------------------

    async def _enqueue(self, repo: RepoRef, specs: list[JobSpec]) -> tuple[int, int]:
        """Enqueue in batches, committing each before starting the next (spec §6).

        A first full sync can be tens of thousands of files; enqueueing them in
        one transaction would be a single enormous commit and a crash would lose
        all of it. Batching makes progress durable, and `documents` records what
        actually landed.
        """
        total = 0
        batches = 0
        for start in range(0, len(specs), self._batch_size):
            batch = specs[start : start + self._batch_size]
            total += await self._enqueuer.enqueue_batch(batch)
            batches += 1
        return total, batches

    async def _load_rules(self, git: GitClient, revision: str) -> IgnoreRules:
        text = await git.read_file_text(revision, KBIGNORE_FILENAME)
        return IgnoreRules.from_lines(parse_kbignore(text) if text else ())


__all__ = [
    "STATUS_ADDED",
    "STATUS_DELETED",
    "STATUS_MODIFIED",
    "STATUS_RENAMED",
    "STATUS_TYPECHANGE",
    "SyncOutcome",
    "SyncPipeline",
]
