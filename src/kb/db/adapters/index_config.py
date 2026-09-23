"""``index_config`` — the guard that refuses to append to a mismatched index.

Spec §5 ④: a chunk table holds exactly one vector column, so its dimension is
fixed at build time. If the configured embedding model no longer matches what
produced the stored vectors, the only correct action is a full rebuild — writing
new vectors next to old ones would silently corrupt retrieval, because cosine
distance between vectors from different models is meaningless.

``chunker_version`` is the same argument one level down: changed chunking logic
with no version stamp leaves old and new chunks indistinguishable in one table.

The read is cross-tenant on purpose — ``index_config`` is a single global row
describing the whole installation, not per-tenant data.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.models import IndexConfig

# The global row's fixed primary key; the schema guarantees at most one row.
SINGLETON_ID = 1


class IndexConfigMismatch(RuntimeError):
    """The stored index was produced by a different model or chunker version."""


@dataclass(frozen=True, slots=True)
class StoredIndexConfig:
    embedding_provider: str
    embedding_model: str
    embedding_dim: int
    chunker_version: int


def describe(provider: str, model: str, dim: int, chunker_version: int) -> str:
    return f"{provider}/{model} dim={dim} chunker_version={chunker_version}"


class PostgresIndexConfig:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def load(self) -> StoredIndexConfig | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(select(IndexConfig).where(IndexConfig.id == SINGLETON_ID))
            ).scalars().one_or_none()
        if row is None:
            return None
        return StoredIndexConfig(
            embedding_provider=row.embedding_provider,
            embedding_model=row.embedding_model,
            embedding_dim=row.embedding_dim,
            chunker_version=row.chunker_version,
        )

    async def stamp(self, *, provider: str, model: str, dim: int, chunker_version: int) -> None:
        """Record what produced the index. Called once after a full build."""
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(
                    pg_insert(IndexConfig)
                    .values(
                        id=SINGLETON_ID,
                        embedding_provider=provider,
                        embedding_model=model,
                        embedding_dim=dim,
                        chunker_version=chunker_version,
                    )
                    .on_conflict_do_update(
                        index_elements=[IndexConfig.id],
                        set_={
                            "embedding_provider": provider,
                            "embedding_model": model,
                            "embedding_dim": dim,
                            "chunker_version": chunker_version,
                        },
                    )
                )

    async def verify(self, *, provider: str, model: str, dim: int, chunker_version: int) -> None:
        """Raise unless the stored index was built with exactly these settings.

        An empty ``index_config`` is accepted: that is a brand-new installation
        with nothing to be inconsistent with. Every mismatch afterwards is a
        rebuild, not an append.
        """
        stored = await self.load()
        if stored is None:
            return
        expected = (provider, model, dim, chunker_version)
        actual = (
            stored.embedding_provider,
            stored.embedding_model,
            stored.embedding_dim,
            stored.chunker_version,
        )
        if expected != actual:
            raise IndexConfigMismatch(
                "the stored index was built with different settings; run a full rebuild "
                "(POST /api/admin/rebuild) instead of appending to it. "
                f"stored: {describe(*actual)} — configured: {describe(*expected)}"
            )
