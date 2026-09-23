"""Application configuration — single source of truth, env-var driven.

Aligns with spec §10: config via environment variables + pydantic-settings.
"""

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _dsn_username(dsn: str) -> str:
    """Extract the role name from a SQLAlchemy/PostgreSQL DSN.

    Returns "" when the DSN carries no userinfo, so callers can treat "unknown"
    and "absent" the same way.
    """
    try:
        return urlsplit(dsn).username or ""
    except ValueError:
        return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- database ----------
    # Application runtime DSN. Must use the unprivileged role created by the
    # database image's init script (`kb_app`): RLS is bypassed for superusers,
    # and for table owners unless the table is marked FORCE. Pointing this at
    # the owner would silently disable tenant isolation (spec §5 ②).
    database_url: str = Field(alias="DATABASE_URL")
    # Owner/superuser DSN. Admin paths only: rebuild, fixtures, CLI. Optional —
    # only the code that genuinely needs elevated access should require it.
    database_url_admin: str = Field(default="", alias="DATABASE_URL_ADMIN")
    # Alembic uses the synchronous driver and needs DDL rights, so this is the
    # owner role as well.
    database_url_sync: str = Field(alias="DATABASE_URL_SYNC")

    # ---------- api / mcp ----------
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")

    # ---------- embedding ----------
    # Defaults describe the deployment this project actually runs on (Aliyun
    # Bailian, through its OpenAI-compatible route); .env is what decides in
    # practice. `embedding_dim` must agree with kb.models.chunk.EMBEDDING_DIM and
    # with the final state of the migration chain — enforced by
    # tests/unit/test_embedding_dim_guard.py.
    embedding_provider: str = Field(default="dashscope", alias="EMBEDDING_PROVIDER")
    embedding_model: str = Field(default="qwen3.7-text-embedding-flash", alias="EMBEDDING_MODEL")
    embedding_dim: int = Field(default=1024, alias="EMBEDDING_DIM")
    embedding_api_base: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1", alias="EMBEDDING_API_BASE"
    )
    embedding_api_key: str = Field(default="", alias="EMBEDDING_API_KEY")
    # The provider's real ceiling for this model is 25 texts per request (the
    # documentation claims 20); 20 keeps a margin and stays inside the docs.
    embedding_batch_size: int = Field(default=20, alias="EMBEDDING_BATCH_SIZE")
    embedding_max_concurrency: int = Field(default=8, alias="EMBEDDING_MAX_CONCURRENCY")
    query_cache_size: int = Field(default=1024, alias="QUERY_CACHE_SIZE")

    # ---------- queue ----------
    queue_lock_timeout_seconds: int = Field(default=900, alias="QUEUE_LOCK_TIMEOUT_SECONDS")
    queue_max_attempts: int = Field(default=5, alias="QUEUE_MAX_ATTEMPTS")

    # ---------- sync ----------
    sync_poll_interval_seconds: int = Field(default=300, alias="SYNC_POLL_INTERVAL_SECONDS")
    sync_batch_size: int = Field(default=500, alias="SYNC_BATCH_SIZE")
    max_file_size_bytes: int = Field(default=52_428_800, alias="MAX_FILE_SIZE_BYTES")

    # ---------- storage ----------
    # Where the bare partial clones live, one directory per repo id. Nothing
    # needs a working tree, so this is scratch space that can always be rebuilt
    # from the remote (spec §5: the index is a derivative).
    git_workdir_root: str = Field(default="var/git", alias="GIT_WORKDIR_ROOT")
    # Uploaded originals. Spec §7 约束 6 keeps conversion server-side, so the
    # original has to be kept: a parser swap must not require re-uploading.
    blob_store_path: str = Field(default="var/blobs", alias="BLOB_STORE_PATH")

    # ---------- retrieval ----------
    retrieval_default_limit: int = Field(default=25, alias="RETRIEVAL_DEFAULT_LIMIT")
    retrieval_max_limit: int = Field(default=50, alias="RETRIEVAL_MAX_LIMIT")
    rrf_k: int = Field(default=60, alias="RRF_K")
    per_document_chunk_limit: int = Field(default=3, alias="PER_DOCUMENT_CHUNK_LIMIT")

    # ---------- chunking ----------
    chunker_version: int = Field(default=1, alias="CHUNKER_VERSION")
    chunk_min_tokens: int = Field(default=120, alias="CHUNK_MIN_TOKENS")
    chunk_max_tokens: int = Field(default=800, alias="CHUNK_MAX_TOKENS")
    chunk_overlap_ratio: float = Field(default=0.15, alias="CHUNK_OVERLAP_RATIO")
    embed_skip_min_tokens: int = Field(default=10, alias="EMBED_SKIP_MIN_TOKENS")
    embed_skip_max_tokens: int = Field(default=8000, alias="EMBED_SKIP_MAX_TOKENS")

    # ---------- git ----------
    # 指向密钥存储的引用，明文绝不落库
    git_credential_ref: str = Field(default="", alias="GIT_CREDENTIAL_REF")

    # ---------- logging ----------
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @field_validator("retrieval_default_limit")
    @classmethod
    def _check_default_limit(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("RETRIEVAL_DEFAULT_LIMIT must be positive")
        return v

    @field_validator("chunk_overlap_ratio")
    @classmethod
    def _check_overlap(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError("CHUNK_OVERLAP_RATIO must be in [0, 1)")
        return v

    @model_validator(mode="after")
    def _check_cross_field_ordering(self) -> "Settings":
        """Cross-field checks must run after all fields are validated.

        Field-level validators run in declaration order, so a validator on an
        earlier-declared field cannot see a later-declared one in `info.data`.
        """
        if self.retrieval_max_limit < self.retrieval_default_limit:
            raise ValueError("RETRIEVAL_MAX_LIMIT must be >= RETRIEVAL_DEFAULT_LIMIT")
        if self.embed_skip_min_tokens >= self.embed_skip_max_tokens:
            raise ValueError("EMBED_SKIP_MIN_TOKENS must be < EMBED_SKIP_MAX_TOKENS")
        return self

    @model_validator(mode="after")
    def _check_app_role_is_unprivileged(self) -> "Settings":
        """Refuse a configuration that would silently defeat tenant isolation.

        Every retrieval path assumes RLS applies to the application's role. It
        does not when the role is a superuser, nor for a table's owner unless
        that table is marked FORCE. If the app and the admin DSN resolve to the
        same role, one of those two is happening — and the failure mode is a
        cross-tenant leak, which is exactly the red line in spec §11.1.

        Only checked when DATABASE_URL_ADMIN is set, so deployments that do not
        need an admin DSN are unaffected.
        """
        if not self.database_url_admin:
            return self
        app_role = _dsn_username(self.database_url)
        admin_role = _dsn_username(self.database_url_admin)
        if app_role and app_role == admin_role:
            raise ValueError(
                "DATABASE_URL and DATABASE_URL_ADMIN must use different roles, "
                f"but both use {app_role!r}. The application has to connect as a "
                "non-owner, non-superuser role (e.g. kb_app) or RLS will not "
                "apply and tenants can read each other's data."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Load settings once. Callers should depend on this, not on a module-level
    instance — importing this module must not require env vars to be present
    (e.g. during linting, or when only touching models)."""
    return Settings()
