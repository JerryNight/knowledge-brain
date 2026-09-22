"""The single entry point for tenant-scoped SQL (spec §5 ②).

Every read or write that touches tenant-owned rows goes through a builder here.
Nothing else in the codebase is allowed to write ``select(Chunk)`` or a ``FROM
chunks`` by hand — ``tests/unit/test_tenant_scoping_guard.py`` scans the source
tree and fails if it finds one outside this module.

That check is the point. "Remember to add ``WHERE user_id = ...``" is a
convention, and conventions decay; a structural guard does not. The RLS policy in
migration 0001 is the second layer, so a missed filter still has to get past
Postgres — but relying on that alone means every leak becomes a database-level
incident instead of a compile-time one.

At this milestone the builders only apply the tenant predicate. The vector and
keyword branches, RRF fusion and per-document capping (spec §8) extend these
functions in the retrieval milestone rather than introducing new query sites.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import Select, select

from kb.db.tenant import MissingTenantContext
from kb.models import Chunk, Document, Repo

# The text search configuration used by the keyword branch. Must match the one
# baked into the generated `chunks.tsv` column (kb.models.chunk.TS_CONFIG).
TS_CONFIG: Final = "chinese"

# Tables that carry a `user_id` and are therefore subject to the tenant predicate.
# Used by the guard test to know what to look for.
TENANT_SCOPED_TABLES: Final = ("chunks", "documents", "repos")


def tenant_id(user_id: uuid.UUID | str) -> uuid.UUID:
    """Normalise and validate a tenant id.

    Rejects ``None`` and malformed values instead of letting a query run without
    a filter. Note the asymmetry with the database layer: RLS turns a *missing*
    ``app.user_id`` into an empty result set, whereas the application layer raises.
    Raising is the right behaviour here — an empty result looks like "nothing
    matched", which is indistinguishable from "you forgot to authenticate".
    """
    if user_id is None:
        raise MissingTenantContext("tenant-scoped query attempted without a user id")
    if isinstance(user_id, uuid.UUID):
        return user_id
    try:
        return uuid.UUID(str(user_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise MissingTenantContext(f"not a valid tenant id: {user_id!r}") from exc


def scoped_chunks(user_id: uuid.UUID | str) -> Select:
    """Base SELECT over ``chunks`` for one tenant.

    ``chunks.user_id`` is denormalised (spec §5 ①) precisely so this needs no
    join to ``documents``: the tenant filter sits in the retrieval query itself,
    is index-backed by ``ix_chunks_user_id``, and can be asserted on directly.
    """
    return select(Chunk).where(Chunk.user_id == tenant_id(user_id))


def scoped_documents(user_id: uuid.UUID | str) -> Select:
    """Base SELECT over ``documents`` for one tenant."""
    return select(Document).where(Document.user_id == tenant_id(user_id))


def scoped_repos(user_id: uuid.UUID | str) -> Select:
    """Base SELECT over ``repos`` for one tenant."""
    return select(Repo).where(Repo.user_id == tenant_id(user_id))
