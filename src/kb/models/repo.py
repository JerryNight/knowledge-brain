"""`repos` table — spec §5.

Git credentials are referenced, never stored in plaintext.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from kb.models.base import Base


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(String, nullable=False)
    branch: Mapped[str] = mapped_column(String, nullable=False, server_default="main")
    # 指向密钥存储的引用，明文绝不落库
    credential_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # diff 的起点；只在整批成功后推进（spec §6）
    last_synced_sha: Mapped[str | None] = mapped_column(String, nullable=True)
    sync_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # 队列锁超时判定
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
