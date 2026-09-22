"""Database engines and session factories.

Two engines, deliberately:

* **app engine** — connects as the unprivileged role (``kb_app``). RLS applies, so
  this is the only engine request-serving code should use.
* **admin engine** — connects as the owner. Privileged paths only: full rebuild,
  test fixtures, maintenance CLI. RLS does not apply here, which is the point.

``kb.config.settings`` refuses to load if both DSNs name the same role, so the
two engines cannot accidentally collapse into one.
"""

from __future__ import annotations

from functools import cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from kb.config import get_settings

# Small pools: one process per service, and Postgres connections are cheap
# relative to the embedding calls this service spends most of its time on.
POOL_SIZE = 5


def _create_engine(dsn: str) -> AsyncEngine:
    return create_async_engine(
        dsn,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=POOL_SIZE,
    )


@cache
def get_app_engine() -> AsyncEngine:
    """Engine for request-serving code. RLS applies."""
    return _create_engine(get_settings().database_url)


@cache
def get_admin_engine() -> AsyncEngine:
    """Engine for privileged paths. RLS does not apply — use sparingly."""
    dsn = get_settings().database_url_admin
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL_ADMIN is not configured. It is required for admin "
            "operations (rebuild, fixtures) and must point at the owner role."
        )
    return _create_engine(dsn)


@cache
def get_app_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Session factory for request-serving code.

    ``expire_on_commit=False`` so objects stay usable after a commit — the
    request handlers read response fields off the ORM objects they just wrote.
    """
    return async_sessionmaker(get_app_engine(), expire_on_commit=False)


@cache
def get_admin_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_admin_engine(), expire_on_commit=False)


async def dispose_engines() -> None:
    """Close pooled connections.

    Needed by tests: each one may point at a different database, and the cached
    factories would otherwise hand back an engine bound to the previous DSN.
    """
    if get_app_engine.cache_info().currsize:
        await get_app_engine().dispose()
    if get_admin_engine.cache_info().currsize:
        await get_admin_engine().dispose()
    get_app_engine.cache_clear()
    get_admin_engine.cache_clear()
    get_app_sessionmaker.cache_clear()
    get_admin_sessionmaker.cache_clear()
