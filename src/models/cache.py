from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.core.database import Base


class CacheEntry(Base):
    __tablename__ = "cache_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)  # VIDEO, AUDIO, SUBTITLE
    codec: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # H264, H265, MP3
    resolution: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)  # 1080p
    height: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    subtitle_lang: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    file_size: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    telegram_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
