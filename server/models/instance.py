from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.binding import DepartmentInstanceBinding, UserInstanceBinding
    from server.models.db_account import DBAccount
    from server.models.user import User


class InstanceType(str, enum.Enum):
    PERSONAL = "personal"
    SHARED = "shared"
    MULTITENANT = "multitenant"


class InstanceStatus(str, enum.Enum):
    ACTIVE = "active"
    STOPPED = "stopped"
    CREATING = "creating"
    POOLED = "pooled"
    POOL_CREATING = "pool_creating"
    FAILED = "failed"


class ProvisioningStep(str, enum.Enum):
    PENDING = "pending"
    CLUSTER_READY = "cluster_ready"
    PASSWORD_STORED = "password_stored"
    ACCOUNT_CREATED = "account_created"
    DATABASE_CREATED = "database_created"
    ENDPOINT_RESOLVED = "endpoint_resolved"
    BOUND = "bound"
    DONE = "done"


class Instance(TimestampMixin, Base):
    __tablename__ = "instances"
    __table_args__ = (
        Index(
            "uix_user_active_personal",
            "owner_user_id",
            unique=True,
            sqlite_where=text("type = 'PERSONAL' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"),
            postgresql_where=text("type = 'PERSONAL' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    cluster_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    type: Mapped[InstanceType] = mapped_column(Enum(InstanceType))
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[InstanceStatus] = mapped_column(Enum(InstanceStatus), default=InstanceStatus.ACTIVE)
    provisioning_step: Mapped[ProvisioningStep | None] = mapped_column(Enum(ProvisioningStep), nullable=True)
    quota_held: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    owner_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)

    owner: Mapped["User | None"] = relationship(foreign_keys=[owner_user_id], lazy="selectin")
    user_bindings: Mapped[list["UserInstanceBinding"]] = relationship(back_populates="instance", lazy="selectin", cascade="all, delete-orphan")
    department_bindings: Mapped[list["DepartmentInstanceBinding"]] = relationship(back_populates="instance", lazy="selectin", cascade="all, delete-orphan")
    db_accounts: Mapped[list["DBAccount"]] = relationship(back_populates="instance", lazy="selectin", cascade="all, delete-orphan")
