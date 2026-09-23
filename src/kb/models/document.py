"""`documents` table — spec §5.

UNIQUE (user_id, source, source_path) is the mechanism that keeps the git
channel and the upload channel independent (spec §5 ③): deleting a file in git
never touches an uploaded document, and same-name files never overwrite.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from kb.models.base import Base

# conversion_status 取值（spec §7）
CONVERSION_STATUSES = ("ok", "failed", "no_text", "unsupported")


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("user_id", "source", "source_path", name="uq_documents_user_source_path"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 不单建索引：唯一约束 (user_id, source, source_path) 的 btree 以 user_id 打头，
    # 租户过滤已经走得上（spec §5 索引清单只要求这一条）。
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # 'git' | 'upload'
    source: Mapped[str] = mapped_column(String, nullable=False)
    # 仓库内相对路径 / 上传文件名
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256(原始字节)。不做索引：三级短路是"按 (user_id, source, source_path)
    # 取出该行再比对哈希"，不存在按 content_sha 反查的查询。
    content_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    # sha256(转换后 markdown)，同时也是缓存键
    converted_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mime: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # frontmatter title 或首行
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    conversion_status: Mapped[str] = mapped_column(String, nullable=False, server_default="ok")
    conversion_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
