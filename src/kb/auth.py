"""Token generation, hashing and bearer parsing — pure functions (spec §9).

Kept free of FastAPI and SQLAlchemy so the security-relevant rules are testable
in isolation:

* **The plaintext token is never stored.** Only ``sha256(token)`` reaches the
  database, and the plaintext is shown to the user exactly once, at creation
  time (spec §9). A leaked database dump therefore yields no usable credentials.
* **Plain sha256, not a password hash.** Deliberate: the token is 32 bytes of
  CSPRNG output, so there is no low-entropy guess space for a slow KDF to
  protect. A salted hash would also break the O(1) lookup by ``token_hash``,
  which is the whole reason the column is unique.
* **The lookup is by exact hash**, which is what makes the ``token_hash`` unique
  index a real authentication primitive rather than a nicety.
"""

from __future__ import annotations

import hashlib
import secrets

# Identifiable prefix so a leaked token can be recognised in a log or a scanner
# finding, and so it is obvious which system it belongs to.
TOKEN_PREFIX = "kb_"

# 32 bytes of urandom → 43 url-safe characters. Comfortably beyond guessing.
TOKEN_BYTES = 32

BEARER_SCHEME = "bearer"


def generate_token() -> str:
    """A fresh plaintext token. Shown once, never persisted."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"


def hash_token(token: str) -> str:
    """The value stored in ``api_tokens.token_hash``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_token(value: str) -> bool:
    """Cheap shape check before hitting the database.

    Explicitly *not* a security control — ``hash_token`` followed by an indexed
    lookup is. This only avoids a pointless query for obviously malformed input,
    the kind a misconfigured client sends on every request.
    """
    return value.startswith(TOKEN_PREFIX) and len(value) > len(TOKEN_PREFIX) + 16


def parse_bearer(header: str | None) -> str | None:
    """Extract the token from an ``Authorization`` header.

    Case-insensitive on the scheme, per RFC 7235. A missing or malformed header
    returns ``None`` so the caller answers 401 in exactly one place.
    """
    if not header:
        return None
    scheme, _, credentials = header.partition(" ")
    if scheme.strip().lower() != BEARER_SCHEME:
        return None
    token = credentials.strip()
    return token or None


def redact(token: str) -> str:
    """A safe form for logs: prefix plus a short fingerprint, never the secret."""
    if len(token) <= 6:
        return "***"
    return f"{token[:3]}…{hash_token(token)[:8]}"


__all__ = [
    "BEARER_SCHEME",
    "TOKEN_PREFIX",
    "generate_token",
    "hash_token",
    "looks_like_token",
    "parse_bearer",
    "redact",
]
