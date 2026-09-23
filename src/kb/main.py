"""ASGI entry point: ``uvicorn kb.main:app`` (spec §4).

Kept as a one-line module so that *importing* the application factory never
requires environment variables. ``kb.app.create_app`` is the factory; this file
is the deployment surface, and it is expected to run with a real ``.env``.
"""

from __future__ import annotations

from kb.app import create_app

app = create_app()

__all__ = ["app"]
