"""``repos`` — listing, registration, and the enqueue side of the admin API.

Reads and writes go through ``kb.retrieval.query_builder`` like everything else
that touches a tenant table (spec §5 ②). The two cross-tenant queries in this
module — the worker's repository scan — live in the query builder too, with a
comment explaining why the tenant predicate is absent.

``enqueue`` here is the *durable* half of "sync this repo now": the job is the
trigger, not a direct call, so a manual sync and a polled sync take the identical
path through the worker. A second code path that synchronised inline would be a
second place for the ``last_synced_sha`` red line to be got wrong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.db.tenant import tenant_transaction
from kb.models.sync_job import JOB_FULL_REBUILD, JOB_REPO_SYNC
from kb.queue.base import JobSpec
from kb.queue.postgres import PostgresJobQueue
from kb.retrieval import query_builder as qb
from kb.sync.ports import RepoRef


@dataclass(frozen=True, slots=True)
class RepoInfo:
    id: uuid.UUID
    user_id: uuid.UUID
    url: str
    branch: str
    last_synced_sha: str | None
    sync_enabled: bool
    credential_ref: str | None

    def as_dict(self) -> dict:
        return {
            "id": str(self.id),
            "url": self.url,
            "branch": self.branch,
            "last_synced_sha": self.last_synced_sha,
            "sync_enabled": self.sync_enabled,
        }


class PostgresRepoService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], queue: PostgresJobQueue) -> None:
        self._sessionmaker = sessionmaker
        self._queue = queue

    async def list_repos(self, user_id: uuid.UUID) -> list[RepoInfo]:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            rows = (await session.execute(qb.all_repos(user_id))).scalars().all()
        return [_to_info(row) for row in rows]

    async def get_repo(self, user_id: uuid.UUID, repo_id: uuid.UUID) -> RepoInfo | None:
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            row = (await session.execute(qb.repo_by_id(user_id, repo_id))).scalars().one_or_none()
        return _to_info(row) if row is not None else None

    async def create_repo(
        self,
        user_id: uuid.UUID,
        *,
        url: str,
        branch: str = "main",
        credential_ref: str | None = None,
        sync_now: bool = True,
    ) -> uuid.UUID:
        """Register a repository and, by default, queue its first sync.

        The initial ``last_synced_sha`` is NULL, which the pipeline reads as
        "first sync — enumerate the whole tree" (spec §6), so no special case is
        needed here.
        """
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            repo_id = (await session.execute(qb.create_repo(
                user_id, url=url, branch=branch, credential_ref=credential_ref
            ))).scalar_one()
        if sync_now:
            await self.request_sync(user_id, repo_id)
        return repo_id

    async def request_sync(self, user_id: uuid.UUID, repo_id: uuid.UUID) -> int:
        """Queue a manual sync. Returns the job id."""
        return await self._queue.enqueue(
            JobSpec(user_id=user_id, kind=JOB_REPO_SYNC, repo_id=repo_id, payload={})
        )

    async def request_full_rebuild(self, user_id: uuid.UUID) -> int:
        """Queue a full index rebuild for one tenant (spec §9)."""
        return await self._queue.enqueue(
            JobSpec(user_id=user_id, kind=JOB_FULL_REBUILD, repo_id=None, payload={})
        )

    async def repos_for_sync(self, user_id: uuid.UUID) -> list[RepoRef]:
        """Enabled repositories as ``RepoRef``s, for the worker's sync job."""
        async with tenant_transaction(self._sessionmaker, user_id) as session:
            rows = (await session.execute(qb.enabled_repos(user_id))).scalars().all()
        return [
            RepoRef(
                id=row.id,
                user_id=row.user_id,
                url=row.url,
                branch=row.branch,
                last_synced_sha=row.last_synced_sha,
                credential_ref=row.credential_ref,
            )
            for row in rows
        ]


def _to_info(row) -> RepoInfo:
    return RepoInfo(
        id=row.id,
        user_id=row.user_id,
        url=row.url,
        branch=row.branch,
        last_synced_sha=row.last_synced_sha,
        sync_enabled=row.sync_enabled,
        credential_ref=row.credential_ref,
    )


__all__ = ["PostgresRepoService", "RepoInfo"]
