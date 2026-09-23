"""M7.1 acceptance: does each retrieval branch actually use its index?

Two things make this worth a script rather than a one-off EXPLAIN:

* **The statement has to be the real one.** ``LIMIT`` placement and the shape of
  the metadata filter are what decide the plan, and both are easy to "tidy" back
  into something that silently costs 1000x. So the script does not re-type the
  SQL — it runs the real ``PostgresSearchBackend`` and captures what the driver
  was handed, bound parameters included.
* **It has to run as the unprivileged role.** ``kb_app`` is what production uses
  and it is the only role that sees the RLS security barrier. Running this as the
  owner makes the keyword branch look healthy when it is not.

Exit codes: 0 pass · 1 the vector branch lost its index · 2 nothing to measure.

Usage::

    docker compose up -d postgres
    uv run python scripts/explain_retrieval.py [--user-id UUID] [--query TEXT]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import Any

import asyncpg
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kb.config import get_settings
from kb.db.adapters.search import PostgresSearchBackend

PLAN_LINE_KEYWORDS = ("Scan", "Join", "Sort", "Limit", "Execution Time")


class Capture:
    """Collects the (statement, parameters) pairs the driver is given."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, Any]] = []

    def record(self, statement: str, parameters: Any) -> None:
        self.entries.append((statement, parameters))

    def clear(self) -> None:
        self.entries.clear()

    def branch_statements(self) -> list[tuple[str, tuple]]:
        """Only the branch queries: the allow-list lookup is not under test."""
        return [
            (statement, tuple(parameters))
            for statement, parameters in self.entries
            if isinstance(parameters, tuple) and len(parameters) > 3 and "FROM chunks" in statement
        ]


def _report(label: str, plan: list[str]) -> str:
    print(f"\n--- {label}")
    for line in plan:
        stripped = line.strip()
        if any(keyword in stripped for keyword in PLAN_LINE_KEYWORDS):
            print("      " + (stripped[:100] + "…" if len(stripped) > 100 else stripped))
    return "\n".join(plan)


async def _explain(conn: asyncpg.Connection, statement: str, parameters: tuple) -> list[str]:
    rows = await conn.fetch("EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) " + statement, *parameters)
    return [row[0] for row in rows]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", help="tenant to measure; defaults to the one with most chunks")
    parser.add_argument("--query", default="向量检索 中文分词", help="keyword branch probe query")
    parser.add_argument("--limit", type=int, default=40, help="branch candidate limit")
    args = parser.parse_args()

    settings = get_settings()
    app_async = settings.database_url
    raw_dsn = app_async.replace("+asyncpg", "")
    admin_async = settings.database_url_admin
    if not admin_async:
        # Without an elevated DSN the corpus count is hidden by RLS, so the
        # tenant has to be named explicitly.
        if not args.user_id:
            print("DATABASE_URL_ADMIN is not set — pass --user-id (exit 2)")
            return 2
        admin_async = app_async

    admin = await asyncpg.connect(admin_async.replace("+asyncpg", ""))
    try:
        if args.user_id:
            tenant = uuid.UUID(args.user_id)
        else:
            row = await admin.fetchrow(
                "SELECT user_id FROM chunks GROUP BY 1 ORDER BY count(*) DESC LIMIT 1"
            )
            if row is None:
                print("no chunks visible in the database — seed a corpus first (exit 2)")
                return 2
            tenant = row["user_id"]

        corpus = await admin.fetchval("SELECT count(*) FROM chunks WHERE user_id = $1", tenant)
        index_names = [
            r["indexname"]
            for r in await admin.fetch(
                "SELECT indexname FROM pg_indexes"
                " WHERE tablename = 'chunks' AND indexdef ILIKE '%USING hnsw%'"
            )
        ]
        if not index_names:
            print("chunks has no HNSW index — wrong database? (exit 2)")
            return 2
    finally:
        await admin.close()

    print(f"tenant {tenant} · {corpus} chunks · hnsw index {index_names}")

    capture = Capture()
    engine = create_async_engine(app_async)
    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, executemany: capture.record(
            statement, parameters
        ),
    )
    backend = PostgresSearchBackend(async_sessionmaker(engine, expire_on_commit=False))
    embedding = [0.0] * settings.embedding_dim

    plans: list[str] = []
    connection = await asyncpg.connect(raw_dsn)
    try:
        for label, run in (
            (
                "vector branch",
                backend.vector_search(user_id=tenant, embedding=embedding, limit=args.limit),
            ),
            (
                "keyword branch",
                backend.keyword_search(user_id=tenant, query=args.query, limit=args.limit),
            ),
        ):
            capture.clear()
            candidates = await run
            statements = capture.branch_statements()
            if not statements:
                print(f"\n--- {label}: no branch statement captured (exit 2)")
                return 2
            print(f"\n=== {label} -> {len(candidates)} candidates")
            transaction = connection.transaction()
            await transaction.start()
            await connection.execute("SELECT set_config('app.user_id', $1, true)", str(tenant))
            for statement, parameters in statements:
                plans.append(_report(label, await _explain(connection, statement, parameters)))
            await transaction.rollback()
    finally:
        await connection.close()
        await engine.dispose()

    vector_plan = plans[0]
    if not any(name in vector_plan for name in index_names):
        print(
            "\nFAIL: the vector branch did not use the HNSW index."
            "\n      The candidate LIMIT must be applied before documents is joined"
            "\n      (see kb/retrieval/query_builder._attach_documents)."
        )
        return 1
    print("\nPASS: the vector branch uses the HNSW index.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
