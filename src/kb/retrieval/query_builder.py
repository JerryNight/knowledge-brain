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

Two rules for anything added here:

* every builder over ``chunks``/``documents``/``repos`` takes ``user_id`` and
  applies it as a predicate, and
* joins between tenant tables repeat the predicate on both sides rather than
  trusting the join to carry it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Final

from sqlalchemy import Delete, Insert, Select, Update, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kb.db.tenant import MissingTenantContext
from kb.models import Chunk, Document, Repo

# The text search configuration used by the keyword branch. Must match the one
# baked into the generated `chunks.tsv` column (kb.models.chunk.TS_CONFIG).
TS_CONFIG: Final = "chinese"

# Tables that carry a `user_id` and are therefore subject to the tenant predicate.
# Used by the guard test to know what to look for.
TENANT_SCOPED_TABLES: Final = ("chunks", "documents", "repos")

# How many candidates each retrieval branch contributes before fusion (spec §8).
DEFAULT_BRANCH_LIMIT: Final = 40


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


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


def document_by_path(user_id: uuid.UUID | str, source: str, source_path: str) -> Select:
    """One document by its unique key — ``(user_id, source, source_path)``.

    This is the lookup behind every short-circuit decision in the sync pipeline,
    so it is index-backed by the unique constraint (spec §5 ③).
    """
    return scoped_documents(user_id).where(Document.source == source, Document.source_path == source_path)


def document_by_id(user_id: uuid.UUID | str, document_id: uuid.UUID) -> Select:
    return scoped_documents(user_id).where(Document.id == document_id)


def document_paths(user_id: uuid.UUID | str, source: str) -> Select:
    """Every path in one channel. Used by the ``.kbignore`` full rescan."""
    return scoped_documents(user_id).with_only_columns(Document.source_path).where(Document.source == source)


def documents_by_status(user_id: uuid.UUID | str, statuses: Sequence[str]) -> Select:
    """Documents in given conversion states — the ``failed``/``no_text`` report."""
    return scoped_documents(user_id).where(Document.conversion_status.in_(list(statuses)))


def documents_for_source(user_id: uuid.UUID | str, source: str | None = None) -> Select:
    statement = scoped_documents(user_id)
    if source is not None:
        statement = statement.where(Document.source == source)
    return statement.order_by(Document.source_path)


def browse_documents(user_id: uuid.UUID | str, *, prefix: str | None = None, limit: int = 100) -> Select:
    """Browse a tenant's documents, optionally under a path prefix.

    Backs ``list_notes`` (spec §9). ``source`` is ordered ahead of ``source_path``
    so the two channels come back in stable, separately-grouped blocks rather
    than interleaved — a directory listing that shuffles between a vault note and
    an upload of the same name is confusing to read.
    """
    statement = scoped_documents(user_id)
    if prefix:
        statement = statement.where(Document.source_path.like(f"{prefix}%"))
    return statement.order_by(Document.source, Document.source_path).limit(max(1, limit))


def documents_by_path(user_id: uuid.UUID | str, source_path: str) -> Select:
    """Every document with this path, across both channels.

    ``read_note`` receives only a path (that is what search returns), and the two
    channels are allowed to share a name (spec §5 ③). Ordering by ``source`` keeps
    the pick deterministic; the response echoes which channel it came from.
    """
    return scoped_documents(user_id).where(Document.source_path == source_path).order_by(Document.source)


def delete_all_chunks(user_id: uuid.UUID | str) -> Delete:
    """Drop every chunk of one tenant — the first half of a full rebuild.

    Deleting chunks (rather than documents) is what makes a rebuild cheap and
    safe: ``documents`` keeps the path and ``content_sha`` needed to re-derive
    everything, and the conversion cache means PDFs are not parsed again.
    """
    return delete(Chunk).where(Chunk.user_id == tenant_id(user_id))


def clear_indexed_at(user_id: uuid.UUID | str) -> Update:
    """Mark every document of a tenant as not-yet-indexed.

    Called at the start of a rebuild. Chunks are already gone, so the level-3
    short circuit re-embeds everything without any help — but a stale
    ``indexed_at`` would make a half-finished rebuild look complete.
    """
    return update(Document).where(Document.user_id == tenant_id(user_id)).values(indexed_at=None)


