"""FastAPI dependencies shared by the REST routers.

Two things every handler needs, both resolved from state established elsewhere:

* the ``Services`` container, put on ``app.state`` at startup by ``kb.main``, and
* the caller's ``user_id``, established by ``kb.middleware`` and read back through
  ``kb.context``.

Neither is re-derived here. A handler that looked up a token itself would be a
second authentication path, and the second one is always the one that forgets a
check.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Request

from kb.context import Principal, require_principal
from kb.wiring import Services


def get_services(request: Request) -> Services:
    """The process-wide service container."""
    return request.app.state.services


def get_principal() -> Principal:
    """The authenticated caller. Raises ``NotAuthenticated`` if middleware was bypassed."""
    return require_principal()


ServicesDep = Annotated[Services, Depends(get_services)]
PrincipalDep = Annotated[Principal, Depends(get_principal)]


def current_user_id(principal: PrincipalDep) -> uuid.UUID:
    return principal.user_id


UserIdDep = Annotated[uuid.UUID, Depends(current_user_id)]


__all__ = [
    "PrincipalDep",
    "ServicesDep",
    "UserIdDep",
    "current_user_id",
    "get_principal",
    "get_services",
]
