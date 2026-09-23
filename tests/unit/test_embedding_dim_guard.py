"""The embedding dimension is declared in three places; they must agree.

``chunks.embedding`` is a fixed-width vector column, so its width has to be
restated every time the provider changes:

* the migration that creates the column, and any later migration that resizes it,
* ``kb.models.chunk.EMBEDDING_DIM`` (what the ORM writes),
* ``Settings.embedding_dim`` (what the embedder asks the provider for).

Disagreement between these is not a loud failure — it is a retrieval bug. A query
vector and a stored vector of different widths never compare; worse, the same
width with a different provenance compares *silently and meaninglessly*, because
cosine distance across two models carries no information. That is the failure
mode behind spec §5 ④'s "changing the model means a full rebuild".

This is not a hypothetical drift. The 2026-09-23 provider switch had the value
written down in five separate files, and the migration chain had to be reasoned
about by hand to prove what the column actually was. These assertions make the
next switch mechanical.

Text-based and simulation-based on purpose, mirroring ``test_rls_policy_guard.py``:
no database, no Docker, runs in milliseconds. The chain is simulated by replacing
the migration module's ``op`` with a recorder and calling ``upgrade()`` — so it
stays correct however a migration chooses to spell its DDL, instead of depending
on a regex over the source.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from kb.config import Settings
from kb.models.chunk import EMBEDDING_DIM, Chunk

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "alembic" / "versions"
INITIAL_MIGRATION = "0001_initial_schema.py"

REVISION = re.compile(r'^revision = "([^"]+)"', re.MULTILINE)
DOWN_REVISION = re.compile(r'^down_revision = (?:None|"([^"]+)")', re.MULTILINE)
VECTOR_WIDTH = re.compile(r"vector\((\d+)\)")


class _Recorder:
    """Stands in for ``alembic.op`` and records what a migration would execute."""

    def __init__(self) -> None:
        self.sql: list[str] = []
        self.tables: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, statement: Any, *args: Any, **kwargs: Any) -> None:
        self.sql.append(statement if isinstance(statement, str) else str(statement))

    def create_table(self, name: str, *columns: Any, **kwargs: Any) -> None:
        self.tables.append((name, columns))

    def __getattr__(self, name: str) -> Any:
        # Every other DDL entry point (create_index, alter_column, …) is
        # irrelevant to the column width; record nothing and carry on.
        def _noop(*args: Any, **kwargs: Any) -> None:
            return None

        return _noop

    def vector_widths(self) -> set[int]:
        widths = {int(w) for statement in self.sql for w in VECTOR_WIDTH.findall(statement)}
        for _, columns in self.tables:
            for column in columns:
                if isinstance(column, sa.Column) and isinstance(column.type, Vector):
                    widths.add(column.type.dim)
        return widths


def _load(migration: str) -> ModuleType:
    """Import a migration by filename — its name is not a valid identifier."""
    path = MIGRATIONS / migration
    spec = importlib.util.spec_from_file_location(migration.removesuffix(".py"), path)
    assert spec and spec.loader, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(module: ModuleType, step: str, monkeypatch: pytest.MonkeyPatch) -> set[int]:
    """Execute ``upgrade``/``downgrade`` with the DDL stubbed out, and read the
    vector widths it would have applied."""
    recorder = _Recorder()
    with monkeypatch.context() as patch:
        patch.setattr(module, "op", recorder)
        getattr(module, step)()
    return recorder.vector_widths()


def _chain() -> list[str]:
    """Migration filenames in application order, from the real revision graph."""
    by_revision: dict[str, str] = {}
    parents: dict[str, str | None] = {}
    for path in sorted(MIGRATIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision = REVISION.search(source)
        if revision is None:  # a helper module, not a migration
            continue
        down = DOWN_REVISION.search(source)
        assert down is not None, f"{path.name} declares no down_revision"
        by_revision[revision.group(1)] = path.name
        parents[revision.group(1)] = down.group(1)

    roots = [revision for revision, parent in parents.items() if parent is None]
    assert len(roots) == 1, f"expected exactly one root migration, found {roots}"

    ordered: list[str] = []
    current: str | None = roots[0]
    while current is not None:
        ordered.append(by_revision[current])
        children = [revision for revision, parent in parents.items() if parent == current]
        assert len(children) <= 1, f"migration history branches at {current}: {children}"
        current = children[0] if children else None

    assert len(ordered) == len(by_revision), (
        f"migrations unreachable from the root: {set(by_revision.values()) - set(ordered)}"
    )
    return ordered


def test_the_guard_found_the_migrations() -> None:
    """A glob that matches nothing would make every assertion below vacuous."""
    assert (MIGRATIONS / INITIAL_MIGRATION).is_file()
    assert len(_chain()) >= 1


def test_the_orm_column_uses_the_model_constant() -> None:
    """Narrow on purpose — read what it can and cannot catch.

    The ORM column is derived from ``EMBEDDING_DIM``, so this cannot fail simply
    because the constant is wrong; the two tests below compare the constant
    against things outside this module, and those are the ones that catch drift.
    What this *can* catch is the model declaration acquiring a literal
    (``Vector(1536)``) that no longer follows the constant at all.
    """
    assert Chunk.__table__.c.embedding.type.dim == EMBEDDING_DIM


def test_the_settings_default_matches_the_model_constant() -> None:
    """A fresh checkout with no EMBEDDING_DIM set must still target the real column."""
    assert Settings.model_fields["embedding_dim"].default == EMBEDDING_DIM


def test_the_migration_chain_ends_at_the_model_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the database's ``chunks.embedding`` actually is after ``upgrade head``."""
    width: int | None = None
    for name in _chain():
        widths = _run(_load(name), "upgrade", monkeypatch)
        if not widths:
            continue  # this migration does not touch the embedding column
        assert len(widths) == 1, (
            f"{name}.upgrade() touches the embedding column at more than one width "
            f"({sorted(widths)}); the guard cannot tell which one the column ends up at."
        )
        width = widths.pop()

    assert width is not None, "no migration declares the width of chunks.embedding"
    assert width == EMBEDDING_DIM, (
        f"the migration chain leaves chunks.embedding at vector({width}) but "
        f"kb.models.chunk.EMBEDDING_DIM is {EMBEDDING_DIM}. Writing {EMBEDDING_DIM}-wide "
        "vectors into the column would fail at runtime — or worse, succeed against a "
        "column built by a different model. Fix the constant and add the migration "
        "together, then run a full rebuild (spec §5 ④)."
    )


