"""Unit tests for the sync pipeline — including the ``last_synced_sha`` red line.

Spec §11.1 #2 asks for exactly one thing: simulate a failure part-way through a
batch and assert the sync marker did **not** advance. That test is here, along
with the three short circuits it protects (spec §11.1 #3 and #4), and the
pairing that makes the pipeline's output usable at all: every ``doc_index`` job
it queues has its ``documents`` row beside it, committed in the same transaction.

That last property was missing. The pipeline used to enqueue jobs and never write
the row, so the indexer — which only ever *updates* a document — found nothing,
acked each job as ``"document no longer exists"``, and left the corpus empty
without a single error. ``test_every_enqueued_job_has_a_document_row`` is the
guard for it.
"""

from __future__ import annotations

import uuid

import pytest

from kb.hashing import sha256_hex
from kb.models.sync_job import JOB_DOC_INDEX
from kb.sync.diff import ChangeEvent
from kb.sync.pipeline import SyncPipeline
from kb.sync.ports import SOURCE_GIT, DocumentRecord, RepoRef
from tests.unit.fakes import FakeGitClient, FakeUnitOfWork, FakeVaultStore, document_id

USER = uuid.uuid4()
REPO_ID = uuid.uuid4()
OLD_SHA = "old0000000"


def repo(last_synced_sha: str | None = None) -> RepoRef:
    return RepoRef(id=REPO_ID, user_id=USER, url="git@example.test:vault.git", last_synced_sha=last_synced_sha)


def document(path: str, content: bytes) -> DocumentRecord:
    return DocumentRecord(
        id=document_id(),
        user_id=USER,
        source=SOURCE_GIT,
        source_path=path,
        content_sha=sha256_hex(content),
    )


def pipeline(git: FakeGitClient, store: FakeVaultStore, uow: FakeUnitOfWork | None = None, **kwargs) -> SyncPipeline:
    return SyncPipeline(
        git_factory=lambda _repo: git,
        store=store,
        unit_of_work=uow or FakeUnitOfWork(store),
        **kwargs,
    )


def batch_sizes(uow: FakeUnitOfWork) -> list[int]:
    return [len(scope.enqueued_in_scope) for scope in uow.scopes]


# ---------------------------------------------------------------------------
# first sync
# ---------------------------------------------------------------------------


async def test_first_sync_enqueues_every_file_then_advances_the_marker() -> None:
    git = FakeGitClient(files=["a.md", "b.md"], blobs={"a.md": b"A", "b.md": b"B"})
    store = FakeVaultStore()
    uow = FakeUnitOfWork(store)

    outcome = await pipeline(git, store, uow).sync(repo())

    assert outcome.full_rescan
    assert outcome.added == 2
    assert outcome.jobs_enqueued == 2
    assert [job.payload["source_path"] for job in uow.jobs] == ["a.md", "b.md"]
    assert all(job.kind == JOB_DOC_INDEX and job.repo_id == REPO_ID for job in uow.jobs)
    assert store.synced_sha[REPO_ID] == git.head
    assert store.calls[-1] == f"advance_sha:{git.head}"


async def test_every_enqueued_job_has_a_document_row() -> None:
    """The indexer only updates a document; it cannot create one.

    Without the row, ``Worker._index_document`` finds nothing, reports the job as
    ``skipped`` and acks it — a silent, complete failure of ingestion. The row is
    the pipeline's responsibility and is written here, once per job.
    """
    git = FakeGitClient(files=["a.md", "b.md"], blobs={"a.md": b"A", "b.md": b"B"})
    store = FakeVaultStore()
    uow = FakeUnitOfWork(store)

    await pipeline(git, store, uow).sync(repo())

    indexed_paths = {path for (_, _, path) in store.documents}
    queued_paths = {job.payload["source_path"] for job in uow.jobs}
    assert indexed_paths == {"a.md", "b.md"}
    assert queued_paths == indexed_paths, "a job without its row would ack itself away"
    for (_, source, _), record in store.documents.items():
        assert source == SOURCE_GIT
        assert record.content_sha == sha256_hex(b"A" if record.source_path == "a.md" else b"B")


async def test_the_row_and_the_job_commit_in_the_same_scope() -> None:
    """Both exist is not enough; they must land together.

    A row that commits without its job would match ``content_sha`` on the next
    sync, take the level-1 short circuit and be reported as ``skipped`` — the file
    would never be indexed, and nothing would ever try again.
    """
    files = ["a.md", "b.md", "c.md"]
    git = FakeGitClient(files=files, blobs={name: name.encode() for name in files})
    uow = FakeUnitOfWork()

    await pipeline(git, FakeVaultStore(), uow, batch_size=2).sync(repo())

    assert len(uow.scopes) == 2
    for scope in uow.scopes:
        assert [document.source_path for document in scope.staged_documents] == [
            job.payload["source_path"] for job in scope.enqueued_in_scope
        ]


async def test_unchanged_head_ends_the_run_immediately() -> None:
    git = FakeGitClient(head=OLD_SHA)
    store = FakeVaultStore()

    outcome = await pipeline(git, store).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.unchanged
    assert store.calls == []
    assert git.diff_calls == []


