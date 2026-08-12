from __future__ import annotations

import enum
import hashlib
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.agent import Agent
    from server.models.department import Department
    from server.models.polarrag import PolarRAGInstance
    from server.models.user import User


class AgentGroupKind(str, enum.Enum):
    DEPARTMENT = "department"
    ENTERPRISE = "enterprise"


class AgentGroupAssignment(TimestampMixin, Base):
    __tablename__ = "agent_group_assignments"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "group_key", name="uq_agent_group_assignment"
        ),
        CheckConstraint(
            "(group_kind = 'department' AND department_id IS NOT NULL "
            "AND identity_domain IS NULL AND provider IS NULL "
            "AND principal_id IS NULL) OR "
            "(group_kind = 'enterprise' AND department_id IS NULL "
            "AND identity_domain IS NOT NULL AND provider IS NOT NULL "
            "AND principal_id IS NOT NULL)",
            name="ck_agent_group_assignment_shape",
        ),
        CheckConstraint(
            "provider IS NULL OR provider IN ('feishu', 'sharepoint')",
            name="ck_agent_group_assignment_provider",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    group_kind: Mapped[AgentGroupKind] = mapped_column(
        Enum(
            AgentGroupKind,
            native_enum=False,
            length=16,
            values_callable=lambda enum_type: [item.value for item in enum_type],
        )
    )
    group_key: Mapped[str] = mapped_column(String(64))
    department_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("departments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    identity_domain: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    principal_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    agent: Mapped[Agent] = relationship(lazy="selectin")
    department: Mapped[Department | None] = relationship(lazy="selectin")
    created_by: Mapped[User | None] = relationship(lazy="selectin")

    @staticmethod
    def _key(*parts: str) -> str:
        return hashlib.sha256("\x00".join(parts).encode()).hexdigest()

    @classmethod
    def for_department(
        cls,
        *,
        agent_id: str,
        department_id: str,
        created_by_user_id: str | None,
    ) -> AgentGroupAssignment:
        return cls(
            agent_id=agent_id,
            group_kind=AgentGroupKind.DEPARTMENT,
            group_key=cls._key("department", department_id),
            department_id=department_id,
            created_by_user_id=created_by_user_id,
        )

    @classmethod
    def for_enterprise_group(
        cls,
        *,
        agent_id: str,
        identity_domain: str,
        provider: str,
        principal_id: str,
        created_by_user_id: str | None,
    ) -> AgentGroupAssignment:
        normalized_domain = identity_domain.strip()
        normalized_provider = provider.strip().lower()
        normalized_principal = principal_id.strip()
        if normalized_provider not in {"feishu", "sharepoint"}:
            raise ValueError("provider is not allowed")
        if not normalized_domain or not normalized_principal:
            raise ValueError("enterprise group identity is required")
        return cls(
            agent_id=agent_id,
            group_kind=AgentGroupKind.ENTERPRISE,
            group_key=cls._key(
                "enterprise",
                normalized_domain,
                normalized_provider,
                normalized_principal,
            ),
            identity_domain=normalized_domain,
            provider=normalized_provider,
            principal_id=normalized_principal,
            created_by_user_id=created_by_user_id,
        )


class AgentPolarRAGInstanceBinding(TimestampMixin, Base):
    __tablename__ = "agent_polarrag_instance_bindings"
    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "polarrag_instance_id",
            name="uq_agent_polarrag_instance_binding",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    polarrag_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("polarrag_instances.id", ondelete="CASCADE"),
        index=True,
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    agent: Mapped[Agent] = relationship(lazy="selectin")
    instance: Mapped[PolarRAGInstance] = relationship(lazy="selectin")
    created_by: Mapped[User | None] = relationship(lazy="selectin")


class AgentUserAssignment(TimestampMixin, Base):
    __tablename__ = "agent_user_assignments"
    __table_args__ = (
        UniqueConstraint(
            "agent_id", "user_id", name="uq_agent_user_assignment"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_direct: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )

    agent: Mapped[Agent] = relationship(lazy="selectin")
    user: Mapped[User] = relationship(
        foreign_keys=[user_id], lazy="selectin"
    )
    created_by: Mapped[User | None] = relationship(
        foreign_keys=[created_by_user_id], lazy="selectin"
    )
    token: Mapped[AgentUserToken | None] = relationship(
        back_populates="assignment",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )


class AgentUserToken(TimestampMixin, Base):
    __tablename__ = "agent_user_tokens"
    __table_args__ = (
        UniqueConstraint(
            "assignment_id", name="uq_agent_user_tokens_assignment_id"
        ),
        UniqueConstraint("token_hash", name="uq_agent_user_tokens_token_hash"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    assignment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_user_assignments.id", ondelete="CASCADE"),
        index=True,
    )
    token_prefix: Mapped[str] = mapped_column(String(32))
    token_hash: Mapped[str] = mapped_column(String(64))
    token_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    assignment: Mapped[AgentUserAssignment] = relationship(
        back_populates="token", lazy="selectin"
    )