def test_every_resize_is_reversible(monkeypatch: pytest.MonkeyPatch) -> None:
    """A downgrade that does not restore the previous width makes the chain one-way.

    The root migration is excluded: its ``downgrade`` drops the table, so there is
    no previous width for it to restore — the width it establishes is the baseline
    every later migration is measured against.
    """
    chain = _chain()
    root = _run(_load(chain[0]), "upgrade", monkeypatch)
    assert len(root) == 1, (
        f"{chain[0]}.upgrade() declares {sorted(root)} as the embedding width; every "
        "later migration's reversibility is measured against it."
    )
    current = root.pop()

    for name in chain[1:]:
        module = _load(name)
        upgraded = _run(module, "upgrade", monkeypatch)
        if not upgraded:
            continue  # this migration does not touch the embedding column

        assert len(upgraded) == 1, (
            f"{name}.upgrade() touches the embedding column at more than one width: {sorted(upgraded)}"
        )
        restored = _run(module, "downgrade", monkeypatch)
        assert len(restored) == 1, (
            f"{name}.downgrade() does not restore exactly one vector width ({sorted(restored)}); "
            "there is no way back from this migration."
        )

        next_width, previous_width = upgraded.pop(), restored.pop()
        assert previous_width == current, (
            f"{name}.downgrade() restores vector({previous_width}) but the width before this "
            f"migration was vector({current})."
        )
        current = next_width


def test_the_embedder_is_built_from_settings_not_a_literal() -> None:
    """The width the provider is asked for has to be the same field the guards pin.

    ``wiring`` constructs embedders in two places — the service graph and the
    worker. A literal there would defeat every assertion above without touching
    any file they read.
    """
    source = (REPO_ROOT / "src" / "kb" / "wiring.py").read_text(encoding="utf-8")
    widths = set(re.findall(r"dim=([A-Za-z_][\w.]*)", source))

    assert widths, "wiring.py no longer builds an embedder with an explicit dim"
    assert widths == {"settings.embedding_dim"}, (
        f"wiring.py passes {sorted(widths)} as the vector width; it must come from "
        "settings.embedding_dim, the value the schema guards above verify."
    )
