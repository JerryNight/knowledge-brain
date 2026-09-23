"""REST routers — the admin surface from spec §9."""

from kb.routers.admin import router as admin_router
from kb.routers.documents import router as documents_router
from kb.routers.health import router as health_router
from kb.routers.repos import router as repos_router

ALL_ROUTERS = (health_router, repos_router, admin_router, documents_router)

__all__ = ["ALL_ROUTERS", "admin_router", "documents_router", "health_router", "repos_router"]
