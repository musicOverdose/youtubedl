from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.core.database import Base


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), primary_key=True, default="ACTIVE")
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
