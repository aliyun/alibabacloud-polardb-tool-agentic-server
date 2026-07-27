from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Enum, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.binding import (
        AgentInstanceBinding,
        DepartmentInstanceBinding,
        UserInstanceBinding,
    )
    from server.models.credential import InstanceCredential
    from server.models.provisioning_backend import ProvisioningBackend
    from server.models.user import User


class InstanceEngine(str, enum.Enum):
    POLARDB_MYSQL = "polardb_mysql"


class InstanceTopology(str, enum.Enum):
    SINGLE_TENANT = "single_tenant"
    MULTITENANT = "multitenant"


class AllocationMode(str, enum.Enum):
    AUTO_PROVISIONED = "auto_provisioned"
    POOLED = "pooled"
    REGISTERED = "registered"


class InstanceStatus(str, enum.Enum):
    CREATING = "creating"
    ACTIVE = "active"
    STOPPED = "stopped"
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
            sqlite_where=text("allocation_mode = 'AUTO_PROVISIONED' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"),
            postgresql_where=text(
                "allocation_mode = 'AUTO_PROVISIONED' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"
            ),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    cluster_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    usage: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    engine: Mapped[InstanceEngine] = mapped_column(
        Enum(InstanceEngine, native_enum=False, length=32),
        default=InstanceEngine.POLARDB_MYSQL,
    )
    topology: Mapped[InstanceTopology] = mapped_column(
        Enum(InstanceTopology, native_enum=False, length=32),
        default=InstanceTopology.SINGLE_TENANT,
    )
    allocation_mode: Mapped[AllocationMode] = mapped_column(
        Enum(AllocationMode, native_enum=False, length=32),
        default=AllocationMode.AUTO_PROVISIONED,
    )
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[InstanceStatus] = mapped_column(
        Enum(InstanceStatus, native_enum=False, length=32),
        default=InstanceStatus.ACTIVE,
    )
    provisioning_step: Mapped[ProvisioningStep | None] = mapped_column(
        Enum(ProvisioningStep, native_enum=False),
        nullable=True,
    )
    quota_held: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    owner_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)

    owner: Mapped["User | None"] = relationship(foreign_keys=[owner_user_id], lazy="selectin")
    user_bindings: Mapped[list["UserInstanceBinding"]] = relationship(
        back_populates="instance", lazy="selectin", cascade="all, delete-orphan"
    )
    department_bindings: Mapped[list["DepartmentInstanceBinding"]] = relationship(
        back_populates="instance", lazy="selectin", cascade="all, delete-orphan"
    )
    agent_bindings: Mapped[list["AgentInstanceBinding"]] = relationship(
        back_populates="instance", lazy="selectin", cascade="all, delete-orphan"
    )
    credentials: Mapped[list["InstanceCredential"]] = relationship(back_populates="instance", lazy="selectin")
    provisioning_backend: Mapped["ProvisioningBackend | None"] = relationship(
        back_populates="instance", lazy="selectin", uselist=False
    )
