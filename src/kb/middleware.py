"""Bearer authentication as a pure ASGI middleware (spec §9).

**Why pure ASGI and not ``BaseHTTPMiddleware``.** ``BaseHTTPMiddleware`` runs the
downstream application in a *child task*. Context variables set before that task
is created are still visible inside it, but the reverse is not true, and the
streaming/response path is split across tasks in ways that have burned people
before. This middleware is a plain ASGI callable: the same task that authenticates
also calls the endpoint, so the principal it sets is unambiguously visible to the
MCP tools and the REST handlers, and the reset cannot leak into the next request.

**Why authenticate here and not per-endpoint.** Spec §9 requires exactly one
answer to "which tenant is this": middleware resolves the bearer token, sets
``app.user_id`` at the database layer (via the tenant-bound session the adapters
open) and ``kb.context`` at the application layer. An endpoint that forgets to
authenticate is not a cross-tenant leak — it simply has no principal and fails
closed.

Health checks are exempt. A liveness probe that needs a credential is a probe
that reports the wrong thing when the credential store is the thing that broke.
"""

from __future__ import annotations

import json
import logging

from kb.auth import parse_bearer, redact
from kb.context import Principal, reset_principal, set_principal
from kb.wiring import Services

LOGGER = logging.getLogger(__name__)

# Paths that never require a token. Everything else under /api and /mcp does.
EXEMPT_PATHS = frozenset({"/api/health"})

PROTECTED_PREFIXES = ("/api", "/mcp")

UNAUTHORIZED_BODY = json.dumps(
    {
        "error": "unauthorized",
        "message": "缺少或无效的 Bearer token。用 `kb user create --email you@example.com` 生成一个。",
    },
    ensure_ascii=False,
).encode("utf-8")


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class BearerAuthMiddleware:
    """Resolve the bearer token, or answer 401 before the app sees the request."""

    def __init__(self, app, *, services: Services) -> None:
        self.app = app
        self.services = services

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not self._protected(path):
            await self.app(scope, receive, send)
            return

        token = parse_bearer(_header(scope, b"authorization"))
        user_id = await self.services.tokens.authenticate(token) if token else None
        if user_id is None:
            if token:
                LOGGER.warning("rejected token %s for %s", redact(token), path)
            await self._unauthorized(send)
            return

        context_token = set_principal(Principal(user_id=user_id))
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(context_token)

    @staticmethod
    def _protected(path: str) -> bool:
        if path in EXEMPT_PATHS:
            return False
        return any(path == prefix or path.startswith(prefix + "/") for prefix in PROTECTED_PREFIXES)

    @staticmethod
    async def _unauthorized(send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"www-authenticate", b"Bearer"),
                    (b"content-length", str(len(UNAUTHORIZED_BODY)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})


__all__ = ["EXEMPT_PATHS", "PROTECTED_PREFIXES", "BearerAuthMiddleware"]
