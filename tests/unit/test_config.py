"""Unit tests for configuration loading and validation (M0 验收)."""

import pytest
from pydantic import ValidationError

from kb.config import Settings

APP_DSN = "postgresql+asyncpg://kb_app:kb_app_pw@localhost:5432/kb"
ADMIN_DSN = "postgresql+asyncpg://kb:kb@localhost:5432/kb"

BASE_ENV = {
    "DATABASE_URL": APP_DSN,
    "DATABASE_URL_SYNC": "postgresql://kb:kb@localhost:5432/kb",
}


def make_settings(**overrides) -> Settings:
    """Build `Settings` from explicit values only.

    `_env_file=None` is load-bearing. `Settings` declares `env_file=".env"` and
    resolves it relative to the working directory, so the README's first step
    (`cp .env.example .env`) used to turn two tests here red on a machine where
    they otherwise pass: the file supplied `DATABASE_URL_ADMIN`, which
    `test_admin_dsn_is_optional` needs absent, and `DATABASE_URL_SYNC`, which
    `test_missing_required_field_raises` needs missing.
    """
    return Settings(_env_file=None, **{**BASE_ENV, **overrides})


def test_settings_loads_required_fields():
    s = make_settings()
    assert s.database_url == APP_DSN
    assert s.database_url_sync.startswith("postgresql://")


def test_defaults_match_spec():
    """Defaults must match the values agreed in spec §8."""
    s = make_settings()
    assert s.retrieval_default_limit == 25  # 宽召回默认 25
    assert s.retrieval_max_limit == 50  # 上限 50
    assert s.rrf_k == 60  # RRF score = Σ 1/(60 + rank)
    assert s.per_document_chunk_limit == 3  # 每文档限量，防霸榜
    assert s.chunk_min_tokens == 120  # 短 section 合并阈值
    assert s.chunk_max_tokens == 800  # 超长 section 滑窗阈值
    assert s.embed_skip_min_tokens == 10  # embedding 跳过窗口下界
    assert s.embed_skip_max_tokens == 8000  # embedding 跳过窗口上界
    assert s.queue_lock_timeout_seconds == 900  # 15 分钟判孤儿


def test_missing_required_field_raises():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, DATABASE_URL="postgresql+asyncpg://x")  # 缺 DATABASE_URL_SYNC


def test_max_limit_must_be_gte_default():
    with pytest.raises(ValidationError):
        make_settings(RETRIEVAL_DEFAULT_LIMIT=30, RETRIEVAL_MAX_LIMIT=20)


def test_overlap_ratio_bounds():
    with pytest.raises(ValidationError):
        make_settings(CHUNK_OVERLAP_RATIO=1.5)
    assert make_settings(CHUNK_OVERLAP_RATIO=0.15).chunk_overlap_ratio == 0.15


def test_embed_skip_window_ordering():
    with pytest.raises(ValidationError):
        make_settings(EMBED_SKIP_MIN_TOKENS=9000, EMBED_SKIP_MAX_TOKENS=8000)


def test_negative_limit_rejected():
    with pytest.raises(ValidationError):
        make_settings(RETRIEVAL_DEFAULT_LIMIT=0)


def test_importing_package_does_not_require_env():
    """Importing kb.config must not need env vars (lint / model-only usage)."""
    import importlib

    import kb.config as cfg

    importlib.reload(cfg)
    assert callable(cfg.get_settings)


# --------------------------------------------------------------------------
# Tenant isolation depends on the app connecting as a role RLS applies to.
# These tests protect the guard that catches the opposite at startup.
# --------------------------------------------------------------------------


def test_admin_dsn_is_optional():
    """Deployments that never need elevated access must still load."""
    s = make_settings()
    assert s.database_url_admin == ""


def test_app_and_admin_roles_must_differ():
    """Same role for both DSNs == RLS silently disabled. Must not load."""
    with pytest.raises(ValidationError) as exc:
        make_settings(DATABASE_URL_ADMIN=APP_DSN)

    # The message has to say what to do, not just that something is wrong.
    assert "kb_app" in str(exc.value)


def test_role_comparison_ignores_password_and_host():
    """Only the role name matters — different passwords are still the same role."""
    with pytest.raises(ValidationError):
        make_settings(DATABASE_URL_ADMIN="postgresql+asyncpg://kb_app:other@db:5432/kb")


def test_distinct_roles_are_accepted():
    s = make_settings(DATABASE_URL_ADMIN=ADMIN_DSN)
    assert s.database_url_admin == ADMIN_DSN
