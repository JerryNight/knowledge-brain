"""`conversion_cache` table — spec §5 / §7.

Keyed by sha256(original bytes) so identical attachments are converted once.
PDF conversion is seconds-to-tens-of-seconds; a full rebuild must not re-convert.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from kb.models.base import Base


class ConversionCache(Base):
    __tablename__ = "conversion_cache"

    content_sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    converted: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
