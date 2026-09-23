"""Unit tests for token handling (spec §9).

The security-relevant properties are all pure functions, so they are tested
directly rather than through a database:

* the stored value is a sha256, and the plaintext is not recoverable from it;
* two tokens never collide, and a token's shape check is not the control;
* ``parse_bearer`` is case-insensitive on the scheme and rejects everything else;
* logging a token cannot leak it.
"""

from __future__ import annotations

from kb.auth import (
    TOKEN_PREFIX,
    generate_token,
    hash_token,
    looks_like_token,
    parse_bearer,
    redact,
)


def test_generated_tokens_are_unique_and_prefixed() -> None:
    tokens = {generate_token() for _ in range(200)}
    assert len(tokens) == 200
    assert all(token.startswith(TOKEN_PREFIX) for token in tokens)


def test_generated_tokens_are_long_enough_to_be_unguessable() -> None:
    # 32 bytes of urandom → 43 url-safe characters, plus the prefix.
    assert len(generate_token()) >= len(TOKEN_PREFIX) + 40


def test_the_stored_hash_is_a_sha256_not_the_token() -> None:
    token = generate_token()
    digest = hash_token(token)
    assert digest != token
    assert len(digest) == 64
    assert token not in digest


def test_hashing_is_deterministic_which_is_what_makes_the_lookup_an_index_hit() -> None:
    token = generate_token()
    assert hash_token(token) == hash_token(token)


def test_different_tokens_hash_differently() -> None:
    assert hash_token(generate_token()) != hash_token(generate_token())


def test_shape_check_accepts_a_real_token_and_rejects_rubbish() -> None:
    assert looks_like_token(generate_token())
    assert not looks_like_token("")
    assert not looks_like_token("not-a-token")
    assert not looks_like_token(TOKEN_PREFIX + "short")


def test_parse_bearer_extracts_the_token() -> None:
    assert parse_bearer("Bearer kb_abc") == "kb_abc"


def test_parse_bearer_is_case_insensitive_on_the_scheme() -> None:
    """RFC 7235: the auth scheme is a case-insensitive token."""
    assert parse_bearer("bearer kb_abc") == "kb_abc"
    assert parse_bearer("BEARER kb_abc") == "kb_abc"


def test_parse_bearer_rejects_missing_or_wrong_schemes() -> None:
    assert parse_bearer(None) is None
    assert parse_bearer("") is None
    assert parse_bearer("Basic kb_abc") is None
    assert parse_bearer("Bearer") is None
    assert parse_bearer("Bearer   ") is None


def test_redaction_keeps_a_fingerprint_but_not_the_secret() -> None:
    token = generate_token()
    redacted = redact(token)
    assert token not in redacted
    assert redacted.startswith(TOKEN_PREFIX)
    assert hash_token(token)[:8] in redacted


def test_redaction_of_a_short_string_reveals_nothing() -> None:
    assert redact("short") == "***"
