from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid


class ProvisioningOperationBudget(TimestampMixin, Base):
    __tablename__ = "provisioning_operation_budgets"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "operation",
            "window_started_at",
            name="uq_provisioning_operation_budgets_window",
        ),
        CheckConstraint(
            "request_count >= 0",
            name="ck_provisioning_operation_budgets_count_nonnegative",
        ),
        Index(
            "ix_provisioning_operation_budgets_window_started_at",
            "window_started_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    scope_type: Mapped[str] = mapped_column(String(32))
    scope_id: Mapped[str] = mapped_column(String(64))
    operation: Mapped[str] = mapped_column(String(32))
    window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True)
    )
    request_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
