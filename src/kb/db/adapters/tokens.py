"""``users`` / ``api_tokens`` — creation and authentication (spec §9).

Neither table carries an RLS policy, and that is not an oversight: token lookup
happens *before* a tenant is known, so a policy keyed on ``app.user_id`` would
make authentication impossible (see migration 0001). Both tables hold only
metadata — an email, a name, and a sha256 — so there is nothing tenant-owned to
leak.

This is the code path that turns a bearer token into the ``user_id`` that every
subsequent, tenant-scoped query depends on. It is therefore short, explicit, and
has no fallbacks: an unknown, malformed or revoked token returns ``None`` and the
caller answers 401.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kb.auth import generate_token, hash_token, looks_like_token
from kb.models import ApiToken, User

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """The one moment the plaintext exists outside the caller's memory."""

    token_id: uuid.UUID
    user_id: uuid.UUID
    name: str
    plaintext: str


class TokenService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    # -- users --------------------------------------------------------------

    async def create_user(self, email: str) -> uuid.UUID:
        """Create a user, or return the existing one for that email.

        Idempotent because the CLI is the only provisioning path in phase 1
        (spec §9): running ``kb user create`` twice for the same person should be
        a no-op, not a traceback about a unique violation.
        """
        normalised = email.strip().lower()
        async with self._sessionmaker() as session:
            async with session.begin():
                existing = (
                    await session.execute(select(User).where(User.email == normalised))
                ).scalars().one_or_none()
                if existing is not None:
                    return existing.id
                user = User(email=normalised)
                session.add(user)
                await session.flush()
                return user.id

    async def find_user_by_email(self, email: str) -> uuid.UUID | None:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(select(User).where(User.email == email.strip().lower()))
            ).scalars().one_or_none()
        return row.id if row is not None else None

    # -- tokens -------------------------------------------------------------

    async def issue_token(self, user_id: uuid.UUID, name: str) -> IssuedToken:
        """Mint a token for a user. The plaintext is returned once and never again."""
        plaintext = generate_token()
        async with self._sessionmaker() as session:
            async with session.begin():
                row = ApiToken(user_id=user_id, token_hash=hash_token(plaintext), name=name)
                session.add(row)
                await session.flush()
                token_id = row.id
        LOGGER.info("issued api token %s for user %s", token_id, user_id)
        return IssuedToken(token_id=token_id, user_id=user_id, name=name, plaintext=plaintext)

    async def authenticate(self, token: str) -> uuid.UUID | None:
        """Resolve a bearer token to its ``user_id``, or ``None``.

        Three rejections, all in one place: wrong shape (no database hit), unknown
        hash, and revoked. ``last_used_at`` is refreshed as a side effect — it is
        the only signal available for spotting a token that is no longer needed,
        and a failed write there must not fail the request.
        """
        if not looks_like_token(token):
            return None
        digest = hash_token(token)
        async with self._sessionmaker() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(ApiToken).where(
                            ApiToken.token_hash == digest,
                            ApiToken.revoked_at.is_(None),
                        )
                    )
                ).scalars().one_or_none()
                if row is None:
                    return None
                await session.execute(
                    update(ApiToken).where(ApiToken.id == row.id).values(last_used_at=datetime.now(UTC))
                )
                return row.user_id

    async def revoke_token(self, token_id: uuid.UUID) -> bool:
        async with self._sessionmaker() as session:
            async with session.begin():
                result = await session.execute(
                    update(ApiToken)
                    .where(ApiToken.id == token_id, ApiToken.revoked_at.is_(None))
                    .values(revoked_at=datetime.now(UTC))
                )
        return result.rowcount > 0


__all__ = ["IssuedToken", "TokenService"]
