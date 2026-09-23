"""embedding dimension 1536 -> 1024 (provider switch)

Revision ID: 0002_embedding_dim_1024
Revises: 0001_initial_schema
Create Date: 2026-09-23

Why this migration exists
-------------------------
``chunks.embedding`` is a single vector column, so its dimension is fixed at
build time and shared by every row (spec §5 ④). Switching to an embedding
provider whose ceiling is lower therefore cannot be done by configuration alone:
pgvector rejects a 1024-dimension value into a ``vector(1536)`` column, and
``kb.indexer.embedding._parse`` rejects it one layer earlier with an explicit
error.

Measured against the incoming provider (Aliyun DashScope / Bailian, through its
OpenAI-compatible route) before this was written:

* ``qwen3.7-text-embedding-flash`` tops out at 1024 and does **not** error when
  asked for 1536 — it answers HTTP 200 with 1024 floats. A silent coercion, which
  is precisely the failure mode that making settings and schema agree is meant to
  rule out.
* Its real batch ceiling is 25, not the 20 the documentation states: 21 items
  succeed, 64 fails with "should not be larger than 25".
* ``qwen3.7-text-embedding`` *can* produce 1536 (so the parameter reference
  claiming 1536 belongs to ``text-embedding-v4`` alone is wrong). Keeping 1536
  would have avoided this migration entirely — at 4× the per-token price, which
  is why 1024 was chosen deliberately.

Why the column is emptied rather than squeezed
----------------------------------------------
Cosine distance between vectors produced by two different models is meaningless.
There is no correct way to fold a 1536-dimension vector into 1024 slots — padding
and truncating both leave the index full of geometrically meaningless rows that
would still look like valid search results. So the old vectors are discarded.

``text`` and the generated ``tsv`` column are untouched, so the keyword half of
hybrid retrieval keeps working while the indexer re-embeds. A full rebuild
(``POST /api/admin/rebuild``) is required afterwards; retrieval will return
keyword-only results for the affected tenant until it completes.

``index_config`` is cleared for the same reason: it records which model produced
the stored vectors, and after this migration that record is stale by definition.
Leaving it would make ``verify()`` refuse every subsequent write with a mismatch
the operator cannot act on.

On this installation the whole thing is a no-op — ``chunks`` was truncated during
the provider evaluation and ``index_config`` had no row — but it is written to be
correct against a populated database.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "0002_embedding_dim_1024"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

# Inlined rather than imported from `kb.models.chunk`, for the same reason as in
# 0001: a migration has to keep working after the application constants move on.
NEW_DIM = 1024
OLD_DIM = 1536


def _resize(dim: int) -> None:
    # `USING NULL` discards the old values. pgvector has no cast between vector
    # widths, so any other form of this statement would be an invention.
    op.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({dim}) USING NULL::vector({dim})")
    # The stored index configuration no longer describes the stored vectors.
    op.execute("DELETE FROM index_config")


def upgrade() -> None:
    _resize(NEW_DIM)


def downgrade() -> None:
    _resize(OLD_DIM)
