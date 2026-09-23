"""The *shape* of the two retrieval statements, pinned in a test.

``test_retrieval.py`` covers fusion and the service's policy decisions; both are
pure Python. What it cannot see is the SQL shape, and the shape is what decides
whether the query is fast.

The vector branch is the reason this file exists. An HNSW index scan carries a
very high startup cost, so Postgres only picks it when a ``Limit`` node can sit
directly on top of it. Move the ``LIMIT`` out of the CTE, or put the
``documents`` join back into the scoring stage, and the planner silently falls
back to "scan every chunk, sort, keep 40" — measured at 5935 ms instead of
5.7 ms on 50k chunks. Nothing else in the suite notices, so the shape is asserted
here, textually, against the compiler's own output.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from kb.retrieval import query_builder as qb

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
EMBEDDING = [0.1] * 8
DOCUMENT = uuid.UUID("22222222-2222-2222-2222-222222222222")


def compiled(statement) -> str:
    """One line, so substring assertions do not fight the pretty-printer."""
    return " ".join(str(statement.compile(dialect=postgresql.dialect())).split())


def scoring_stage(sql: str) -> str:
    """The half that runs over ``chunks`` only, before metadata is attached."""
    assert "WITH top AS " in sql, "the candidate query is no longer a CTE"
    return sql.split("WITH top AS ", 1)[1].split(") SELECT ", 1)[0]


# ---------------------------------------------------------------------------
# The vector branch — LIMIT before the join
# ---------------------------------------------------------------------------


def test_vector_candidate_limit_lands_before_the_join() -> None:
    sql = compiled(qb.chunk_vector_search(TENANT, EMBEDDING, limit=7))
    assert "LIMIT" in scoring_stage(sql)
    assert sql.index("LIMIT") < sql.index("JOIN documents")


def test_vector_scoring_stage_does_not_read_documents() -> None:
    """A join here is exactly what costs the index (~1000x on 50k chunks)."""
    assert "documents" not in scoring_stage(compiled(qb.chunk_vector_search(TENANT, EMBEDDING)))


def test_vector_branch_keeps_the_hnsw_ordering_expression() -> None:
    sql = compiled(qb.chunk_vector_search(TENANT, EMBEDDING))
    assert "chunks.embedding <=> " in sql
    assert "ORDER BY top.score ASC" in sql  # cosine distance: smaller is closer


def test_vector_branch_keeps_both_tenant_predicates() -> None:
    """The module rule: a join repeat the predicate, never inherit it."""
    sql = compiled(qb.chunk_vector_search(TENANT, EMBEDDING))
    assert "chunks.user_id" in scoring_stage(sql)
    assert "documents.user_id" in sql


# ---------------------------------------------------------------------------
# The metadata filter — resolved up front, passed as an allow-list
# ---------------------------------------------------------------------------


def test_metadata_filter_becomes_an_allow_list_on_the_scoring_stage() -> None:
    sql = compiled(qb.chunk_vector_search(TENANT, EMBEDDING, document_ids=[DOCUMENT]))
    scoring = scoring_stage(sql)
    assert "chunks.document_id IN " in scoring
    # A subquery would be de-correlated into a hash join (measured 6352 ms).
    assert "documents" not in scoring
    assert scoring.count("SELECT") == 1, "a nested subquery here is de-correlated into a hash join"


def test_no_filter_means_no_allow_list_at_all() -> None:
    """``None`` is "every document", and must not add a predicate."""
    assert "chunks.document_id IN " not in scoring_stage(compiled(qb.chunk_vector_search(TENANT, EMBEDDING)))


def test_filter_query_is_tenant_scoped_and_projects_ids_only() -> None:
    sql = compiled(qb.documents_matching_filters(TENANT, tags=["rag"], path_prefix="notes/"))
    assert "documents.user_id" in sql
    assert "documents.id" in sql
    assert "&&" in sql  # ARRAY overlap: any requested tag matches
    assert "LIKE " in sql


def test_filter_query_without_filters_selects_the_whole_tenant() -> None:
    sql = compiled(qb.documents_matching_filters(TENANT))
    assert "documents.user_id" in sql
    assert "&&" not in sql
    assert "LIKE " not in sql


# ---------------------------------------------------------------------------
# The keyword branch — same shape, opposite score direction
# ---------------------------------------------------------------------------


def test_keyword_branch_scores_without_joining_documents() -> None:
    sql = compiled(qb.chunk_keyword_search(TENANT, "向量检索 中文分词", limit=7))
    scoring = scoring_stage(sql)
    assert "chunks.tsv @@ " in scoring
    assert "documents" not in scoring
    assert sql.index("LIMIT") < sql.index("JOIN documents")


def test_keyword_branch_ranks_by_descending_ts_rank() -> None:
    sql = compiled(qb.chunk_keyword_search(TENANT, "向量检索"))
    assert "ts_rank(chunks.tsv" in sql
    assert "ORDER BY top.score DESC" in sql


def test_keyword_branch_accepts_the_same_allow_list() -> None:
    sql = compiled(qb.chunk_keyword_search(TENANT, "向量检索", document_ids=[DOCUMENT]))
    assert "chunks.document_id IN " in scoring_stage(sql)
    assert "documents" not in scoring_stage(sql)
