"""`index_config` table — spec §5.

Global single row. Records which embedding provider/model/dim and which
chunker version produced the current index.

Rationale (spec §5 ④): a chunk table can only hold one fixed-dimension vector
column. Comparing runtime settings against this row is how we refuse to run
against a mismatched index instead of silently corrupting retrieval.

`chunker_version` matters too: without a version number, changed chunking logic
would leave old and new chunks mixed in one table with no way to tell them apart.
"""

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from kb.models.base import Base


class IndexConfig(Base):
    __tablename__ = "index_config"

    # 全局一行，固定主键 1
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    embedding_provider: Mapped[str] = mapped_column(String, nullable=False)
    embedding_model: Mapped[str] = mapped_column(String, nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    chunker_version: Mapped[int] = mapped_column(Integer, nullable=False)
