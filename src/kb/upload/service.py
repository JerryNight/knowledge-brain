"""Manual upload channel (spec §2).

Deliberately separate from the git channel by namespace: uploads are stored with
``source='upload'``, so a vault file and an upload can share a name without
either overwriting the other (spec §5 ③).

The whole write happens inside one unit of work, which is the guarantee spec §4
singles out: the document row and its ``doc_index`` job commit together, so a
job can never point at a document that was rolled back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from kb.hashing import sha256_hex
from kb.models.sync_job import JOB_DOC_INDEX
from kb.queue.base import JobSpec
from kb.sync.ports import SOURCE_UPLOAD, JobPayload, NewDocument, UnitOfWork


@dataclass(frozen=True, slots=True)
class UploadRequest:
    user_id: uuid.UUID
    filename: str
    blob: bytes
    mime: str | None = None


@dataclass(frozen=True, slots=True)
class UploadOutcome:
    content_sha: str
    document_id: uuid.UUID | None = None
    job_id: int | None = None
    skipped: bool = False
    replaced: bool = False


class UploadService:
    def __init__(self, unit_of_work: UnitOfWork) -> None:
        self._uow = unit_of_work

    async def upload(self, request: UploadRequest) -> UploadOutcome:
        content_sha = sha256_hex(request.blob)

        async with self._uow.begin(request.user_id) as scope:
            existing = await scope.find_document(
                user_id=request.user_id, source=SOURCE_UPLOAD, source_path=request.filename
            )
            if existing is not None and existing.content_sha == content_sha:
                # Same bytes under the same name: nothing to index. Reusing the
                # git channel's level-1 short circuit keeps the two paths
                # behaving alike.
                return UploadOutcome(content_sha=content_sha, document_id=existing.id, skipped=True)

            record = await scope.upsert_document(
                NewDocument(
                    user_id=request.user_id,
                    source=SOURCE_UPLOAD,
                    source_path=request.filename,
                    content_sha=content_sha,
                    mime=request.mime,
                    size_bytes=len(request.blob),
                )
            )
            job_id = await scope.enqueue(
                JobSpec(
                    user_id=request.user_id,
                    kind=JOB_DOC_INDEX,
                    payload=JobPayload(
                        source=SOURCE_UPLOAD,
                        source_path=request.filename,
                        content_sha=content_sha,
                        size_bytes=len(request.blob),
                        mime=request.mime,
                    ).as_dict(),
                )
            )

        return UploadOutcome(
            content_sha=content_sha,
            document_id=record.id,
            job_id=job_id,
            replaced=existing is not None,
        )
