"""Unit tests for the upload channel.

The property under test is spec §4's queue guarantee: the document row and its
job are written in the same unit of work, so a queued job can never outlive a
rolled-back document.
"""

from __future__ import annotations

import uuid

from kb.hashing import sha256_hex
from kb.models.sync_job import JOB_DOC_INDEX
from kb.sync.ports import SOURCE_UPLOAD
from kb.upload.service import UploadRequest, UploadService
from tests.unit.fakes import FakeUnitOfWork

USER = uuid.UUID("11111111-1111-1111-1111-111111111111")


def request(blob: bytes, filename: str = "报告.pdf") -> UploadRequest:
    return UploadRequest(user_id=USER, filename=filename, blob=blob, mime="application/pdf")


async def test_upload_writes_the_document_and_its_job_in_one_unit_of_work() -> None:
    uow = FakeUnitOfWork()
    outcome = await UploadService(uow).upload(request(b"pdf bytes"))

    assert outcome.job_id is not None
    assert outcome.content_sha == sha256_hex(b"pdf bytes")
    assert len(uow.scopes) == 1
    scope = uow.scopes[0]
    # Both writes happened inside the same scope — that is the atomicity claim.
    assert scope.enqueued_in_scope and scope.committed
    assert any(call.startswith("upsert:报告.pdf") for call in uow.store.calls)


async def test_upload_job_carries_everything_the_indexer_needs() -> None:
    uow = FakeUnitOfWork()
    await UploadService(uow).upload(request(b"bytes", filename="a.pdf"))

    job = uow.jobs[0]
    assert job.kind == JOB_DOC_INDEX
    assert job.repo_id is None  # uploads belong to no repository
    assert job.payload["source"] == SOURCE_UPLOAD
    assert job.payload["source_path"] == "a.pdf"
    assert job.payload["size_bytes"] == 5


async def test_reuploading_identical_bytes_is_a_no_op() -> None:
    """Level 1 applies to uploads too, so a retried request costs nothing."""
    uow = FakeUnitOfWork()
    service = UploadService(uow)

    first = await service.upload(request(b"same", filename="a.md"))
    second = await service.upload(request(b"same", filename="a.md"))

    assert not first.skipped
    assert second.skipped
    assert second.document_id == first.document_id
    assert len(uow.jobs) == 1


async def test_reuploading_changed_bytes_replaces_the_document() -> None:
    uow = FakeUnitOfWork()
    service = UploadService(uow)

    await service.upload(request(b"v1", filename="a.md"))
    outcome = await service.upload(request(b"v2", filename="a.md"))

    assert outcome.replaced
    assert len(uow.jobs) == 2
    assert uow.jobs[1].payload["content_sha"] == sha256_hex(b"v2")


async def test_uploads_live_in_their_own_namespace() -> None:
    """A file uploaded as ``note.md`` must not collide with the git channel's."""
    uow = FakeUnitOfWork()
    await UploadService(uow).upload(request(b"body", filename="note.md"))

    keys = list(uow.store.documents)
    assert keys[0][1] == SOURCE_UPLOAD
