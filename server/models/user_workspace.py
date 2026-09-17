from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid


class UserWorkspace(TimestampMixin, Base):
    __tablename__ = "user_workspaces"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    user_id: Mapped[str] = mapped_column(
        String(36), index=True
    )
    default_agent_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
