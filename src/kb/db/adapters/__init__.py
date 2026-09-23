"""Postgres implementations of the ports the pipelines are written against.

Each module here is a thin translation layer: SQL lives in
``kb.retrieval.query_builder`` (the single tenant-scoped entry point, spec §5 ②),
and these classes only move rows between that builder and the dataclasses the
domain layer speaks.

Keeping them separate from the domain code is what makes the interesting
behaviours testable without a database — the sync pipeline, the indexer and the
retrieval service all run against ports, and the fakes in ``tests/unit/fakes.py``
are the same shape as the classes here.
"""

from kb.db.adapters.blobs import (
    GitBlobSource,
    LocalBlobStore,
    RoutingBlobSource,
    UploadBlobSource,
)
from kb.db.adapters.conversion import PostgresConversionCache
from kb.db.adapters.documents import DocumentContent, DocumentSummary, PostgresDocumentReader
from kb.db.adapters.index_admin import PostgresIndexMaintenance
from kb.db.adapters.index_config import PostgresIndexConfig
from kb.db.adapters.index_writer import PostgresIndexWriter
from kb.db.adapters.repos import PostgresRepoService, RepoInfo
from kb.db.adapters.search import PostgresSearchBackend
from kb.db.adapters.tokens import IssuedToken, TokenService
from kb.db.adapters.vault import PostgresUnitOfWork, PostgresVaultStore, to_record

__all__ = [
    "DocumentContent",
    "DocumentSummary",
    "GitBlobSource",
    "IssuedToken",
    "LocalBlobStore",
    "PostgresConversionCache",
    "PostgresDocumentReader",
    "PostgresIndexConfig",
    "PostgresIndexMaintenance",
    "PostgresIndexWriter",
    "PostgresRepoService",
    "PostgresSearchBackend",
    "PostgresUnitOfWork",
    "PostgresVaultStore",
    "RepoInfo",
    "RoutingBlobSource",
    "TokenService",
    "UploadBlobSource",
    "to_record",
]
