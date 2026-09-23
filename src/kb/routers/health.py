"""``GET /api/health`` — liveness (spec §9).

Deliberately **not** authenticated (see ``kb.middleware.EXEMPT_PATHS``) and
deliberately **not** touching the database. A liveness probe should answer one
question: is this process serving? If it also pings Postgres it becomes a
readiness probe wearing the wrong name, and a transient database blip will
restart a perfectly healthy container.

What it does report is configuration that silently degrades the service — above
all ``vector_search_enabled``. A deployment with no ``EMBEDDING_API_KEY`` looks
fine from the outside while answering every query with half the retrieval
system, so that fact belongs somewhere a human will see it.
"""

from __future__ import annotations

from fastapi import APIRouter

from kb.deps import ServicesDep

router = APIRouter(tags=["health"])

VERSION = "0.1.0"


@router.get("/api/health")
async def health(services: ServicesDep) -> dict:
    settings = services.settings
    return {
        "status": "ok",
        "version": VERSION,
        "vector_search_enabled": services.vector_search_enabled,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "chunker_version": settings.chunker_version,
    }
