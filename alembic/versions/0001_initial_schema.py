"""initial schema — tenants, documents, chunks, indexes, RLS

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-09-22

Implements spec §5 in full. Three things in here are load-bearing and should not
be "simplified" later:

**① The `chunks.tsv` generated column.** It is declared with `GENERATED ALWAYS AS
... STORED` rather than being written from Python. That makes it impossible for
the index and the text to disagree. The expression is inlined here rather than
imported from `kb.models` on purpose: a migration has to keep working after the
application code that produced it has changed.

**② Row level security on the tenant tables.** This is the database half of the
two-layer isolation in spec §5 ② — the application query builder is the other
half. Neither alone is sufficient.

Which tables get a policy, and which deliberately do not:

* ``documents`` / ``chunks`` / ``repos`` — tenant-owned content and configuration
  reachable from retrieval and the REST admin API. RLS applies.
* ``api_tokens`` — the token lookup happens *before* a tenant is known, so a
  policy keyed on ``app.user_id`` would make authentication impossible. The table
  stores only sha256 hashes and is read by exact hash.
* ``users`` — same reason as ``api_tokens``; an email lookup precedes any tenant
  context.
* ``sync_jobs`` — the worker legitimately dequeues jobs belonging to every
  tenant. A tenant-scoped policy would break the queue, and the row holds no
  document content (only a job kind and a payload of ids).
* ``conversion_cache`` / ``index_config`` — no ``user_id`` column at all.
  ``conversion_cache`` is content-addressed and global by design; ``index_config``
  is a single global row describing which embedding model produced the index.

**③ FORCE ROW LEVEL SECURITY.** Table owners bypass RLS unless the table is
marked FORCE, and superusers bypass it unconditionally. FORCE is cheap insurance
against someone later making a non-superuser own these tables. The application
role (``kb_app``) is neither the owner nor a superuser, which is what makes any
of this real — `kb.config.settings` refuses to start if the app DSN and the admin
DSN resolve to the same role.
"""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None

# Deliberately inlined, not imported from kb.models.chunk — see module docstring ①.
TSV_EXPRESSION = "to_tsvector('chinese', text)"

# 与 kb.models.chunk.EMBEDDING_DIM 一致；写死在这里的理由同上。
EMBEDDING_DIM = 1536

# Columns used by the RLS policy. `missing_ok => true` makes an unset variable
# yield NULL instead of raising, and NULLIF maps the empty string to NULL too, so
# "no tenant context" filters every row out rather than exposing all of them.
# A malformed value still raises on the cast, which fails closed and loudly.
TENANT_PREDICATE = "user_id = NULLIF(current_setting('app.user_id', true), '')::uuid"

TENANT_TABLES = ("documents", "chunks", "repos")

# Sanity check. Without zhparser the generated column below cannot even be
# created, and without the extension the failure would be a confusing parser
# error. Fail with an explanation instead.
PREFLIGHT = """
DO $kb_preflight$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        RAISE EXCEPTION 'pgvector is not installed in this database. Build and run the image from docker/Dockerfile.postgres (docker compose up -d postgres) instead of a stock PostgreSQL image.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'zhparser') THEN
        RAISE EXCEPTION 'zhparser is not installed in this database. Without it to_tsvector treats an entire Chinese sentence as a single token, which silently kills the keyword half of hybrid retrieval. Build and run the image from docker/Dockerfile.postgres.';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese') THEN
        RAISE EXCEPTION 'the text search configuration "chinese" does not exist. It is created by docker/initdb/10-init.sh the first time a container from docker/Dockerfile.postgres initialises its data directory.';
    END IF;
END
$kb_preflight$;
"""


def upgrade() -> None:
    op.execute(PREFLIGHT)

    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # ------------------------------------------------------------- api_tokens
    op.create_table(
        "api_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_api_tokens_token_hash"),
    )

    # ------------------------------------------------------------------ repos
    op.create_table(
        "repos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("branch", sa.String(), nullable=False, server_default="main"),
        sa.Column("credential_ref", sa.String(), nullable=True),
        sa.Column("last_synced_sha", sa.String(), nullable=True),
        sa.Column("sync_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # -------------------------------------------------------------- documents
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("content_sha", sa.String(64), nullable=False),
        sa.Column("converted_sha", sa.String(64), nullable=True),
        sa.Column("mime", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("conversion_status", sa.String(), nullable=False, server_default="ok"),
        sa.Column("conversion_error", sa.Text(), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        # spec §5 ③：两条渠道互不打架，同名文件也不覆盖。该 btree 以 user_id 打头，
        # 因此租户过滤也走这条索引，不需要再单建 documents(user_id)。
        sa.UniqueConstraint("user_id", "source", "source_path", name="uq_documents_user_source_path"),
    )

    # ----------------------------------------------------------------- chunks
    op.create_table(
        "chunks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        # 冗余存储（spec §5 ①）：租户过滤必须出现在检索 SQL 本身、能走索引、能被单测验证
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("heading_path", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("locator", postgresql.JSONB(), nullable=True),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("tsv", postgresql.TSVECTOR(), sa.Computed(TSV_EXPRESSION, persisted=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),
    )

    # ------------------------------------------------------- conversion_cache
    op.create_table(
        "conversion_cache",
        sa.Column("content_sha", sa.String(64), primary_key=True),
        sa.Column("converted", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ----------------------------------------------------------- index_config
    op.create_table(
        "index_config",
        # 全局单行：固定主键，靠 PK 约束保证"只能有一行"
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("embedding_provider", sa.String(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("embedding_dim", sa.Integer(), nullable=False),
        sa.Column("chunker_version", sa.Integer(), nullable=False),
    )

    # ------------------------------------------------------------- sync_jobs
    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("repos.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # ---------------------------------------------------------------- indexes
    # Exactly the list in spec §5, plus the two auth/queue keys. Anything beyond
    # that is a cost with no measured benefit.
    op.create_index("ix_chunks_user_id", "chunks", ["user_id"])
    op.create_index("ix_sync_jobs_status_run_after", "sync_jobs", ["status", "run_after"])

    # Vector path (spec §5). cosine distance matches the `<=>` operator used by
    # the retrieval query; `m` / `ef_construction` are pinned so two builds of
    # this migration produce the same index.
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    # Keyword path (spec §5).
    op.execute("CREATE INDEX ix_chunks_tsv_gin ON chunks USING gin (tsv)")

    # ----------------------------------------------------------- grants + RLS
    # kb_app is created by docker/initdb/10-init.sh, i.e. it exists whenever the
    # database came from docker/Dockerfile.postgres.
    op.execute("GRANT USAGE ON SCHEMA public TO kb_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO kb_app")
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kb_app")
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kb_app"
    )
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO kb_app")

    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})")


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM kb_app"
    )
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE USAGE, SELECT ON SEQUENCES FROM kb_app")

    # No explicit DROP POLICY needed — dropping the table takes its policies with it.
    op.drop_table("sync_jobs")
    op.drop_table("index_config")
    op.drop_table("conversion_cache")
    op.drop_table("chunks")
    op.drop_table("documents")
    op.drop_table("repos")
    op.drop_table("api_tokens")
    op.drop_table("users")
