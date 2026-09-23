"""The FastAPI application (spec §9).

One process serves both surfaces: ``/mcp`` for Claude, ``/api/*`` for
administration. Spec §4's process model has exactly two entry points — ``api``
and ``worker`` — and both are built from the same ``kb.wiring.build_services``,
so a setting can never mean two different things depending on which one is
running.

**How the MCP endpoint is attached, and why not with ``app.mount``.** The MCP
streamable-HTTP transport keeps a session manager that must be started and
stopped by whoever owns the event loop. Mounting a sub-application puts its
lifespan out of reach, so the session manager would never start. Instead this
module takes the MCP routes (a single ``Route`` whose endpoint is a raw ASGI
app) and adds them to FastAPI's own router, then starts the session manager from
FastAPI's lifespan. One loop, one startup, one shutdown.

``create_app`` takes ``services`` as a parameter so tests can supply fakes and
never touch an engine. The module-level ``app`` lives in ``kb.main``, because
importing *that* requires environment variables to be present.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from kb.context import NotAuthenticated
from kb.db.engine import dispose_engines
from kb.mcp.server import build_mcp_server
from kb.middleware import BearerAuthMiddleware
from kb.routers import ALL_ROUTERS
from kb.wiring import Services, build_services

LOGGER = logging.getLogger(__name__)

TITLE = "knowledge-brain"

DESCRIPTION = (
    "Obsidian RAG knowledge base backend. "
    "`/mcp` 是给 Claude Code / Claude Desktop 用的 MCP 端点；`/api/*` 是管理接口。"
    "所有端点都需要 `Authorization: Bearer <token>`，`/api/health` 除外。"
)

# Imported from pyproject at release time in a real pipeline; a literal is one
# fewer moving part for phase 1.
VERSION = "0.1.0"


def create_app(services: Services | None = None) -> FastAPI:
    services = services or build_services()
    mcp_server = build_mcp_server(services)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.services = services
        logger = logging.getLogger("kb")
        logger.info(
            "starting %s (vector search %s)",
            TITLE,
            "enabled" if services.vector_search_enabled else "disabled — keyword only",
        )
        # Starts the streamable session manager, and stops it on the way out.
        async with mcp_server.session_manager.run():
            try:
                yield
            finally:
                await dispose_engines()

    app = FastAPI(title=TITLE, description=DESCRIPTION, version=VERSION, lifespan=lifespan)

    # Set eagerly as well as in the lifespan: a test that uses the app without
    # running startup should still resolve its dependencies.
    app.state.services = services

    app.add_middleware(BearerAuthMiddleware, services=services)

    for router in ALL_ROUTERS:
        app.include_router(router)

    # The MCP route is appended last so every /api route is matched first.
    for route in mcp_server.streamable_http_app().routes:
        app.router.routes.append(route)

    @app.exception_handler(NotAuthenticated)
    async def _unauthenticated(request: Request, exc: NotAuthenticated) -> JSONResponse:
        """Fail closed.

        Reaching here means middleware let a request through without a principal
        — a wiring bug, not a user error. Answering 401 says so plainly instead
        of raising a 500 whose stack trace suggests the server is broken.
        """
        LOGGER.error("handler reached without a principal: %s %s", request.method, request.url.path)
        return JSONResponse(status_code=401, content={"error": "unauthorized"})

    return app


__all__ = ["VERSION", "create_app"]
