"""The RLS decision is load-bearing — removing it must not be silent.

``docs/m7-index-usage-findings.md`` §三 records a product decision (2026-09-23):
the keyword branch does **not** use ``ix_chunks_tsv_gin`` because the RLS policy
predicate acts as a security barrier, and the branch is deliberately **not** being
fixed by turning RLS off. The tempting "fix" — drop the policy so the GIN index
becomes reachable — buys ~7 ms per query and downgrades tenant isolation from a
database guarantee ("any single layer failing leaks nothing") to an application
convention.

Nothing else in the suite would go red if someone did it. The tests that actually
exercise RLS need a real Postgres and are skipped whenever Docker is unavailable,
so the regression would land quietly. These assertions cost nothing to run and
read the migration text instead of the database.

They also pin the *app-side* half of the decision: ``kb_app`` must stay
``NOBYPASSRLS``, because a role that bypasses RLS makes every policy below it
decorative.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from kb.retrieval.query_builder import TENANT_SCOPED_TABLES

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "alembic" / "versions"
INITIAL_MIGRATION = "0001_initial_schema.py"
INITDB = REPO_ROOT / "docker" / "initdb" / "10-init.sh"

# Statements that would hand tenant isolation back to the application alone.
OFF_SWITCHES = (
    re.compile(r"DISABLE\s+ROW\s+LEVEL\s+SECURITY", re.IGNORECASE),
    re.compile(r"NO\s+FORCE\s+ROW\s+LEVEL\s+SECURITY", re.IGNORECASE),
    re.compile(r"DROP\s+POLICY\s+tenant_isolation", re.IGNORECASE),
)


def _load(migration: str) -> ModuleType:
    """Import a migration by filename — its name is not a valid identifier."""
    path = MIGRATIONS / migration
    spec = importlib.util.spec_from_file_location(migration.removesuffix(".py"), path)
    assert spec and spec.loader, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sources() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS.glob("*.py"))}


def test_the_guard_found_the_migration() -> None:
    """A glob that matches nothing would make every assertion below vacuous."""
    assert INITIAL_MIGRATION in _sources()
    assert INITDB.is_file()


def test_migration_and_query_builder_agree_on_the_tenant_tables() -> None:
    """Two independent lists of the same thing — drifting apart is a bug in itself."""
    migration_tables = set(_load(INITIAL_MIGRATION).TENANT_TABLES)
    assert migration_tables == set(TENANT_SCOPED_TABLES)


def test_tenant_tables_get_enable_force_and_a_policy() -> None:
    source = (MIGRATIONS / INITIAL_MIGRATION).read_text(encoding="utf-8")
    for template in (
        "ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        "CREATE POLICY tenant_isolation ON {table} ",
    ):
        assert template in source, (
            f"the initial migration no longer establishes isolation: {template!r} is gone. "
            "See docs/m7-index-usage-findings.md §三 — this is not a performance knob."
        )


@pytest.mark.parametrize("name", sorted(_sources()))
def test_no_migration_turns_isolation_off(name: str) -> None:
    source = _sources()[name]
    for pattern in OFF_SWITCHES:
        found = pattern.search(source)
        assert found is None, (
            f"{name} turns row-level security off ({found.group(0)!r}). "
            "Tenant isolation is a spec §11.1 red line; the keyword branch's GIN index "
            "is deliberately left unused (docs/m7-index-usage-findings.md §三)."
        )


def test_the_application_role_cannot_bypass_rls() -> None:
    source = INITDB.read_text(encoding="utf-8")
    assert "NOSUPERUSER" in source and "NOBYPASSRLS" in source
    grants_bypass = re.search(r"ALTER\s+ROLE\s+kb_app[^;]*\sBYPASSRLS", source)
    assert grants_bypass is None, (
        f"kb_app is granted BYPASSRLS ({grants_bypass.group(0)!r}); every policy in the "
        "migration becomes decorative the moment that is true."
    )
