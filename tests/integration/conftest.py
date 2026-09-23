"""Integration test fixtures — a real PostgreSQL, because mocks cannot work here.

``pgvector`` and ``zhparser`` are database extensions. There is no way to fake
them, and the tenant isolation red line (spec §11.1 #1) is enforced by an RLS
policy inside the database — a stub that returns whatever the test expects would
prove nothing at all.

The container comes from ``docker/Dockerfile.postgres``, i.e. the same image
production uses. Build it first::

    docker compose build postgres

If Docker is unavailable, or the image has not been built, these tests skip with
an actionable message rather than failing — so ``pytest tests/unit`` stays usable
without Docker. Run integration tests explicitly, or with ``-m integration``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The image built by docker/Dockerfile.postgres (docker compose build postgres).
PG_IMAGE = os.environ.get("KB_PG_IMAGE", "kb-postgres:local")

OWNER_USER = "kb"
OWNER_PASSWORD = "kb"
DB_NAME = "kb"
APP_USER = "kb_app"
APP_PASSWORD = "kb_app_pw"

PG_PORT = 5432

# Every table, so one test can never see another test's leftovers.
ALL_TABLES = (
    "sync_jobs",
    "chunks",
    "documents",
    "repos",
    "api_tokens",
    "users",
    "conversion_cache",
    "index_config",
)


@dataclass(frozen=True)
class Dsns:
    """Connection strings for the three roles used across the suite."""

    host: str
    port: int

    @property
    def app_async(self) -> str:
        """Unprivileged role — RLS applies. This is what the application uses."""
        return f"postgresql+asyncpg://{APP_USER}:{APP_PASSWORD}@{self.host}:{self.port}/{DB_NAME}"

    @property
    def admin_async(self) -> str:
        """Owner role — RLS does not apply. Fixtures and maintenance only."""
        return f"postgresql+asyncpg://{OWNER_USER}:{OWNER_PASSWORD}@{self.host}:{self.port}/{DB_NAME}"

    @property
    def admin_sync(self) -> str:
        """Owner role over psycopg2, for Alembic."""
        return f"postgresql://{OWNER_USER}:{OWNER_PASSWORD}@{self.host}:{self.port}/{DB_NAME}"


def _wait_until_initialised(dsns: Dsns, timeout: float = 180.0) -> None:
    """Block until the database is up *and* the init script has finished.

    Waiting for the log line "database system is ready to accept connections"
    is not enough: the entrypoint prints it once for the temporary server it uses
    to run the init scripts, and again for the real one. Polling for the `chinese`
    text search configuration instead proves the init script ran to completion,
    which is what the migration's preflight check depends on.
    """
    import psycopg2

    deadline = time.monotonic() + timeout
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            conn = psycopg2.connect(
                host=dsns.host,
                port=dsns.port,
                dbname=DB_NAME,
                user=OWNER_USER,
                password=OWNER_PASSWORD,
                connect_timeout=3,
            )
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last_error = exc
            time.sleep(1)
            continue

        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_ts_config WHERE cfgname = 'chinese'")
                if cur.fetchone():
                    return
            last_error = RuntimeError("init script has not created the 'chinese' configuration yet")
        except Exception as exc:  # noqa: BLE001 - retried until the deadline
            last_error = exc
        finally:
            conn.close()

        time.sleep(1)

    raise RuntimeError(f"database was never ready: {last_error}")


def _run_migrations(dsns: Dsns) -> None:
    """Apply the schema in a subprocess.

    A subprocess rather than the Alembic API on purpose: ``alembic/env.py`` reads
    the DSN from settings, which is process-wide and cached, so an in-process call
    would race with whatever the rest of the suite has already loaded.
    """
    env = {
        **os.environ,
        "DATABASE_URL": dsns.app_async,
        "DATABASE_URL_ADMIN": dsns.admin_async,
        "DATABASE_URL_SYNC": dsns.admin_sync,
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade head failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )


@pytest.fixture(scope="session")
def pg_dsns() -> Dsns:
    """Start PostgreSQL from the project image and migrate it to head."""
    try:
        from testcontainers.core.container import DockerContainer
        from testcontainers.core.exceptions import ContainerStartException
    except ImportError as exc:  # pragma: no cover - dependency is declared, this is a guard
        # Report *why* the import failed. This guard used to swallow any
        # ImportError as "testcontainers is not installed", which is how the
        # whole tenant-isolation suite came to skip silently: testcontainers 4.x
        # dropped `DockerException` from this module, so the second import blew
        # up while testcontainers was installed and working.
        pytest.skip(f"testcontainers is not importable: {exc}")

    # `docker` is a transitive dependency of testcontainers, and this is the
    # exception the SDK raises when no daemon answers.
    from docker.errors import DockerException

    container = (
        DockerContainer(PG_IMAGE)
        .with_env("POSTGRES_USER", OWNER_USER)
        .with_env("POSTGRES_PASSWORD", OWNER_PASSWORD)
        .with_env("POSTGRES_DB", DB_NAME)
        .with_env("KB_APP_PASSWORD", APP_PASSWORD)
        .with_exposed_ports(PG_PORT)
    )

    try:
        container.start()
    except (ContainerStartException, DockerException) as exc:
        pytest.skip(
            f"cannot start {PG_IMAGE}: {exc}\n"
            "Integration tests need a running Docker daemon and the project's "
            "PostgreSQL image. Start Docker, then run `docker compose build postgres`."
        )

    try:
        dsns = Dsns(host=container.get_container_host_ip(), port=int(container.get_exposed_port(PG_PORT)))
        try:
            _wait_until_initialised(dsns)
        except RuntimeError as exc:
            pytest.skip(f"{PG_IMAGE} did not finish initialising: {exc}")
        _run_migrations(dsns)
        yield dsns
    finally:
        container.stop()


@pytest.fixture
def app_engine(pg_dsns: Dsns) -> AsyncEngine:
    """Engine bound to the unprivileged application role.

    ``NullPool`` because asyncpg connections belong to the event loop that opened
    them, and pytest gives each test its own loop. Pooling across tests would
    hand a connection to a loop it does not belong to. Reuse *within* one
    connection is exercised deliberately in the isolation tests.
    """
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(pg_dsns.app_async, poolclass=NullPool)


@pytest.fixture
def app_sessionmaker(pg_dsns: Dsns) -> async_sessionmaker[AsyncSession]:
    """Session factory over the unprivileged role, so RLS is in force.

    What the adapters take in production, so tests exercise the same wiring —
    including the ``app.user_id`` binding done by ``tenant_transaction``.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker as make_sessionmaker
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(pg_dsns.app_async, poolclass=NullPool)
    return make_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def admin_engine(pg_dsns: Dsns) -> AsyncEngine:
    """Engine bound to the owner role. RLS does not apply — fixtures only."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(pg_dsns.admin_async, poolclass=NullPool)


@pytest.fixture(autouse=True)
def reset_settings_cache(pg_dsns: Dsns) -> None:
    """Point settings at the container before any code calls ``get_settings()``.

    ``lru_cache`` means the first caller wins, so the environment has to be set
    and the cache cleared before the first database access in the session.
    """
    os.environ["DATABASE_URL"] = pg_dsns.app_async
    os.environ["DATABASE_URL_ADMIN"] = pg_dsns.admin_async
    os.environ["DATABASE_URL_SYNC"] = pg_dsns.admin_sync

    from kb.config import get_settings

    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def clean_tables(admin_engine: AsyncEngine) -> None:
    """Empty every table before each test.

    Runs as the owner, so RLS does not get in the way of the cleanup. ``CASCADE``
    keeps this working as foreign keys are added, ``RESTART IDENTITY`` keeps
    bigserial ids predictable across tests.
    """
    from sqlalchemy import text

    async with admin_engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE"))