async def test_jobs_are_enqueued_in_batches() -> None:
    """A first sync of a large vault must land progressively, not in one commit."""
    files = [f"n{i}.md" for i in range(5)]
    git = FakeGitClient(files=files, blobs={name: name.encode() for name in files})
    uow = FakeUnitOfWork()

    outcome = await pipeline(git, FakeVaultStore(), uow, batch_size=2).sync(repo())

    assert batch_sizes(uow) == [2, 2, 1]
    assert outcome.batches == 3


# ---------------------------------------------------------------------------
# red line: last_synced_sha advances only after every batch is durable
# ---------------------------------------------------------------------------


async def test_marker_does_not_advance_when_a_batch_fails() -> None:
    files = [f"n{i}.md" for i in range(3)]
    git = FakeGitClient(files=files, blobs={name: name.encode() for name in files})
    store = FakeVaultStore()
    uow = FakeUnitOfWork(store, fail_on_scope=1)  # second batch blows up

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await pipeline(git, store, uow, batch_size=2).sync(repo())

    # The marker is the diff starting point. Advancing it here would make the
    # third file invisible to every future sync.
    assert store.synced_sha == {}
    assert "advance_sha" not in " ".join(store.calls)
    # The first batch is durable — rows and jobs alike.
    assert batch_sizes(uow) == [2, 0]
    assert len(uow.jobs) == 2


async def test_a_failed_batch_leaves_neither_row_nor_job() -> None:
    """Rollback, not half-write. A row surviving its failed batch is the exact
    state the next sync misreads as "already indexed"."""
    files = [f"n{i}.md" for i in range(3)]
    git = FakeGitClient(files=files, blobs={name: name.encode() for name in files})
    store = FakeVaultStore()
    uow = FakeUnitOfWork(store, fail_on_scope=1)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await pipeline(git, store, uow, batch_size=2).sync(repo())

    indexed = {path for (_, _, path) in store.documents}
    assert indexed == {"n0.md", "n1.md"}
    # The third file was staged inside the scope that raised, and went no further.
    assert [document.source_path for document in uow.scopes[1].staged_documents] == ["n2.md"]
    assert not uow.scopes[1].committed


async def test_a_rerun_after_a_failure_finishes_only_what_is_left() -> None:
    """The committed batch is still queued, so the rerun must not re-queue it.

    Re-enqueueing would be duplicate work and, worse, would re-embed documents
    that are already on their way through the indexer.
    """
    files = [f"n{i}.md" for i in range(3)]
    git = FakeGitClient(files=files, blobs={name: name.encode() for name in files})
    store = FakeVaultStore()

    with pytest.raises(RuntimeError):
        await pipeline(git, store, FakeUnitOfWork(store, fail_on_scope=1), batch_size=2).sync(repo())

    uow = FakeUnitOfWork(store)
    outcome = await pipeline(git, store, uow, batch_size=2).sync(repo())

    assert outcome.jobs_enqueued == 1
    assert outcome.skipped == 2
    assert [job.payload["source_path"] for job in uow.jobs] == ["n2.md"]
    assert store.synced_sha[REPO_ID] == git.head


# ---------------------------------------------------------------------------
# short circuits
# ---------------------------------------------------------------------------


async def test_identical_content_is_skipped_entirely() -> None:
    """Level 1: a touch or whitespace-free edit costs one hash, nothing more."""
    git = FakeGitClient(
        files=[],
        events=[ChangeEvent(status="M", path="a.md")],
        blobs={"a.md": b"unchanged"},
    )
    store = FakeVaultStore([document("a.md", b"unchanged")])
    uow = FakeUnitOfWork(store)

    outcome = await pipeline(git, store, uow).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.skipped == 1
    assert outcome.jobs_enqueued == 0
    assert store.synced_sha[REPO_ID] == git.head


async def test_rename_without_content_change_updates_the_path_only() -> None:
    """Spec §11.1 #4: a pure rename must not cost a single embedding."""
    git = FakeGitClient(
        files=[],
        events=[ChangeEvent(status="R", path="new/a.md", old_path="old/a.md", old_blob="same", new_blob="same")],
    )
    store = FakeVaultStore([document("old/a.md", b"body")])
    uow = FakeUnitOfWork(store)

    outcome = await pipeline(git, store, uow).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.renamed == 1
    assert outcome.jobs_enqueued == 0
    assert git.blob_reads == []  # the blob was never even fetched
    assert (USER, SOURCE_GIT, "new/a.md") in store.documents


async def test_rename_with_an_edit_reindexes_and_drops_the_old_row() -> None:
    git = FakeGitClient(
        files=[],
        events=[ChangeEvent(status="R", path="new/a.md", old_path="old/a.md", old_blob="before", new_blob="after")],
        blobs={"new/a.md": b"new body"},
    )
    store = FakeVaultStore([document("old/a.md", b"old body")])
    uow = FakeUnitOfWork(store)

    outcome = await pipeline(git, store, uow).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.added == 1
    assert uow.jobs[0].payload["source_path"] == "new/a.md"
    assert "delete:old/a.md" in store.calls


