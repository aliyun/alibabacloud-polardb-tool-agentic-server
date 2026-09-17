from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.agent_api_token import AgentAPIToken
    from server.models.user import User


class AgentStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class AgentKnowledgeScopeMode(str, enum.Enum):
    LEGACY_ALL = "LEGACY_ALL"
    SCOPED = "SCOPED"


class Agent(TimestampMixin, Base):
    __tablename__ = "agents"
    __table_args__ = (
        CheckConstraint(
            "max_active_resources IS NULL OR max_active_resources > 0",
            name="ck_agents_max_active_resources_positive",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[AgentStatus] = mapped_column(
        Enum(AgentStatus, native_enum=False, length=32),
        default=AgentStatus.ACTIVE,
    )
    max_active_resources: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oauth_redirect_uri: Mapped[str | None] = mapped_column(Text, nullable=True)
    knowledge_scope_mode: Mapped[AgentKnowledgeScopeMode] = mapped_column(
        Enum(
            AgentKnowledgeScopeMode,
            native_enum=False,
            length=16,
            values_callable=lambda values: [item.value for item in values],
        ),
        default=AgentKnowledgeScopeMode.LEGACY_ALL,
        server_default=AgentKnowledgeScopeMode.LEGACY_ALL.value,
    )
    bulk_assignment_status: Mapped[str] = mapped_column(
        String(32), default="completed", server_default="completed"
    )
    bulk_assignment_created_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    bulk_assignment_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    bulk_assignment_worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bulk_assignment_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)

    creator: Mapped["User | None"] = relationship(lazy="selectin")
    api_token: Mapped["AgentAPIToken | None"] = relationship(
        back_populates="agent",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )
