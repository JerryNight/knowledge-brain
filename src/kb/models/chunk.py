"""`chunks` table — spec §5.

Two deliberate design points live here:

① `user_id` is denormalized (not joined from documents). Tenant filtering is
   the only security boundary — it must appear in the retrieval SQL itself,
   be index-backed, and be directly unit-testable.

② `embedding` is nullable: the embedding skip window (spec §8) means short
   fragments and oversized tables only enter the full-text index, never get
   vectorized.
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from kb.models.base import Base

# embedding 维度。由 provider 决定，建表时固定；换模型 = 全量重跑（spec §5 ④）
# 与 settings.embedding_dim 保持一致；模型层不读配置，避免导入期依赖环境变量。
EMBEDDING_DIM = 1536


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 冗余存储，见模块 docstring ①
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    # 返回给用户的干净原文
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # ['第三章', '3.2 认证']
    heading_path: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    # markdown: null（用 heading_path 定位）
    # pdf: {page: 12}
    # excel: {sheet: 'Sheet1', row_start: 40}
    locator: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # vector(N) — 维度由 provider 决定，建表时固定；HNSW 索引在迁移里建。
    # 可为 NULL：embedding 跳过窗口（spec §8）内的 chunk 只进全文索引。
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)
    # tsvector — GIN 索引在迁移里建；由迁移中的生成列维护，不从 Python 写入。
    tsv: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
