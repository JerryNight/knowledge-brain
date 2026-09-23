"""Unit tests for the sync pipeline — including the ``last_synced_sha`` red line.

Spec §11.1 #2 asks for exactly one thing: simulate a failure part-way through a
batch and assert the sync marker did **not** advance. That test is here, along
with the three short circuits it protects (spec §11.1 #3 and #4).
"""

from __future__ import annotations

import uuid

import pytest

from kb.hashing import sha256_hex
from kb.models.sync_job import JOB_DOC_INDEX
from kb.sync.diff import ChangeEvent
from kb.sync.pipeline import SyncPipeline
from kb.sync.ports import SOURCE_GIT, DocumentRecord, RepoRef
from tests.unit.fakes import FakeEnqueuer, FakeGitClient, FakeVaultStore, document_id

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


def pipeline(git: FakeGitClient, store: FakeVaultStore, enqueuer=None, **kwargs) -> SyncPipeline:
    return SyncPipeline(
        git_factory=lambda _repo: git,
        store=store,
        enqueuer=enqueuer or FakeEnqueuer(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# first sync
# ---------------------------------------------------------------------------


async def test_first_sync_enqueues_every_file_then_advances_the_marker() -> None:
    git = FakeGitClient(files=["a.md", "b.md"], blobs={"a.md": b"A", "b.md": b"B"})
    store = FakeVaultStore()
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, store, enqueuer).sync(repo())

    assert outcome.full_rescan
    assert outcome.added == 2
    assert outcome.jobs_enqueued == 2
    assert [job.payload["source_path"] for job in enqueuer.jobs] == ["a.md", "b.md"]
    assert all(job.kind == JOB_DOC_INDEX and job.repo_id == REPO_ID for job in enqueuer.jobs)
    assert store.synced_sha[REPO_ID] == git.head
    assert store.calls[-1] == f"advance_sha:{git.head}"


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
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, FakeVaultStore(), enqueuer, batch_size=2).sync(repo())

    assert [len(batch) for batch in enqueuer.batches] == [2, 2, 1]
    assert outcome.batches == 3


# ---------------------------------------------------------------------------
# red line: last_synced_sha advances only after every batch is durable
# ---------------------------------------------------------------------------


async def test_marker_does_not_advance_when_a_batch_fails() -> None:
    files = [f"n{i}.md" for i in range(3)]
    git = FakeGitClient(files=files, blobs={name: name.encode() for name in files})
    store = FakeVaultStore()
    enqueuer = FakeEnqueuer(fail_on_batch=1)  # second batch blows up

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await pipeline(git, store, enqueuer, batch_size=2).sync(repo())

    # The marker is the diff starting point. Advancing it here would make the
    # third file invisible to every future sync.
    assert store.synced_sha == {}
    assert "advance_sha" not in " ".join(store.calls)
    # The first batch is durable and stays queued.
    assert len(enqueuer.batches[0]) == 2


async def test_a_rerun_after_a_failure_completes_the_sync() -> None:
    files = [f"n{i}.md" for i in range(3)]
    git = FakeGitClient(files=files, blobs={name: name.encode() for name in files})
    store = FakeVaultStore()

    with pytest.raises(RuntimeError):
        await pipeline(git, store, FakeEnqueuer(fail_on_batch=1), batch_size=2).sync(repo())

    enqueuer = FakeEnqueuer()
    outcome = await pipeline(git, store, enqueuer, batch_size=2).sync(repo())

    assert outcome.jobs_enqueued == 3
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
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, store, enqueuer).sync(repo(last_synced_sha=OLD_SHA))

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
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, store, enqueuer).sync(repo(last_synced_sha=OLD_SHA))

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
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, store, enqueuer).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.added == 1
    assert enqueuer.jobs[0].payload["source_path"] == "new/a.md"
    assert "delete:old/a.md" in store.calls


async def test_modified_file_is_enqueued_with_its_new_hash() -> None:
    git = FakeGitClient(files=[], events=[ChangeEvent(status="M", path="a.md")], blobs={"a.md": b"new content"})
    store = FakeVaultStore([document("a.md", b"old content")])
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, store, enqueuer).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.modified == 1
    assert enqueuer.jobs[0].payload["content_sha"] == sha256_hex(b"new content")


async def test_deleted_file_is_removed_without_a_job() -> None:
    git = FakeGitClient(files=[], events=[ChangeEvent(status="D", path="a.md")])
    store = FakeVaultStore([document("a.md", b"body")])
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, store, enqueuer).sync(repo(last_synced_sha=OLD_SHA))

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
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, store, enqueuer).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.skipped == 1
    assert outcome.jobs_enqueued == 0
    assert "delete:.obsidian/workspace.json" in store.calls


async def test_kbignore_itself_is_never_indexed() -> None:
    """A config file is not content, even when the rescan sees it in the tree."""
    git = FakeGitClient(files=[".kbignore"], events=[ChangeEvent(status="M", path=".kbignore")])
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, FakeVaultStore(), enqueuer).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.full_rescan
    assert outcome.skipped == 1
    assert outcome.jobs_enqueued == 0


async def test_oversized_files_are_skipped_without_fetching_them() -> None:
    git = FakeGitClient(files=[], events=[ChangeEvent(status="A", path="big.pdf")], blobs={"big.pdf": b"x" * 100})
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, FakeVaultStore(), enqueuer, max_file_size_bytes=10).sync(
        repo(last_synced_sha=OLD_SHA)
    )

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
    enqueuer = FakeEnqueuer()

    outcome = await pipeline(git, store, enqueuer).sync(repo(last_synced_sha=OLD_SHA))

    assert outcome.full_rescan
    # drafts/ is still ignored, so its document is removed...
    assert "delete:drafts/old.md" in store.calls
    # ...the new file is indexed, and the untouched one is not re-read or
    # re-enqueued, because the rescan compares against what is already indexed.
    assert [job.payload["source_path"] for job in enqueuer.jobs] == ["newly/allowed.md"]
    assert git.blob_reads == ["newly/allowed.md"]


async def test_rescan_skips_ignored_files_entirely() -> None:
    git = FakeGitClient(
        files=["notes/a.md", "drafts/b.md"],
        events=[ChangeEvent(status="M", path=".kbignore")],
        blobs={"notes/a.md": b"A", "drafts/b.md": b"B"},
        kbignore="drafts/\n",
    )
    enqueuer = FakeEnqueuer()

    await pipeline(git, FakeVaultStore(), enqueuer).sync(repo(last_synced_sha=OLD_SHA))

    assert [job.payload["source_path"] for job in enqueuer.jobs] == ["notes/a.md"]
