"""Tenant context — the application half of the two-layer isolation (spec §5 ②).

The database half is the RLS policy installed by migration 0001, which reads
``current_setting('app.user_id')``. This module is what puts a value there.

**Why the variable is transaction-local, not session-local.**

The obvious implementation is ``SET app.user_id = ...`` on the session and a
``RESET`` when the connection goes back to the pool. That is one forgotten
``RESET`` away from serving tenant A's rows to tenant B, and the bug only shows
up under connection reuse — the worst possible failure mode.

Instead every value is written with ``set_config('app.user_id', ..., is_local =>
true)``, which Postgres scopes to the surrounding transaction and discards at
COMMIT or ROLLBACK. A connection cannot carry a tenant into the next transaction,
so the reset is not something anyone has to remember. `test_tenant_isolation.py`
pins this down: with a single-connection pool, a later transaction that sets no
tenant sees zero rows rather than the previous tenant's.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# is_local => true (third argument) is the whole trick — see module docstring.
SET_TENANT_SQL = text("SELECT set_config('app.user_id', :user_id, true)")

# Only used by health checks and tests, to observe what the database believes.
GET_TENANT_SQL = text("SELECT NULLIF(current_setting('app.user_id', true), '')")


class MissingTenantContext(RuntimeError):
    """A tenant-scoped operation was attempted without a tenant.

    Raised rather than defaulting to "all tenants": an empty ``app.user_id`` makes
    Postgres return zero rows, so silently continuing would look like "no results"
    instead of "you forgot to authenticate".
    """


def as_tenant_id(user_id: uuid.UUID | str) -> str:
    """Validate a tenant id and return it as the canonical string form.

    Accepts ``str`` because ids arrive from JWT claims, CLI arguments and test
    parameters. Anything that is not a UUID is rejected here rather than reaching
    Postgres, where the ``::uuid`` cast in the policy would abort the transaction
    with a less obvious message.
    """
    if user_id is None:
        raise MissingTenantContext("no tenant id supplied")
    if isinstance(user_id, uuid.UUID):
        return str(user_id)
    try:
        return str(uuid.UUID(str(user_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise MissingTenantContext(f"not a valid tenant id: {user_id!r}") from exc


async def set_tenant(session: AsyncSession, user_id: uuid.UUID | str) -> str:
    """Bind the current transaction to a tenant. Returns the canonical id.

    Must be called inside a transaction — ``is_local => true`` outside one is
    silently useless, because the implicit transaction ends with the statement.
    """
    tenant = as_tenant_id(user_id)
    await session.execute(SET_TENANT_SQL, {"user_id": tenant})
    return tenant


@asynccontextmanager
async def tenant_transaction(
    sessionmaker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID | str,
) -> AsyncIterator[AsyncSession]:
    """Open a session bound to one tenant, in one transaction.

    Use this instead of reaching for ``sessionmaker()`` directly on any path that
    touches tenant data: it is what guarantees the RLS policy has something to
    match on. Commits on clean exit, rolls back on exception.

    Usage::

        async with tenant_transaction(get_app_sessionmaker(), user_id) as session:
            session.add(document)
    """
    tenant = as_tenant_id(user_id)
    async with sessionmaker() as session:
        async with session.begin():
            await session.execute(SET_TENANT_SQL, {"user_id": tenant})
            yield session


async def current_tenant(session: AsyncSession) -> str | None:
    """Read back ``app.user_id`` as the database sees it. For tests and diagnostics."""
    result = await session.execute(GET_TENANT_SQL)
    return result.scalar_one()
