"""Retrieval package.

Today this holds only the tenant-scoped query builder (spec §5 ②, the application
half of the isolation). The hybrid retrieval pipeline — vector branch, keyword
branch, RRF fusion, per-document capping (spec §8) — lands here next and extends
the builders rather than opening new query sites.
"""

from kb.retrieval.query_builder import (
    TENANT_SCOPED_TABLES,
    TS_CONFIG,
    scoped_chunks,
    scoped_documents,
    scoped_repos,
    tenant_id,
)

__all__ = [
    "TENANT_SCOPED_TABLES",
    "TS_CONFIG",
    "scoped_chunks",
    "scoped_documents",
    "scoped_repos",
    "tenant_id",
]
