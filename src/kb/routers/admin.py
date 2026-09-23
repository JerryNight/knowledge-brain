"""Admin endpoints — rebuild and status (spec §9).

Both are tenants of spec §5's founding property: *the index is a derivative*.
Everything searchable can be recomputed from the git repositories and the stored
uploads, so the recovery path for a bad index, a changed chunker, or a different
embedding model is always "clear it and rebuild", never "repair it in place".
``/api/admin/rebuild`` is where that property is cashed in.

Like ``/api/repos/{id}/sync``, rebuild **enqueues**. A full rebuild of a large
vault is an hours-long, thousands-of-jobs operation (spec §6); doing it inside a
request would be a request that times out while leaving the index half-cleared.
The job is durable and resumable instead.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel

from kb.deps import ServicesDep, UserIdDep

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Conversion states that mean "known about, but not searchable" (spec §7).
UNSEARCHABLE_STATUSES = ("failed", "no_text")


class RebuildResponse(BaseModel):
    job_id: int
    message: str


class StatusResponse(BaseModel):
    repos: list[dict]
    jobs: dict[str, int]
    unsearchable: list[dict]
    vector_search_enabled: bool
    index: dict


@router.post("/rebuild", status_code=status.HTTP_202_ACCEPTED)
async def rebuild(services: ServicesDep, user_id: UserIdDep) -> RebuildResponse:
    """Queue a full index rebuild for the calling tenant.

    Scope is one tenant, not the installation: the job carries a ``user_id`` and
    every write it performs is tenant-scoped, so one user's rebuild cannot touch
    another's chunks.
    """
    job_id = await services.repos.request_full_rebuild(user_id)
    return RebuildResponse(
        job_id=job_id,
        message="全量重建任务已入队。worker 会清空该租户的 chunks 并逐个文档重跑，转换结果走缓存不重算。",
    )


@router.get("/status")
async def admin_status(services: ServicesDep, user_id: UserIdDep) -> StatusResponse:
    """Per-repo sync position, queue depth, and the not-searchable file list."""
    repos = await services.repos.list_repos(user_id)
    jobs = await services.queue.counts_for_user(user_id)
    unsearchable = await services.documents.by_status(user_id=user_id, statuses=UNSEARCHABLE_STATUSES)
    stored = await services.index_config.load()
    settings = services.settings
    return StatusResponse(
        repos=[repo.as_dict() for repo in repos],
        jobs=jobs,
        unsearchable=[document.as_dict() for document in unsearchable],
        vector_search_enabled=services.vector_search_enabled,
        index={
            "configured": {
                "provider": settings.embedding_provider,
                "model": settings.embedding_model,
                "dim": settings.embedding_dim,
                "chunker_version": settings.chunker_version,
            },
            "stored": None
            if stored is None
            else {
                "provider": stored.embedding_provider,
                "model": stored.embedding_model,
                "dim": stored.embedding_dim,
                "chunker_version": stored.chunker_version,
            },
        },
    )


__all__ = ["router"]
