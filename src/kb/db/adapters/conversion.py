"""Postgres-backed conversion cache (spec §7 约束 2).

``conversion_cache`` is content-addressed and global on purpose — it has no
``user_id`` column and no RLS policy (see the note in migration 0001). Two
tenants uploading the same PDF genuinely should share the conversion: the cache
key is ``sha256(original bytes)``, so a hit leaks nothing but the fact that some
file with those exact bytes was converted before, and the markdown returned is a
deterministic function of those bytes.

It is not a tenant-scoped table, so this adapter correctly does not open a
tenant-bound transaction — the query builder's guard does not apply to it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.converter.service import CachedConversion
from kb.models import ConversionCache


class PostgresConversionCache:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def get(self, content_sha: str) -> CachedConversion | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(select(ConversionCache).where(ConversionCache.content_sha == content_sha))
            ).scalars().one_or_none()
        if row is None:
            return None
        return CachedConversion(status=row.status, converted=row.converted)

    async def put(self, content_sha: str, *, status: str, converted: str | None) -> None:
        """Insert or refresh. ``DO NOTHING`` on conflict.

        A content hash maps to exactly one conversion result, so a second write
        for the same key carries identical data — re-converting is the only way
        the values could differ, and that would be a bug rather than an update to
        record. Using ``DO NOTHING`` also keeps concurrent workers from
        deadlocking over the same row.
        """
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    pg_insert(ConversionCache)
                    .values(content_sha=content_sha, converted=converted, status=status)
                    .on_conflict_do_nothing(index_elements=[ConversionCache.content_sha])
                )


__all__ = ["PostgresConversionCache"]
