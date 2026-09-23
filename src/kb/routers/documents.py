"""``POST /api/documents`` — the manual upload channel (spec §2 / §7 约束 6).

The server receives the **original** file and converts it itself. Letting a
client convert first would put the conversion logic in two places and take away
the server's ability to re-convert later — which is exactly what a parser swap
would require, and it would then mean asking every user to re-upload.

Order of operations, and why:

1. **Store the bytes first.** Spec §5 makes the index a derivative, but only of
   things that still exist: an upload whose original was never persisted cannot
   be rebuilt, so the bytes land on disk before the database row is written.
2. **Then the unit of work.** ``documents`` row and ``doc_index`` job commit
   together, so a job can never reference an upload that was rolled back.
3. **Then convert — in the worker.** Conversion is seconds-to-tens-of-seconds for
   a PDF. Doing it here would make the upload request's latency a function of the
   file's parse difficulty.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel

from kb.db.adapters.blobs import UnsafeBlobPath
from kb.deps import ServicesDep, UserIdDep
from kb.upload.service import UploadRequest

router = APIRouter(prefix="/api/documents", tags=["documents"])


class UploadResponse(BaseModel):
    path: str
    content_sha: str
    document_id: str | None = None
    job_id: int | None = None
    skipped: bool = False
    replaced: bool = False
    message: str


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def upload_document(
    request: Request,
    services: ServicesDep,
    user_id: UserIdDep,
    path: str = Query(
        min_length=1,
        description="File name inside the upload namespace, e.g. `报告.pdf`. May include subdirectories.",
    ),
) -> UploadResponse:
    """Accept raw bytes as the request body.

    Raw body rather than multipart: the filename is already a query parameter, so
    a multipart envelope would carry no extra information while adding a parser
    and a dependency. ``Content-Type`` is taken as the MIME hint; the extension on
    ``path`` remains authoritative for converter selection (spec §7 边界).
    """
    blob = await request.body()
    if not blob:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="empty body")

    max_bytes = services.settings.max_file_size_bytes
    if len(blob) > max_bytes:
        # Rejected at the edge. The conversion layer would mark it `unsupported`
        # anyway, but refusing here means an oversized upload never occupies disk
        # or a worker.
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"file is {len(blob)} bytes, over the {max_bytes} byte limit",
        )

    try:
        await services.blob_store.put(user_id=user_id, name=path, blob=blob)
    except UnsafeBlobPath as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    outcome = await services.upload.upload(
        UploadRequest(
            user_id=user_id,
            filename=path,
            blob=blob,
            mime=request.headers.get("content-type"),
        )
    )
    if outcome.skipped:
        message = "内容与已有版本完全一致，未重新索引。"
    elif outcome.replaced:
        message = "已替换同名文档，索引任务已入队。"
    else:
        message = "已接收，索引任务已入队。"
    return UploadResponse(
        path=path,
        content_sha=outcome.content_sha,
        document_id=str(outcome.document_id) if outcome.document_id else None,
        job_id=outcome.job_id,
        skipped=outcome.skipped,
        replaced=outcome.replaced,
        message=message,
    )


__all__ = ["router"]
