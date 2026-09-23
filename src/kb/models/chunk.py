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
from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from kb.models.base import Base

# embedding 维度。由 provider 决定，建表时固定；换模型 = 全量重跑（spec §5 ④）
# 与 settings.embedding_dim 保持一致；模型层不读配置，避免导入期依赖环境变量。
# 与本常量、settings 默认值、迁移链终态的三方一致性由
# tests/unit/test_embedding_dim_guard.py 强制校验。
EMBEDDING_DIM = 1024

# 全文检索的 text search configuration。刻意不做成配置项：生成列的表达式会被
# 固化进 DDL，若运行时可改就会与 schema 漂移。改名意味着一次重写列 + 重建索引的迁移。
TS_CONFIG = "chinese"

# 生成列表达式：由数据库维护 tsv，Python 侧永不写入。
TSV_EXPRESSION = f"to_tsvector('{TS_CONFIG}', text)"


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "ordinal", name="uq_chunks_document_ordinal"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # 冗余存储，见模块 docstring ①；spec §5 索引清单要求 btree(user_id)
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
    # tsvector —— 由数据库生成并维护。用 Computed 而不是普通列有两个原因：
    #   ① ORM 永远不写它，索引与文本不可能不一致；
    #   ② 模型元数据与 DDL 完全一致，autogenerate 不会每次都想"修"这一列。
    # 代价：ZH 词典/配置变更后已有行不会自动重算，需重建列或 REINDEX。
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(TSV_EXPRESSION, persisted=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