async def test_modified_file_is_enqueued_with_its_new_hash() -> None:
    git = FakeGitClient(files=[], events=[ChangeEvent(status="M", path="a.md")], blobs={"a.md": b"new content"})
    store = FakeVaultStore([document("a.md", b"old content")])
    uow = FakeUnitOfWork(store)

    outcome = await pipeline(git, store, uow).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.modified == 1
    assert uow.jobs[0].payload["content_sha"] == sha256_hex(b"new content")


async def test_a_modified_file_has_its_row_refreshed_to_the_new_hash() -> None:
    """The row and the job must agree on the hash, or the indexer will reject the
    job as stale ("content changed since the job was queued") and skip the file."""
    git = FakeGitClient(files=[], events=[ChangeEvent(status="M", path="a.md")], blobs={"a.md": b"new content"})
    store = FakeVaultStore([document("a.md", b"old content")])

    await pipeline(git, store, FakeUnitOfWork(store)).sync(repo(last_synced_sha=OLD_SHA))

    assert store.documents[(USER, SOURCE_GIT, "a.md")].content_sha == sha256_hex(b"new content")


async def test_deleted_file_is_removed_without_a_job() -> None:
    git = FakeGitClient(files=[], events=[ChangeEvent(status="D", path="a.md")])
    store = FakeVaultStore([document("a.md", b"body")])
    uow = FakeUnitOfWork(store)

    outcome = await pipeline(git, store, uow).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.deleted == 1
    assert outcome.jobs_enqueued == 0
    assert (USER, SOURCE_GIT, "a.md") not in store.documents


async def test_ignored_paths_are_skipped_and_purged() -> None:
    git = FakeGitClient(files=[], events=[ChangeEvent(status="A", path=".obsidian/workspace.json")])
    store = FakeVaultStore(
        [
            DocumentRecord(
                id=document_id(),
                user_id=USER,
                source=SOURCE_GIT,
                source_path=".obsidian/workspace.json",
                content_sha="x",
            )
        ]
    )
    uow = FakeUnitOfWork(store)

    outcome = await pipeline(git, store, uow).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.skipped == 1
    assert outcome.jobs_enqueued == 0
    assert "delete:.obsidian/workspace.json" in store.calls


async def test_kbignore_itself_is_never_indexed() -> None:
    """A config file is not content, even when the rescan sees it in the tree."""
    git = FakeGitClient(files=[".kbignore"], events=[ChangeEvent(status="M", path=".kbignore")])
    uow = FakeUnitOfWork()

    outcome = await pipeline(git, FakeVaultStore(), uow).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.full_rescan
    assert outcome.skipped == 1
    assert outcome.jobs_enqueued == 0
    assert (USER, SOURCE_GIT, ".kbignore") not in uow.store.documents


async def test_oversized_files_are_skipped_without_fetching_them() -> None:
    git = FakeGitClient(files=[], events=[ChangeEvent(status="A", path="big.pdf")], blobs={"big.pdf": b"x" * 100})
    uow = FakeUnitOfWork()

    outcome = await pipeline(git, FakeVaultStore(), uow, max_file_size_bytes=10).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.oversized == 1
    assert outcome.jobs_enqueued == 0
    assert git.blob_reads == []


# ---------------------------------------------------------------------------
# .kbignore change → full comparison in both directions
# ---------------------------------------------------------------------------


async def test_kbignore_edit_triggers_a_full_rescan() -> None:
    """Widening the rules must index newly-allowed files; narrowing must purge.

    Without this, "I un-ignored it and it still doesn't show up" (spec §6).
    """
    git = FakeGitClient(
        files=["kept.md", "newly/allowed.md"],
        events=[ChangeEvent(status="M", path=".kbignore")],
        blobs={"kept.md": b"kept unchanged", "newly/allowed.md": b"fresh"},
        kbignore="drafts/\n",
    )
    store = FakeVaultStore([document("kept.md", b"kept unchanged"), document("drafts/old.md", b"gone")])
    uow = FakeUnitOfWork(store)

    outcome = await pipeline(git, store, uow).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.full_rescan
    # drafts/ is still ignored, so its document is removed...
    assert "delete:drafts/old.md" in store.calls
    # ...the new file is indexed, and the untouched one is not re-read or
    # re-enqueued, because the rescan compares against what is already indexed.
    assert [job.payload["source_path"] for job in uow.jobs] == ["newly/allowed.md"]
    assert git.blob_reads == ["newly/allowed.md"]


async def test_rescan_skips_ignored_files_entirely() -> None:
    git = FakeGitClient(
        files=["notes/a.md", "drafts/b.md"],
        events=[ChangeEvent(status="M", path=".kbignore")],
        blobs={"notes/a.md": b"A", "drafts/b.md": b"B"},
        kbignore="drafts/\n",
    )
    uow = FakeUnitOfWork()

    await pipeline(git, FakeVaultStore(), uow).sync(repo(last_synced_sha=OLD_SHA))

    assert [job.payload["source_path"] for job in uow.jobs] == ["notes/a.md"]
