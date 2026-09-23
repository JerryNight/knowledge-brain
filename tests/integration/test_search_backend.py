"""The retrieval branches against a real database.

Everything that makes retrieval fast or safe here is a database behaviour —
pgvector's HNSW index, zhparser's Chinese segmentation, the RLS policy — and none
of it can be stubbed. ``tests/unit`` covers fusion and the query shapes; this file
covers what only Postgres can answer.

Two properties:

* **The read path stays inside its tenant.** The branches run over the *app*
  role, so RLS is the last line of defence. Each tenant is seeded with a note
  that has the same path, the same Chinese text *and* the same embedding as the
  other tenant's: if either layer leaks, the leak cannot hide.
* **The metadata filter still filters.** It now arrives as a separately resolved
  allow-list, so "no document carries this tag" and "the filter was ignored"
  have to be told apart — the first must return nothing, the second would
  return the closest note.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from kb.db.adapters.search import PostgresSearchBackend

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

EMBEDDING_DIM = 1536

# Identical for both tenants on purpose: a shared path, shared text and a shared
# embedding means a leak shows up as a wrong chunk id, not as an empty result.
SHARED_TEXT = "关于 PostgreSQL 向量检索与中文分词的调优记录"
UNRELATED_TEXT = "红烧肉的做法与火候控制"


def vector(*hot: int) -> list[float]:
    """A near-one-hot vector: 1.0 at ``hot``, 0.0 elsewhere."""
    values = [0.0] * EMBEDDING_DIM
    for index in hot:
        values[index] = 1.0
    return values


NEAR = vector(0, 1)
FAR = vector(EMBEDDING_DIM - 2, EMBEDDING_DIM - 1)


@dataclass(frozen=True)
class Tenant:
    """A seeded tenant and the ids its notes are keyed by."""

    user_id: uuid.UUID
    document_ids: dict[str, uuid.UUID]
    chunk_ids: dict[str, int]


@dataclass(frozen=True)
class Note:
    path: str
    tags: list[str]
    text: str
    embedding: list[float]


async def _seed_tenant(engine: AsyncEngine, *, email: str, notes: Sequence[Note]) -> Tenant:
    """Insert one tenant over the owner engine.

    The owner bypasses RLS — the fixture is not the thing under test, and seeding
    through the application role would make it depend on the policy it exists to
    check.
    """
    user_id = uuid.uuid4()
    document_ids: dict[str, uuid.UUID] = {}
    chunk_ids: dict[str, int] = {}

    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": user_id, "email": email},
        )
        for note in notes:
            document_id = uuid.uuid4()
            document_ids[note.path] = document_id
            await conn.execute(
                text(
                    "INSERT INTO documents"
                    " (id, user_id, source, source_path, content_sha, conversion_status, title, tags)"
                    " VALUES (:id, :uid, 'git', :path, :sha, 'ok', :title, CAST(:tags AS text[]))"
                ),
                {
                    "id": document_id,
                    "uid": user_id,
                    "path": note.path,
                    "sha": "0" * 64,
                    "title": note.path.rsplit("/", 1)[-1],
                    "tags": note.tags,
                },
            )
            result = await conn.execute(
                text(
                    "INSERT INTO chunks (user_id, document_id, ordinal, text, embedding)"
                    " VALUES (:uid, :did, 0, :body, CAST(:vec AS vector)) RETURNING id"
                ),
                {
                    "uid": user_id,
                    "did": document_id,
                    "body": note.text,
                    "vec": "[" + ",".join(repr(value) for value in note.embedding) + "]",
                },
            )
            chunk_ids[note.path] = result.scalar_one()

    return Tenant(user_id=user_id, document_ids=document_ids, chunk_ids=chunk_ids)


@pytest.fixture
async def tenants(admin_engine: AsyncEngine) -> dict[str, Tenant]:
    a = await _seed_tenant(
        admin_engine,
        email="a@example.com",
        notes=[
            Note("notes/vector.md", ["rag", "vector"], SHARED_TEXT, NEAR),
            Note("notes/cooking.md", ["misc"], UNRELATED_TEXT, FAR),
        ],
    )
    b = await _seed_tenant(
        admin_engine,
        email="b@example.com",
        notes=[Note("notes/vector.md", ["secret"], SHARED_TEXT, NEAR)],
    )
    return {"a": a, "b": b}


@pytest.fixture
def backend(app_sessionmaker: async_sessionmaker[AsyncSession]) -> PostgresSearchBackend:
    """The real adapter, over the role RLS applies to."""
    return PostgresSearchBackend(app_sessionmaker)


# ---------------------------------------------------------------------------
# Isolation on the read path
# ---------------------------------------------------------------------------


async def test_vector_search_returns_only_the_calling_tenants_chunks(backend, tenants) -> None:
    for tenant in tenants.values():
        hits = await backend.vector_search(user_id=tenant.user_id, embedding=NEAR, limit=25)
        assert hits, "the tenant's own note was not found at all"
        assert {hit.document_id for hit in hits} <= set(tenant.document_ids.values())


async def test_vector_search_maps_the_document_metadata(backend, tenants) -> None:
    """``source``/``title``/``chunk_id`` come from the second stage of the shape."""
    a = tenants["a"]
    hits = await backend.vector_search(user_id=a.user_id, embedding=NEAR, limit=25)

    closest = hits[0]
    assert closest.document_id == a.document_ids["notes/vector.md"]
    assert closest.chunk_id == a.chunk_ids["notes/vector.md"]
    assert closest.source == "git"
    assert closest.source_path == "notes/vector.md"
    assert closest.title == "vector.md"
    assert closest.score >= 0.0


async def test_keyword_search_returns_only_the_calling_tenants_chunks(backend, tenants) -> None:
    for tenant in tenants.values():
        hits = await backend.keyword_search(user_id=tenant.user_id, query="向量检索", limit=25)
        assert hits, "zhparser did not match the tenant's own Chinese note"
        assert {hit.chunk_id for hit in hits} <= set(tenant.chunk_ids.values())


async def test_unknown_tenant_sees_nothing(backend) -> None:
    """Fail closed: an id with no rows must return empty, not the whole table."""
    stranger = uuid.uuid4()
    assert await backend.vector_search(user_id=stranger, embedding=NEAR, limit=25) == []
    assert await backend.keyword_search(user_id=stranger, query="向量检索", limit=25) == []


# ---------------------------------------------------------------------------
# The metadata filter, now that it is resolved separately
# ---------------------------------------------------------------------------


async def test_tag_filter_restricts_the_vector_branch(backend, tenants) -> None:
    """The tag filter must override relevance — the closest note is tagged "rag"."""
    a = tenants["a"]
    hits = await backend.vector_search(user_id=a.user_id, embedding=NEAR, limit=25, tags=["misc"])
    assert {hit.document_id for hit in hits} == {a.document_ids["notes/cooking.md"]}


async def test_path_prefix_filter_restricts_the_vector_branch(backend, tenants) -> None:
    a = tenants["a"]
    hits = await backend.vector_search(
        user_id=a.user_id, embedding=NEAR, limit=25, path_prefix="notes/vector"
    )
    assert {hit.document_id for hit in hits} == {a.document_ids["notes/vector.md"]}


async def test_a_filter_that_matches_nothing_returns_nothing(backend, tenants) -> None:
    """Distinguishes "no such tag" from "the filter was dropped on the floor"."""
    a = tenants["a"]
    assert await backend.vector_search(user_id=a.user_id, embedding=NEAR, limit=25, tags=["nobody"]) == []
    assert await backend.keyword_search(user_id=a.user_id, query="向量检索", limit=25, tags=["nobody"]) == []


async def test_keyword_branch_respects_the_tag_filter(backend, tenants) -> None:
    """The note holding the phrase is tagged "rag"/"vector", not "misc"."""
    a = tenants["a"]
    hits = await backend.keyword_search(user_id=a.user_id, query="向量检索", limit=25, tags=["misc"])
    assert hits == []
