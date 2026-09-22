"""Cross-tenant leakage — spec §11.1 #1, the first red line.

Two tenants, each with a document and chunks. Tenant A's context must never see a
single row belonging to tenant B.

The two layers of spec §5 ② are verified **separately**, because a test that
exercises both at once cannot tell you which one is doing the work:

* **RLS alone** — raw SQL over the unprivileged role. The query builder is not
  involved; the database policy is the only thing standing in the way.
* **Query builder alone** — the builder's statement executed over the *owner*
  engine, where RLS does not apply. If the builder ever forgets its ``WHERE``,
  tenant B's rows show up here and this test fails.

Plus the failure modes that are easy to get wrong: a missing tenant variable, a
malformed one, a write that tries to stamp another tenant's id, and — the one
that only shows up under load — a connection handed back to the pool still
holding the previous tenant's binding.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
from sqlalchemy import exc, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from kb.db import MissingTenantContext, tenant_transaction
from kb.retrieval.query_builder import scoped_chunks

from .conftest import Dsns

CHUNKS_PER_TENANT = 3


@dataclass
class Tenant:
    """A seeded tenant and the ids its data is keyed by."""

    user_id: uuid.UUID
    document_id: uuid.UUID
    chunk_ids: list[int] = field(default_factory=list)

    @property
    def chunk_count(self) -> int:
        return len(self.chunk_ids)


async def _seed_tenant(engine: AsyncEngine, *, email: str, marker: str) -> Tenant:
    """Insert a tenant with one document and ``CHUNKS_PER_TENANT`` chunks.

    Runs over the owner engine, which bypasses RLS — the fixture is not the thing
    under test, and using the application role here would make seeding depend on
    the very policy being verified.
    """
    tenant = Tenant(user_id=uuid.uuid4(), document_id=uuid.uuid4())

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": tenant.user_id, "email": email},
        )
        await conn.execute(
            text(
                "INSERT INTO documents (id, user_id, source, source_path, content_sha, conversion_status)"
                " VALUES (:id, :uid, 'git', :path, :sha, 'ok')"
            ),
            {
                "id": tenant.document_id,
                "uid": tenant.user_id,
                "path": f"notes/{marker}.md",
                "sha": "0" * 64,
            },
        )
        for ordinal in range(CHUNKS_PER_TENANT):
            result = await conn.execute(
                text(
                    "INSERT INTO chunks (user_id, document_id, ordinal, text)"
                    " VALUES (:uid, :did, :ordinal, :body) RETURNING id"
                ),
                {
                    "uid": tenant.user_id,
                    "did": tenant.document_id,
                    "ordinal": ordinal,
                    # Unique per tenant so an assertion failure names the culprit.
                    "body": f"{marker} chunk {ordinal}",
                },
            )
            tenant.chunk_ids.append(result.scalar_one())

    return tenant


@pytest.fixture
async def two_tenants(admin_engine: AsyncEngine) -> tuple[Tenant, Tenant]:
    a = await _seed_tenant(admin_engine, email="a@example.com", marker="tenant-a")
    b = await _seed_tenant(admin_engine, email="b@example.com", marker="tenant-b")
    return a, b


@pytest.fixture
async def app_sessionmaker(pg_dsns: Dsns) -> AsyncIterator[async_sessionmaker]:
    """Session factory over the unprivileged role, so RLS is in force."""
    engine = create_async_engine(pg_dsns.app_async)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _visible_chunk_ids(conn, user_id: uuid.UUID | None) -> list[int]:
    """Read chunk ids inside a transaction optionally bound to ``user_id``.

    Raw SQL on purpose — this is the path that must be stopped by the database,
    not by application code.
    """
    if user_id is None:
        return (await conn.execute(text("SELECT id FROM chunks ORDER BY id"))).scalars().all()
    await conn.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)})
    return (await conn.execute(text("SELECT id FROM chunks ORDER BY id"))).scalars().all()


# ---------------------------------------------------------------------------
# Layer ① — the database policy, with the query builder out of the picture
# ---------------------------------------------------------------------------


async def test_rls_hides_other_tenants_chunks_from_raw_sql(app_sessionmaker, two_tenants) -> None:
    tenant_a, tenant_b = two_tenants

    async with tenant_transaction(app_sessionmaker, tenant_a.user_id) as session:
        visible = (await session.execute(text("SELECT id FROM chunks ORDER BY id"))).scalars().all()

    assert sorted(visible) == sorted(tenant_a.chunk_ids)
    assert not set(visible) & set(tenant_b.chunk_ids), "raw SQL leaked another tenant's chunks"


async def test_rls_hides_other_tenants_documents_from_raw_sql(app_sessionmaker, two_tenants) -> None:
    tenant_a, tenant_b = two_tenants

    async with tenant_transaction(app_sessionmaker, tenant_a.user_id) as session:
        visible = (await session.execute(text("SELECT id FROM documents"))).scalars().all()

    assert [str(v) for v in visible] == [str(tenant_a.document_id)]
    assert str(tenant_b.document_id) not in {str(v) for v in visible}


async def test_rls_hides_other_tenants_repos_from_raw_sql(app_sessionmaker, two_tenants) -> None:
    """`repos` is behind RLS too — it is reachable from the REST admin API."""
    async with app_sessionmaker() as session:
        async with session.begin():
            await session.execute(
                text("SELECT set_config('app.user_id', :uid, true)"),
                {"uid": str(two_tenants[0].user_id)},
            )
            visible = (await session.execute(text("SELECT id FROM repos ORDER BY id"))).scalars().all()

    assert visible == []


# ---------------------------------------------------------------------------
# Layer ② — the query builder, with RLS switched off
# ---------------------------------------------------------------------------


async def test_query_builder_scopes_even_without_rls(admin_engine: AsyncEngine, two_tenants) -> None:
    """Run the builder's statement as the owner, where RLS does not apply.

    This is the only way to prove the *application* layer filters on its own. If
    `scoped_chunks` ever loses its `WHERE user_id`, tenant B's rows appear here
    even though every RLS-based test still passes.
    """
    tenant_a, tenant_b = two_tenants

    async with admin_engine.connect() as conn:
        rows = (await conn.execute(scoped_chunks(tenant_a.user_id))).all()

    returned = {row.id for row in rows}
    assert returned == set(tenant_a.chunk_ids)
    assert not returned & set(tenant_b.chunk_ids), "query builder leaked another tenant's chunks"


async def test_query_builder_rejects_missing_tenant() -> None:
    """No tenant means no query — not "all tenants"."""
    for bad in (None, "", "not-a-uuid", "12345"):
        with pytest.raises(MissingTenantContext):
            scoped_chunks(bad)


async def test_query_builder_renders_a_tenant_predicate() -> None:
    """Cheap structural check: the compiled SQL carries the filter."""
    tenant_id = uuid.uuid4()
    compiled = str(scoped_chunks(tenant_id).compile(compile_kwargs={"literal_binds": True}))
    assert "user_id" in compiled
    assert str(tenant_id) in compiled


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_unset_tenant_variable_returns_nothing(app_sessionmaker, two_tenants) -> None:
    """A missing `app.user_id` must yield an empty set, never the whole table."""
    async with app_sessionmaker() as session:
        async with session.begin():
            visible = await _visible_chunk_ids(session, None)

    assert visible == [], "an unset tenant variable exposed rows"


async def test_empty_tenant_variable_returns_nothing(app_sessionmaker, two_tenants) -> None:
    """The empty string is what a sloppy RESET leaves behind — also must be empty."""
    async with app_sessionmaker() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.user_id', '', true)"))
            visible = (await session.execute(text("SELECT id FROM chunks"))).scalars().all()

    assert visible == []


async def test_malformed_tenant_variable_fails_closed(app_sessionmaker, two_tenants) -> None:
    """Garbage in `app.user_id` must abort, not silently match everything.

    The policy casts to uuid; an unparseable value raises inside the statement
    (SQLSTATE 42501 is for policy violations, 22P02 for a bad cast — either way
    the query does not return data).
    """
    with pytest.raises(exc.DBAPIError):
        async with app_sessionmaker() as session:
            async with session.begin():
                await session.execute(text("SELECT set_config('app.user_id', 'garbage', true)"))
                await session.execute(text("SELECT id FROM chunks"))


async def test_with_check_blocks_stamping_another_tenant(app_sessionmaker, two_tenants) -> None:
    """Writing a row for someone else is blocked by the policy's WITH CHECK."""
    tenant_a, tenant_b = two_tenants

    with pytest.raises(exc.DBAPIError):
        async with tenant_transaction(app_sessionmaker, tenant_a.user_id) as session:
            await session.execute(
                text("INSERT INTO chunks (user_id, document_id, ordinal, text) VALUES (:uid, :did, 99, 'smuggled')"),
                {"uid": tenant_b.user_id, "did": tenant_a.document_id},
            )


