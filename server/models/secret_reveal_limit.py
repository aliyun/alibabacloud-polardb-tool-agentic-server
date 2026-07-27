from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base


class SecretRevealLimit(Base):
    """Shared reveal budget for one administrator and one secret target."""

    __tablename__ = "secret_reveal_limits"
    __table_args__ = (
        CheckConstraint(
            "request_count > 0 AND request_count <= 5",
            name="ck_secret_reveal_limits_count",
        ),
        Index(
            "ix_secret_reveal_limits_window_started_at",
            "window_started_at",
        ),
    )

    admin_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(Integer)
