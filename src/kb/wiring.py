"""The object graph, assembled in one place.

Every adapter is constructed here and nowhere else. That keeps two things true:

* **``api`` and ``worker`` share one wiring.** Spec §4's process model is "one
  codebase, two entry points", and that only stays honest if the two processes
  build the same services from the same settings. A second assembly site would
  drift.
* **Nothing constructs an engine at import time.** ``kb.db.engine`` caches its
  factories, and ``kb.config.settings`` refuses to load without environment
  variables — so wiring has to be a call, not a module-level constant. Importing
  anything here must stay side-effect free.

The retrieval embedder is optional **on purpose**. With no ``EMBEDDING_API_KEY``
the service still starts and search still answers through the keyword branch
alone (spec §8: one branch failing is not a failed query). It logs loudly,
because silently running half a retrieval system in production is a bug worth
noticing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.config import Settings, get_settings
from kb.converter.service import ConversionService
from kb.db.adapters.blobs import (
    GitBlobSource,
    LocalBlobStore,
    RoutingBlobSource,
    UploadBlobSource,
    default_git_factory,
)
from kb.db.adapters.conversion import PostgresConversionCache
from kb.db.adapters.documents import PostgresDocumentReader
from kb.db.adapters.index_admin import PostgresIndexMaintenance
from kb.db.adapters.index_config import PostgresIndexConfig
from kb.db.adapters.index_writer import PostgresIndexWriter
from kb.db.adapters.repos import PostgresRepoService
from kb.db.adapters.search import PostgresSearchBackend
from kb.db.adapters.tokens import TokenService
from kb.db.adapters.vault import PostgresUnitOfWork, PostgresVaultStore
from kb.db.engine import get_admin_sessionmaker, get_app_sessionmaker
from kb.indexer.chunker import ChunkOptions
from kb.indexer.embedding import EmbeddingError, OpenAICompatibleEmbedder, QueryEmbedder, QueryEmbeddingCache
from kb.indexer.service import IndexingService
from kb.queue.postgres import PostgresJobQueue
from kb.retrieval.service import RetrievalService, RetrievalSettings
from kb.sync.ports import SOURCE_GIT, SOURCE_UPLOAD
from kb.upload.service import UploadService

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Services:
    """Everything a request handler or a worker iteration might need."""

    settings: Settings
    app_sessionmaker: async_sessionmaker[AsyncSession]
    admin_sessionmaker: async_sessionmaker[AsyncSession]

    queue: PostgresJobQueue
    tokens: TokenService
    vault: PostgresVaultStore
    unit_of_work: PostgresUnitOfWork
    repos: PostgresRepoService
    documents: PostgresDocumentReader
    search_backend: PostgresSearchBackend
    retrieval: RetrievalService
    conversion_cache: PostgresConversionCache
    converter: ConversionService
    index_writer: PostgresIndexWriter
    index_config: PostgresIndexConfig
    index_maintenance: PostgresIndexMaintenance
    blob_store: LocalBlobStore
    upload_blobs: UploadBlobSource
    git_blobs: GitBlobSource
    blob_router: RoutingBlobSource
    indexer: IndexingService
    upload: UploadService
    # False when no embedding provider is configured: the service still runs,
    # but only the keyword branch answers. Surfaced on the status endpoint so
    # half-configured deployments are visible rather than merely slow.
    vector_search_enabled: bool = False


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or get_settings()
    app_sm = get_app_sessionmaker()
    admin_sm = get_admin_sessionmaker()

    queue = PostgresJobQueue.from_settings(app_sm)
    tokens = TokenService(app_sm)
    vault = PostgresVaultStore(app_sm)
    unit_of_work = PostgresUnitOfWork(app_sm, queue)
    repos = PostgresRepoService(app_sm, queue)
    documents = PostgresDocumentReader(app_sm)
    search_backend = PostgresSearchBackend(app_sm)

    conversion_cache = PostgresConversionCache(app_sm)
    converter = ConversionService(cache=conversion_cache, max_bytes=settings.max_file_size_bytes)
    index_writer = PostgresIndexWriter(app_sm)
    index_config = PostgresIndexConfig(app_sm)
    index_maintenance = PostgresIndexMaintenance(app_sm)

    blob_store = LocalBlobStore(settings.blob_store_path)
    upload_blobs = UploadBlobSource(blob_store)
    git_factory = default_git_factory(workdir_root=settings.git_workdir_root)
    git_blobs = GitBlobSource(admin_sm, git_factory=git_factory)
    blob_router = RoutingBlobSource({SOURCE_GIT: git_blobs, SOURCE_UPLOAD: upload_blobs})

    embedder = _build_embedder(settings)

    async def verify_index() -> None:
        await index_config.verify(
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            chunker_version=settings.chunker_version,
        )

    indexer = IndexingService(
        source=blob_router,
        converter=converter,
        writer=index_writer,
        embedder=embedder,
        options=ChunkOptions(
            min_tokens=settings.chunk_min_tokens,
            max_tokens=settings.chunk_max_tokens,
            overlap_ratio=settings.chunk_overlap_ratio,
            embed_skip_min_tokens=settings.embed_skip_min_tokens,
            embed_skip_max_tokens=settings.embed_skip_max_tokens,
            chunker_version=settings.chunker_version,
        ),
        verify_index=verify_index,
    )

    retrieval = RetrievalService(
        backend=search_backend,
        query_embedder=_build_query_embedder(embedder, settings),
        settings=RetrievalSettings(
            default_limit=settings.retrieval_default_limit,
            max_limit=settings.retrieval_max_limit,
            per_document_limit=settings.per_document_chunk_limit,
            rrf_k=settings.rrf_k,
        ),
    )

    return Services(
        settings=settings,
        app_sessionmaker=app_sm,
        admin_sessionmaker=admin_sm,
        queue=queue,
        tokens=tokens,
        vault=vault,
        unit_of_work=unit_of_work,
        repos=repos,
        documents=documents,
        search_backend=search_backend,
        retrieval=retrieval,
        conversion_cache=conversion_cache,
        converter=converter,
        index_writer=index_writer,
        index_config=index_config,
        index_maintenance=index_maintenance,
        blob_store=blob_store,
        upload_blobs=upload_blobs,
        git_blobs=git_blobs,
        blob_router=blob_router,
        indexer=indexer,
        upload=UploadService(unit_of_work),
        vector_search_enabled=embedder is not None,
    )


def _build_embedder(settings: Settings) -> OpenAICompatibleEmbedder | None:
    """The embedding provider, or ``None`` when none is configured.

    ``None`` is a supported configuration, not a failure: indexing still stores
    text (the keyword branch works) and search still answers. It logs loudly
    because half a retrieval system in production is a bug worth noticing, and
    ``GET /api/health`` reports it too.
    """
    if not settings.embedding_api_key:
        LOGGER.warning(
            "EMBEDDING_API_KEY is not set: indexing stores no vectors and retrieval "
            "runs keyword-only. Hybrid search needs the key."
        )
        return None
    try:
        return OpenAICompatibleEmbedder(
            api_base=settings.embedding_api_base,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
            batch_size=settings.embedding_batch_size,
            max_concurrency=settings.embedding_max_concurrency,
        )
    except EmbeddingError as exc:  # pragma: no cover - defensive
        LOGGER.warning("embedding provider unavailable (%s); continuing without vectors", exc)
        return None


def _build_query_embedder(
    embedder: OpenAICompatibleEmbedder | None, settings: Settings
) -> QueryEmbedder | None:
    """Wrap the provider in the LRU cache that only ever holds query vectors (spec §8)."""
    if embedder is None:
        return None
    return QueryEmbedder(provider=embedder, cache=QueryEmbeddingCache(max_entries=settings.query_cache_size))


__all__ = ["Services", "build_services"]