def update_document_path(user_id: uuid.UUID | str, document_id: uuid.UUID, new_path: str) -> Update:
    """Re-path a document. Called for a rename whose content did not change."""
    return (
        update(Document)
        .where(Document.id == document_id, Document.user_id == tenant_id(user_id))
        .values(source_path=new_path)
    )


def update_document_conversion(
    user_id: uuid.UUID | str,
    document_id: uuid.UUID,
    *,
    values: dict,
) -> Update:
    """Record the outcome of a conversion attempt.

    Takes a mapping rather than named arguments so the indexer can set
    ``converted_sha``/``conversion_status``/``conversion_error`` together — those
    three are only ever meaningful as a set.
    """
    return (
        update(Document)
        .where(Document.id == document_id, Document.user_id == tenant_id(user_id))
        .values(**values)
    )


def delete_document(user_id: uuid.UUID | str, document_id: uuid.UUID) -> Delete:
    """Delete a document; ``chunks`` follow via ``ON DELETE CASCADE`` (spec §6)."""
    return delete(Document).where(Document.id == document_id, Document.user_id == tenant_id(user_id))


def delete_document_by_path(user_id: uuid.UUID | str, source: str, source_path: str) -> Delete:
    return delete(Document).where(
        Document.user_id == tenant_id(user_id),
        Document.source == source,
        Document.source_path == source_path,
    )


# ---------------------------------------------------------------------------
# chunks
# ---------------------------------------------------------------------------


def chunks_for_document(user_id: uuid.UUID | str, document_id: uuid.UUID) -> Select:
    """Every chunk of one document, in order. The indexer's level-3 comparison."""
    return scoped_chunks(user_id).where(Chunk.document_id == document_id).order_by(Chunk.ordinal)


def delete_chunks_for_document(user_id: uuid.UUID | str, document_id: uuid.UUID) -> Delete:
    return delete(Chunk).where(Chunk.document_id == document_id, Chunk.user_id == tenant_id(user_id))


def chunk_count(user_id: uuid.UUID | str) -> Select:
    return select(func.count()).select_from(Chunk).where(Chunk.user_id == tenant_id(user_id))


def insert_chunks(user_id: uuid.UUID | str, rows: Sequence[Mapping[str, object]]) -> Insert:
    """Bulk-insert chunk rows for one tenant.

    ``user_id`` is stamped here rather than trusted from the caller: the
    denormalised tenant column (spec §5 ①) is the retrieval filter, so a row
    written without it would be unreachable *and* would slip past the RLS policy
    on the way in. Callers pass only content columns.
    """
    tenant = tenant_id(user_id)
    return insert(Chunk).values([{**dict(row), "user_id": tenant} for row in rows])


# ---------------------------------------------------------------------------
# writes
# ---------------------------------------------------------------------------


def upsert_document(
    user_id: uuid.UUID | str,
    *,
    source: str,
    source_path: str,
    content_sha: str,
    mime: str | None = None,
    size_bytes: int | None = None,
    title: str | None = None,
    tags: Sequence[str] | None = None,
) -> Insert:
    """Create the document row, or refresh it when ``(user_id, source, source_path)`` exists.

    The conflict target is the unique constraint from spec §5 ③ — the same one
    that keeps the git and upload channels from colliding. ``converted_sha`` and
    ``conversion_status`` are deliberately *not* reset here: the row's content
    changed, but the indexer has not run yet, so clearing the conversion outcome
    would make a concurrent reader see a document with no known state.
    """
    statement = pg_insert(Document).values(
        id=uuid.uuid4(),
        user_id=tenant_id(user_id),
        source=source,
        source_path=source_path,
        content_sha=content_sha,
        mime=mime,
        size_bytes=size_bytes,
        title=title,
        tags=list(tags) if tags else None,
    )
    return statement.on_conflict_do_update(
        constraint="uq_documents_user_source_path",
        set_={
            "content_sha": statement.excluded.content_sha,
            "mime": statement.excluded.mime,
            "size_bytes": statement.excluded.size_bytes,
            "title": statement.excluded.title,
            "tags": statement.excluded.tags,
            "updated_at": func.now(),
        },
    ).returning(Document.id)


