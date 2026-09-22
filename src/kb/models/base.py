"""SQLAlchemy declarative base and model registry.

All ORM models live in this package. `Base.metadata` is what Alembic
autogenerate compares against.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all models."""
