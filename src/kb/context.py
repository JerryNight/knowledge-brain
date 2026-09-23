"""Request-scoped state: who is asking (spec §9).

A ``ContextVar`` rather than a thread-local or a global because the whole service
is one event loop with many concurrent requests. A context variable is copied
into each task, so two in-flight requests can never observe each other's tenant —
which is the same failure mode the RLS ``is_local => true`` trick prevents at the
database layer, one level up.

The value is set exactly once per authenticated request, by the middleware in
``kb.main``. Nothing else writes it, so "which tenant is this?" has a single
answer and a single origin.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

_current: ContextVar[Principal | None] = ContextVar("kb_current_principal", default=None)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller."""

    user_id: uuid.UUID
    token_id: uuid.UUID | None = None
    email: str | None = None


class NotAuthenticated(RuntimeError):
    """Tenant-scoped work was attempted with no authenticated principal.

    Raised rather than defaulting to anything. An unauthenticated request that
    quietly ran as "no tenant" would surface as an empty result set, i.e. it
    would look like *the knowledge base is empty* rather than *you are not
    logged in* — the same reasoning as ``MissingTenantContext``.
    """


def set_principal(principal: Principal | None):
    """Bind the principal for the current task. Returns the reset token."""
    return _current.set(principal)


def reset_principal(token) -> None:
    _current.reset(token)


def current_principal() -> Principal | None:
    return _current.get()


def require_principal() -> Principal:
    principal = _current.get()
    if principal is None:
        raise NotAuthenticated("no authenticated principal in this request context")
    return principal


__all__ = [
    "NotAuthenticated",
    "Principal",
    "current_principal",
    "require_principal",
    "reset_principal",
    "set_principal",
]
