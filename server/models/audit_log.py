from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, generate_uuid, utc_now


class AuditStatus(str, enum.Enum):
    SUCCESS = "success"
    ERROR = "error"
    BLOCKED = "blocked"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), index=True)
    instance_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("instances.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(64))
    sql_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[AuditStatus] = mapped_column(Enum(AuditStatus))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sql_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    instance_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    db_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    client_info: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