def create_repo(
    user_id: uuid.UUID | str,
    *,
    url: str,
    branch: str = "main",
    credential_ref: str | None = None,
) -> Insert:
    """Register a repository for one tenant.

    The URL is not unique per tenant on purpose: the same vault can legitimately
    be tracked twice (two branches, or a read-only mirror) and each needs its own
    ``last_synced_sha``.
    """
    return (
        insert(Repo)
        .values(
            id=uuid.uuid4(),
            user_id=tenant_id(user_id),
            url=url,
            branch=branch,
            credential_ref=credential_ref,
        )
        .returning(Repo.id)
    )


def all_repos(user_id: uuid.UUID | str) -> Select:
    """Every repository of one tenant, enabled or not — the admin listing."""
    return scoped_repos(user_id).order_by(Repo.created_at)


def set_repo_sync_enabled(user_id: uuid.UUID | str, repo_id: uuid.UUID, enabled: bool) -> Update:
    return (
        update(Repo)
        .where(Repo.id == repo_id, Repo.user_id == tenant_id(user_id))
        .values(sync_enabled=enabled)
    )


# -- retrieval branches (spec §8) -------------------------------------------

# What a branch scores. Selecting these explicitly keeps the two branches
# symmetric, so RRF can key on chunk id without either side depending on ORM
# identity. Document metadata is deliberately absent — see `_attach_documents`.
def _candidate_columns(score_expression):
    return (
        Chunk.id.label("chunk_id"),
        Chunk.document_id.label("document_id"),
        Chunk.text.label("text"),
        Chunk.heading_path.label("heading_path"),
        Chunk.locator.label("locator"),
        score_expression.label("score"),
    )


def documents_matching_filters(
    user_id: uuid.UUID | str,
    *,
    tags: Sequence[str] | None = None,
    path_prefix: str | None = None,
) -> Select:
    """Ids of a tenant's documents matching the metadata filters.

    These filters live on ``documents``, but the scoring stage of a branch must
    not join to it (see `_attach_documents`), so they are resolved up front in
    this separate, cheap query and handed down as a plain allow-list.

    The distinction is not cosmetic. Written as an ``IN (subquery)`` the planner
    de-correlates the filter into a hash join and the vector branch loses HNSW
    again: measured 6352 ms, versus 8.1 ms once the ids arrive as an array.
    """
    statement = scoped_documents(user_id).with_only_columns(Document.id)
    if tags:
        # ARRAY overlap (&&): matching any requested tag, which is what a user
        # filtering by tag means.
        statement = statement.where(Document.tags.overlap(list(tags)))
    if path_prefix:
        statement = statement.where(Document.source_path.like(f"{path_prefix}%"))
    return statement


def _attach_documents(user_id: uuid.UUID | str, inner: Select, *, ascending: bool) -> Select:
    """Attach document metadata to an already-limited candidate query.

    The ``LIMIT`` has to be applied *inside* the CTE, before the join — that is
    the entire point of this shape. An HNSW index scan carries a very high
    startup cost (pgvector estimates ~2500 before the first row), so it only
    pays off when a ``Limit`` node can sit directly on top of it. Insert a join
    in between and the planner can no longer push the ``LIMIT`` down; it falls
    back to "scan every chunk, sort, keep 40", and the index is never chosen.
    On 50k chunks that is 5935 ms against 5.7 ms.
    """
    top = inner.cte("top")
    ordering = top.c.score.asc() if ascending else top.c.score.desc()
    return (
        select(
            top.c.chunk_id,
            top.c.document_id,
            top.c.text,
            top.c.heading_path,
            top.c.locator,
            top.c.score,
            Document.source,
            Document.source_path,
            Document.title,
        )
        .select_from(top)
        .join(Document, Document.id == top.c.document_id)
        # Repeated rather than inherited from the join — see the module docstring.
        .where(Document.user_id == tenant_id(user_id))
        .order_by(ordering)
    )


