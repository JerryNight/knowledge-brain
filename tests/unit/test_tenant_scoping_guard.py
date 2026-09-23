"""Structural guard: tenant-scoped SQL may only be written in one module.

"每个查询都要记得带上 WHERE user_id" is a convention, and conventions decay —
especially once several people are adding retrieval code. So the rule is
enforced mechanically instead: this test parses every module under ``src/kb``
and fails if it finds a hand-written ``select(Chunk)`` or a SQL string touching
``chunks`` / ``documents`` / ``repos`` anywhere outside
``kb/retrieval/query_builder.py``.

Two things are checked:

* **ORM** — an AST walk for ``select(<Model>)`` where the model is one of the
  tenant-scoped ones. AST rather than regex so it does not trip over the names
  appearing in a docstring or comment.
* **Raw SQL** — string literals that contain a SQL verb *and* a
  ``FROM``/``JOIN``/``UPDATE``/``DELETE FROM`` against one of those tables. Both
  conditions are required so that ordinary prose ("not joined from documents")
  is not mistaken for a query, and docstrings are skipped outright — a
  docstring cannot leak anything.

RLS in migration 0001 is the second layer of isolation (spec §5 ②). This layer is
the cheap one: it fails at test time, not at 3am.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from kb.retrieval.query_builder import TENANT_SCOPED_TABLES

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "kb"
ALLOWED_MODULE = SRC_ROOT / "retrieval" / "query_builder.py"

SCOPED_MODELS = frozenset({"Chunk", "Document", "Repo"})

SQL_VERB_RE = re.compile(r"\b(?:select|insert|update|delete)\b", re.IGNORECASE)
SQL_TABLE_RE = re.compile(
    r"\b(?:from|join|update|into|delete\s+from)\s+(?:" + "|".join(TENANT_SCOPED_TABLES) + r")\b",
    re.IGNORECASE,
)

_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstring_constants(tree: ast.AST) -> set[int]:
    """Ids of the ``Constant`` nodes that are docstrings.

    Identified by position (the first statement of a module/class/function), not
    by looking like prose — guessing from content is what produced a false
    positive on ``models/chunk.py``.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            if isinstance(body[0].value.value, str):
                found.add(id(body[0].value))
    return found


def find_violations(source: str, *, filename: str = "<inline>") -> list[str]:
    """Return a description for every hand-written tenant-scoped query found."""
    violations: list[str] = []
    tree = ast.parse(source)
    docstrings = _docstring_constants(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "select":
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in SCOPED_MODELS:
                        violations.append(f"{filename}:{node.lineno} hand-written select({arg.id})")

        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings or not SQL_VERB_RE.search(node.value):
                continue
            match = SQL_TABLE_RE.search(node.value)
            if match:
                violations.append(f"{filename}:{node.lineno} hand-written SQL: {match.group(0)!r}")

    return violations


def _guarded_modules() -> list[Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py") if p.resolve() != ALLOWED_MODULE)


@pytest.mark.parametrize(
    "path",
    _guarded_modules(),
    ids=lambda p: str(p.relative_to(SRC_ROOT)),
)
def test_no_hand_written_tenant_queries(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    violations = find_violations(source, filename=str(path.relative_to(SRC_ROOT)))
    assert not violations, "tenant-scoped SQL must be built in kb/retrieval/query_builder.py:\n  " + "\n  ".join(
        violations
    )


def test_query_builder_is_discovered_by_the_guard() -> None:
    """A sanity check on the glob — an empty parametrisation would pass vacuously."""
    assert _guarded_modules(), "found no modules to guard; is SRC_ROOT correct?"
    assert ALLOWED_MODULE.resolve() not in {p.resolve() for p in _guarded_modules()}


# --------------------------------------------------------------------------
# The detector itself is under test. A guard that never fires — or that fires on
# prose and gets muted — is worse than no guard, because it reads like
# protection that is actually there.
# --------------------------------------------------------------------------


def test_detector_catches_orm_select() -> None:
    assert find_violations("stmt = select(Chunk)\n")


def test_detector_catches_orm_select_via_module() -> None:
    assert find_violations("stmt = sa.select(Document)\n")


def test_detector_catches_raw_sql() -> None:
    assert find_violations('sql = "SELECT id FROM chunks WHERE 1 = 1"\n')


def test_detector_catches_lowercase_sql() -> None:
    assert find_violations('sql = "delete from documents where id = 1"\n')


def test_detector_catches_multi_line_sql() -> None:
    assert find_violations('sql = """\nselect 1\nfrom repos\n"""\n')


def test_detector_ignores_unrelated_tables_and_models() -> None:
    assert find_violations('stmt = select(User)\nsql = "SELECT id FROM users"\n') == []


def test_detector_ignores_comments() -> None:
    """Comments are not executed, so they cannot leak anything."""
    assert find_violations("# we used to do FROM chunks here, never again\n") == []


def test_detector_ignores_docstrings() -> None:
    """Regression: models/chunk.py explains itself in prose and quotes SQL tables."""
    source = '"""Chunks are not joined from documents here.\n\nSee SELECT ... FROM documents.\n"""\n'
    assert find_violations(source) == []


def test_detector_ignores_prose_strings() -> None:
    """A string with tables but no SQL verb is prose, not a query."""
    assert find_violations('note = "rows come from documents and chunks"\n') == []


def test_detector_ignores_plain_strings() -> None:
    assert find_violations('label = "documents and chunks"\n') == []
