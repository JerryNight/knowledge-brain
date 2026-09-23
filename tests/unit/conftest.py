"""Unit-test isolation from the surrounding machine.

Unit tests must not depend on the developer's shell environment or on a local
`.env`. Both are easy to pick up by accident — the README's first step is
`cp .env.example .env`, and `.env` is read by `Settings` from the working
directory — and both silently change what `Settings()` resolves to.
"""

from __future__ import annotations

import pytest

from kb.config import Settings


def _settings_env_names() -> set[str]:
    """Every environment variable name `Settings` reads."""
    return {field.alias for field in Settings.model_fields.values() if isinstance(field.alias, str)}


@pytest.fixture(autouse=True)
def _no_ambient_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip settings env vars so a test only ever sees what it sets itself.

    Without this, an exported `DATABASE_URL_SYNC` makes
    `test_missing_required_field_raises` pass vacuously — it asserts that
    omitting that variable is an error.
    """
    for name in _settings_env_names():
        monkeypatch.delenv(name, raising=False)
