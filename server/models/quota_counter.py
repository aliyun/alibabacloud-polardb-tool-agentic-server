from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid


class QuotaCounter(TimestampMixin, Base):
    __tablename__ = "quota_counters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    scope: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    current_count: Mapped[int] = mapped_column(Integer, default=0)
    max_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
