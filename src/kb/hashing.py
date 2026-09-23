"""Hashing helpers, in one place so the two hashes in the schema cannot drift.

``documents.content_sha`` and ``documents.converted_sha`` are both sha256 hex
digests (spec §5), and ``content_sha`` is simultaneously the primary key of
``conversion_cache``. Having a single function keeps "what exactly is hashed"
answerable.
"""

from __future__ import annotations

import hashlib


def sha256_hex(data: bytes) -> str:
    """sha256 of raw bytes, hex encoded."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """sha256 of a string, encoded UTF-8 first.

    Used for ``converted_sha``: the same markdown produced from the same bytes
    must hash identically regardless of which converter produced it.
    """
    return sha256_hex(text.encode("utf-8"))
