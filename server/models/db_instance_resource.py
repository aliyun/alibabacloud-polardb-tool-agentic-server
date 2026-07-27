from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin
from server.models.instance import InstanceEngine

if TYPE_CHECKING:
    from server.models.credential import InstanceCredential


class DBInstanceStatus(str, enum.Enum):
    CREATING = "creating"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"
    DELETED = "deleted"
    DELETE_FAILED = "delete_failed"


class LeaseProvisioningStep(str, enum.Enum):
    PENDING = "pending"
    RESOURCE_CONFIG_CREATED = "resource_config_created"
    TENANT_CREATED = "tenant_created"
    USER_CREATED = "user_created"
    DATABASE_CREATED = "database_created"
    GRANTED = "granted"
    VERIFIED = "verified"


class LeaseCleanupStep(str, enum.Enum):
    PENDING = "pending"
    DATABASE_DROPPED = "database_dropped"
    TENANT_DROPPED = "tenant_dropped"
    RESOURCE_CONFIG_DROPPED = "resource_config_dropped"
    RESIDUE_VERIFIED = "residue_verified"


def generate_db_instance_id() -> str:
    return f"dbi-{uuid.uuid4().hex}"


class DBInstanceResource(TimestampMixin, Base):
    __tablename__ = "db_instance_resources"
    __table_args__ = (
        UniqueConstraint(
            "owner_agent_id",
            "client_token",
            name="uq_db_instance_resources_agent_client_token",
        ),
        UniqueConstraint("tenant_name", name="uq_db_instance_resources_tenant_name"),
        Index("ix_db_instance_resources_worker_scan", "status", "next_retry_at"),
        Index("ix_db_instance_resources_capacity", "backend_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=generate_db_instance_id)
    owner_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id"), index=True)
    backend_id: Mapped[str] = mapped_column(String(36), ForeignKey("provisioning_backends.id"), index=True)
    client_token: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    fingerprint_version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    engine: Mapped[InstanceEngine] = mapped_column(
        Enum(InstanceEngine, native_enum=False, length=32),
        default=InstanceEngine.POLARDB_MYSQL,
    )
    status: Mapped[DBInstanceStatus] = mapped_column(
        Enum(DBInstanceStatus, native_enum=False, length=32),
        default=DBInstanceStatus.CREATING,
    )
    tenant_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_config_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    database_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provisioning_step: Mapped[LeaseProvisioningStep] = mapped_column(
        Enum(LeaseProvisioningStep, native_enum=False, length=64),
        default=LeaseProvisioningStep.PENDING,
    )
    cleanup_step: Mapped[LeaseCleanupStep] = mapped_column(
        Enum(LeaseCleanupStep, native_enum=False, length=64),
        default=LeaseCleanupStep.PENDING,
    )
    cleanup_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    failure_reason: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    capacity_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    credentials: Mapped[list["InstanceCredential"]] = relationship(
        back_populates="resource",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