def chunk_vector_search(
    user_id: uuid.UUID | str,
    embedding: Sequence[float],
    *,
    limit: int = DEFAULT_BRANCH_LIMIT,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> Select:
    """Vector branch: cosine distance over the HNSW index, top ``limit``.

    The ``ORDER BY`` expression is the same one the index was built for
    (``vector_cosine_ops``), which is what lets Postgres walk the index instead
    of computing the distance for every row.

    ``document_ids`` is the allow-list from `documents_matching_filters`;
    ``None`` means no metadata filter was requested, which is not the same as an
    empty list — an empty allow-list must match nothing, and ``in_([])`` renders
    as a false constant so that falls out correctly.
    """
    tenant = tenant_id(user_id)
    distance = Chunk.embedding.cosine_distance(list(embedding))
    conditions = [Chunk.user_id == tenant, Chunk.embedding.is_not(None)]
    if document_ids is not None:
        conditions.append(Chunk.document_id.in_(list(document_ids)))
    inner = select(*_candidate_columns(distance)).where(*conditions).order_by(distance).limit(limit)
    return _attach_documents(user_id, inner, ascending=True)


def chunk_keyword_search(
    user_id: uuid.UUID | str,
    query: str,
    *,
    limit: int = DEFAULT_BRANCH_LIMIT,
    document_ids: Sequence[uuid.UUID] | None = None,
) -> Select:
    """Keyword branch: ``websearch_to_tsquery`` over the GIN-indexed ``tsv``.

    ``websearch_to_tsquery`` rather than ``plainto_tsquery`` because it accepts
    the syntax users actually type — quoted phrases, ``or``, leading ``-`` — and
    degrades gracefully instead of erroring on punctuation. The whole branch
    depends on zhparser: without it Chinese text is one token per sentence and
    this path finds nothing (spec §5 坑).

    Known limitation, measured rather than assumed: ``ix_chunks_tsv_gin`` is not
    reachable while ``chunks`` has row-level security. The policy predicate acts
    as a security barrier and ``tsvector @@ tsquery`` is not leakproof, so it
    cannot be pushed down to the index (cost 2265.80 with RLS, 38.96 without).
    This branch therefore scans its tenant's chunks. See
    ``docs/m7-index-usage-findings.md``.
    """
    tsquery = func.websearch_to_tsquery(TS_CONFIG, query)
    rank = func.ts_rank(Chunk.tsv, tsquery)
    tenant = tenant_id(user_id)
    conditions = [Chunk.user_id == tenant, Chunk.tsv.op("@@")(tsquery)]
    if document_ids is not None:
        conditions.append(Chunk.document_id.in_(list(document_ids)))
    inner = select(*_candidate_columns(rank)).where(*conditions).order_by(rank.desc()).limit(limit)
    return _attach_documents(user_id, inner, ascending=False)


def chunks_by_ids(user_id: uuid.UUID | str, chunk_ids: Sequence[int]) -> Select:
    """Fetch specific chunks, still tenant-filtered.

    RRF merges two ranked lists; this is how a caller can re-read the chunks it
    selected without losing the tenant predicate.
    """
    return scoped_chunks(user_id).where(Chunk.id.in_(list(chunk_ids)))


# ---------------------------------------------------------------------------
# repos
# ---------------------------------------------------------------------------


def repo_by_id(user_id: uuid.UUID | str, repo_id: uuid.UUID) -> Select:
    return scoped_repos(user_id).where(Repo.id == repo_id)


def enabled_repos(user_id: uuid.UUID | str) -> Select:
    return scoped_repos(user_id).where(Repo.sync_enabled.is_(True)).order_by(Repo.created_at)


def update_repo_synced_sha(user_id: uuid.UUID | str, repo_id: uuid.UUID, sha: str) -> Update:
    """Advance the diff starting point.

    Only the sync pipeline calls this, and only after every batch of jobs has
    been committed — see ``kb.sync.pipeline`` and spec §6.
    """
    return (
        update(Repo)
        .where(Repo.id == repo_id, Repo.user_id == tenant_id(user_id))
        .values(last_synced_sha=sha)
    )


def all_enabled_repos_for_worker() -> Select:
    """Every enabled repository, **across tenants**.

    The one query in this module with no tenant predicate, and it is deliberate.
    A worker has to discover work before it can know whose work it is; there is
    no tenant to scope by yet. It is therefore only ever executed on the admin
    engine, and its results are used to enqueue jobs — each of which carries its
    own ``user_id``, after which every subsequent query is tenant-scoped again.

    `repos` has an RLS policy, so running this through the application engine
    returns nothing rather than everything. That failure mode is safe, which is
    why the omission is acceptable here.
    """
    return select(Repo).where(Repo.sync_enabled.is_(True)).order_by(Repo.created_at)
