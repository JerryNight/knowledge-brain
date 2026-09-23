"""Repository management — ``POST /api/repos/{id}/sync`` and friends (spec §9).

The sync endpoint **enqueues** rather than synchronising inline. That is the
design, not a shortcut: the polled sync and the manual sync then travel the
identical code path through the worker, so the ``last_synced_sha`` red line
(spec §11.1 #2) has one implementation instead of two. It also means the request
returns immediately instead of holding an HTTP connection open for the length of
a full clone.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from kb.deps import ServicesDep, UserIdDep

router = APIRouter(prefix="/api/repos", tags=["repos"])


class CreateRepoRequest(BaseModel):
    url: str = Field(min_length=1, description="Git URL, e.g. git@github.com:you/vault.git")
    branch: str = Field(default="main", min_length=1)
    credential_ref: str | None = Field(
        default=None,
        description="Name of the environment variable holding the token. The token never reaches the DB.",
    )
    sync_now: bool = Field(default=True, description="Queue the first full sync immediately.")


class SyncResponse(BaseModel):
    repo_id: uuid.UUID
    job_id: int
    message: str


class CreateRepoResponse(BaseModel):
    repo_id: uuid.UUID
    job_id: int | None = None


@router.get("")
async def list_repos(services: ServicesDep, user_id: UserIdDep) -> dict:
    repos = await services.repos.list_repos(user_id)
    return {"count": len(repos), "repos": [repo.as_dict() for repo in repos]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_repo(
    payload: CreateRepoRequest, services: ServicesDep, user_id: UserIdDep
) -> CreateRepoResponse:
    repo_id = await services.repos.create_repo(
        user_id,
        url=payload.url,
        branch=payload.branch,
        credential_ref=payload.credential_ref,
        sync_now=False,
    )
    job_id = await services.repos.request_sync(user_id, repo_id) if payload.sync_now else None
    return CreateRepoResponse(repo_id=repo_id, job_id=job_id)


@router.post("/{repo_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync_repo(repo_id: uuid.UUID, services: ServicesDep, user_id: UserIdDep) -> SyncResponse:
    """Queue a sync for one repository.

    404 rather than 403 when the repo belongs to someone else — the tenant-scoped
    query simply does not see it, which is the same answer as "does not exist"
    and leaks nothing about other tenants' repo ids.
    """
    repo = await services.repos.get_repo(user_id, repo_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="repository not found")
    job_id = await services.repos.request_sync(user_id, repo_id)
    return SyncResponse(
        repo_id=repo_id,
        job_id=job_id,
        message="同步任务已入队，由 worker 消费；用 GET /api/admin/status 看进度。",
    )


__all__ = ["router"]
