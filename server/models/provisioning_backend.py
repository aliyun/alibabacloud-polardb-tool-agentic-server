from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.credential import InstanceCredential
    from server.models.instance import Instance


class ProvisioningBackendStatus(str, enum.Enum):
    ACTIVE = "active"
    DRAINING = "draining"
    DISABLED = "disabled"


class ProvisioningBackend(TimestampMixin, Base):
    __tablename__ = "provisioning_backends"
    __table_args__ = (
        CheckConstraint(
            "max_active_resources > 0",
            name="ck_provisioning_backends_max_active_resources_positive",
        ),
        CheckConstraint(
            "resource_min_cpu >= 0 AND resource_max_cpu > 0 AND resource_min_cpu <= resource_max_cpu",
            name="ck_provisioning_backends_resource_cpu_range",
        ),
        CheckConstraint(
            "ddl_concurrency > 0",
            name="ck_provisioning_backends_ddl_concurrency_positive",
        ),
        CheckConstraint(
            "config_revision > 0",
            name="ck_provisioning_backends_config_revision_positive",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("instances.id", ondelete="RESTRICT"),
        unique=True,
        index=True,
    )
    admin_credential_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("instance_credentials.id", ondelete="RESTRICT"),
        unique=True,
    )
    status: Mapped[ProvisioningBackendStatus] = mapped_column(
        Enum(ProvisioningBackendStatus, native_enum=False, length=32),
        default=ProvisioningBackendStatus.ACTIVE,
    )
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_active_resources: Mapped[int] = mapped_column(Integer)
    resource_min_cpu: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    resource_max_cpu: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    ddl_concurrency: Mapped[int] = mapped_column(Integer, default=4, server_default="4")
    config_revision: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
    )

    instance: Mapped["Instance"] = relationship(back_populates="provisioning_backend", lazy="selectin")
    admin_credential: Mapped["InstanceCredential"] = relationship(lazy="selectin")
    health: Mapped["ProvisioningBackendHealth | None"] = relationship(
        back_populates="backend",
        cascade="all, delete-orphan",
        lazy="selectin",
        uselist=False,
    )


class ProvisioningBackendHealth(TimestampMixin, Base):
    __tablename__ = "provisioning_backend_health"

    backend_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("provisioning_backends.id", ondelete="CASCADE"),
        primary_key=True,
    )
    healthy: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    backend: Mapped["ProvisioningBackend"] = relationship(back_populates="health", lazy="selectin")


class ProvisioningCapacity(TimestampMixin, Base):
    __tablename__ = "provisioning_capacities"
    __table_args__ = (
        UniqueConstraint("scope_type", "scope_id", name="uq_provisioning_capacities_scope"),
        CheckConstraint(
            "active_count >= 0",
            name="ck_provisioning_capacities_active_count_nonnegative",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_uuid)
    scope_type: Mapped[str] = mapped_column(String(32))
    scope_id: Mapped[str] = mapped_column(String(64))
    active_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
