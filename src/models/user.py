from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from src.core.constants import UserRole, UserStatus
from src.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram User ID
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default=UserRole.USER.value, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=UserStatus.ACTIVE.value, nullable=False)
    total_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_jobs: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    requests = relationship("JobRequest", back_populates="user", cascade="all, delete-orphan")