async def test_tenant_binding_does_not_survive_the_transaction(app_sessionmaker, two_tenants) -> None:
    """The pool-reset hazard, exercised on a single physical connection.

    `app.user_id` is written with `set_config(..., is_local => true)`, so Postgres
    discards it at COMMIT. Two transactions on the *same* connection stand in for
    "connection returned to the pool, then checked out again" — which is exactly
    when a session-scoped `SET` would leak tenant A into tenant B's request.
    """
    tenant_a, tenant_b = two_tenants

    async with app_sessionmaker() as session:
        async with session.begin():
            first = await _visible_chunk_ids(session, tenant_a.user_id)
            assert sorted(first) == sorted(tenant_a.chunk_ids)  # not vacuous

        async with session.begin():
            second = await _visible_chunk_ids(session, None)

    assert second == [], (
        "tenant binding survived into the next transaction on the same connection — "
        f"got {second}, which is {tenant_a.chunk_ids} / {tenant_b.chunk_ids}"
    )


async def test_owner_engine_sees_every_tenant(admin_engine: AsyncEngine, two_tenants) -> None:
    """Sanity check on the fixture: RLS really is off for the owner role.

    Without this, `test_query_builder_scopes_even_without_rls` could be passing
    because RLS was doing the work after all.
    """
    tenant_a, tenant_b = two_tenants

    async with admin_engine.connect() as conn:
        total = (await conn.execute(text("SELECT count(*) FROM chunks"))).scalar_one()

    assert total == tenant_a.chunk_count + tenant_b.chunk_count
