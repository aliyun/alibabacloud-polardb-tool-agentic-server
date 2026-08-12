from __future__ import annotations

import enum
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.db_instance_resource import DBInstanceResource
    from server.models.instance import Instance
    from server.models.permission_template import PermissionTemplateRevision
    from server.models.provisioning_backend import ProvisioningBackend


class DedicatedPoolStatus(str, enum.Enum):
    ACTIVE = "active"
    DRAINING = "draining"
    DISABLED = "disabled"


class DedicatedMemberStatus(str, enum.Enum):
    REPLENISHING = "replenishing"
    AVAILABLE = "available"
    ALLOCATED_PREPARING = "allocated_preparing"
    ALLOCATED = "allocated"
    COOLING_DOWN = "cooling_down"
    SANITIZING = "sanitizing"
    QUARANTINED = "quarantined"
    DELETING = "deleting"
    DELETED = "deleted"


class ReadinessStatus(str, enum.Enum):
    FRESH = "fresh"
    STALE = "stale"
    CHECKING = "checking"


class DedicatedPreparationStep(str, enum.Enum):
    PENDING = "pending"
    PURCHASE_INTENT_STORED = "purchase_intent_stored"
    PURCHASE_REQUESTED = "purchase_requested"
    CLUSTER_READY = "cluster_ready"
    ENDPOINT_RESOLVED = "endpoint_resolved"
    LIFECYCLE_ACCOUNT_STORED = "lifecycle_account_stored"
    LIFECYCLE_ACCOUNT_CREATED = "lifecycle_account_created"
    SANDBOX_ACCOUNT_STORED = "sandbox_account_stored"
    SANDBOX_ACCOUNT_CREATED = "sandbox_account_created"
    DATABASE_CREATED = "database_created"
    OPENAPI_READY = "openapi_ready"
    PRIVILEGES_GRANTED = "privileges_granted"
    VERIFIED = "verified"


class ReclaimPolicy(str, enum.Enum):
    DESTROY = "destroy"
    SANITIZE_AND_REUSE = "sanitize_and_reuse"


class LifecycleAdministratorPolicy(str, enum.Enum):
    PAS_MANAGED = "pas_managed"
    ADMIN_PROVIDED = "admin_provided"


def validate_readiness_schedule(
    *,
    check_interval_seconds: int,
    stale_after_seconds: int,
) -> None:
    if check_interval_seconds <= 0:
        raise ValueError("health check interval must be positive")
    if stale_after_seconds < 2 * check_interval_seconds:
        raise ValueError(
            "maximum evidence age must be at least twice the health check interval"
        )


class DedicatedPool(TimestampMixin, Base):
    __tablename__ = "dedicated_pools"
    __table_args__ = (
        CheckConstraint(
            "target_size >= 0 AND max_total_members > 0 "
            "AND target_size <= max_total_members",
            name="ck_dedicated_pools_size_range",
        ),
        CheckConstraint(
            "max_member_purchases_per_hour > 0",
            name="ck_dedicated_pools_purchase_budget_positive",
        ),
        CheckConstraint(
            "max_create_requests_per_agent_per_hour > 0",
            name="ck_dedicated_pools_create_budget_positive",
        ),
        CheckConstraint(
            "max_delete_requests_per_agent_per_hour > 0",
            name="ck_dedicated_pools_delete_budget_positive",
        ),
        CheckConstraint(
            "delete_cooldown_duration_hours IS NULL "
            "OR delete_cooldown_duration_hours >= 1",
            name="ck_dedicated_pools_cooldown_positive",
        ),
        CheckConstraint(
            "available_health_check_interval_seconds > 0 "
            "AND available_health_stale_after_seconds > 0",
            name="ck_dedicated_pools_health_intervals_positive",
        ),
        CheckConstraint(
            "config_revision > 0",
            name="ck_dedicated_pools_config_revision_positive",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    name: Mapped[str] = mapped_column(String(255), unique=True)
    status: Mapped[DedicatedPoolStatus] = mapped_column(
        Enum(DedicatedPoolStatus, native_enum=False, length=32),
        default=DedicatedPoolStatus.ACTIVE,
    )
    target_size: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    max_total_members: Mapped[int] = mapped_column(Integer)
    max_member_purchases_per_hour: Mapped[int] = mapped_column(Integer)
    max_create_requests_per_agent_per_hour: Mapped[int] = mapped_column(Integer)
    max_delete_requests_per_agent_per_hour: Mapped[int] = mapped_column(Integer)
    purchase_config_json: Mapped[str] = mapped_column(Text)
    purchase_profile_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    purchase_profile_revision: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    storage_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region_id: Mapped[str] = mapped_column(String(64))
    vpc_id: Mapped[str] = mapped_column(String(64))
    vswitch_id: Mapped[str] = mapped_column(String(64))
    zone_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    security_ip_list: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    endpoint_net_type: Mapped[str] = mapped_column(
        String(32), default="Private", server_default="Private"
    )
    reclaim_policy: Mapped[ReclaimPolicy] = mapped_column(
        Enum(ReclaimPolicy, native_enum=False, length=32),
        default=ReclaimPolicy.DESTROY,
        server_default="DESTROY",
    )
    lifecycle_admin_policy: Mapped[LifecycleAdministratorPolicy] = (
        mapped_column(
            Enum(
                LifecycleAdministratorPolicy,
                native_enum=False,
                length=32,
            ),
            default=LifecycleAdministratorPolicy.PAS_MANAGED,
            server_default="PAS_MANAGED",
        )
    )
    permission_template_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("permission_template_revisions.id", ondelete="RESTRICT"),
        index=True,
    )
    account_name_template: Mapped[str] = mapped_column(
        String(64), default="agentic", server_default="agentic"
    )
    database_name_template: Mapped[str] = mapped_column(
        String(64), default="agentic", server_default="agentic"
    )
    delete_cooldown_duration_hours: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    available_health_check_interval_seconds: Mapped[int] = mapped_column(
        Integer, default=300, server_default="300"
    )
    available_health_stale_after_seconds: Mapped[int] = mapped_column(
        Integer, default=600, server_default="600"
    )
    config_revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1"
    )

    permission_template_revision: Mapped["PermissionTemplateRevision"] = (
        relationship(lazy="selectin")
    )
    members: Mapped[list["DedicatedPoolMember"]] = relationship(
        back_populates="pool",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    provisioning_backend: Mapped["ProvisioningBackend | None"] = relationship(
        back_populates="dedicated_pool", lazy="selectin", uselist=False
    )


class DedicatedWorkerHeartbeat(Base):
    __tablename__ = "dedicated_worker_heartbeats"
    __table_args__ = (
        CheckConstraint(
            "config_revision > 0",
            name="ck_dedicated_worker_heartbeats_config_revision_positive",
        ),
        Index(
            "ix_dedicated_worker_heartbeats_last_heartbeat_at",
            "last_heartbeat_at",
        ),
    )

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_revision: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    last_heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )


class DedicatedPoolMember(TimestampMixin, Base):
    __tablename__ = "dedicated_pool_members"
    __table_args__ = (
        UniqueConstraint(
            "instance_id", name="uq_dedicated_pool_members_instance_id"
        ),
        UniqueConstraint(
            "allocated_resource_id",
            name="uq_dedicated_pool_members_allocated_resource_id",
        ),
        CheckConstraint(
            "delete_cooldown_duration_hours IS NULL "
            "OR delete_cooldown_duration_hours >= 1",
            name="ck_dedicated_pool_members_cooldown_positive",
        ),
        CheckConstraint(
            "retry_count >= 0",
            name="ck_dedicated_pool_members_retry_count_nonnegative",
        ),
        Index(
            "ix_dedicated_pool_members_allocation",
            "pool_id",
            "status",
            "readiness_status",
            "last_ready_verified_at",
        ),
        Index(
            "ix_dedicated_pool_members_worker_scan",
            "status",
            "next_retry_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    pool_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("dedicated_pools.id", ondelete="CASCADE"),
        index=True,
    )
    instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("instances.id", ondelete="RESTRICT"),
        index=True,
    )
    status: Mapped[DedicatedMemberStatus] = mapped_column(
        Enum(DedicatedMemberStatus, native_enum=False, length=32),
        default=DedicatedMemberStatus.REPLENISHING,
    )
    readiness_status: Mapped[ReadinessStatus] = mapped_column(
        Enum(ReadinessStatus, native_enum=False, length=32),
        default=ReadinessStatus.STALE,
    )
    preparation_step: Mapped[DedicatedPreparationStep] = mapped_column(
        Enum(DedicatedPreparationStep, native_enum=False, length=64),
        default=DedicatedPreparationStep.PENDING,
        server_default="PENDING",
    )
    purchase_token: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True
    )
    cloud_request_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    agentic_db_cluster_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    last_ready_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lifecycle_username_ciphertext: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    lifecycle_password_ciphertext: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    sandbox_username_ciphertext: Mapped[str | None] = mapped_column(
        String(1024), nullable=True
    )
    sandbox_password_ciphertext: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    database_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    permission_template_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("permission_template_revisions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    permission_snapshot_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    allocated_resource_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("db_instance_resources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    delete_cooldown_duration_hours: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    pool: Mapped[DedicatedPool] = relationship(
        back_populates="members", lazy="selectin"
    )
    instance: Mapped["Instance"] = relationship(
        back_populates="dedicated_pool_member", lazy="selectin"
    )
    permission_template_revision: Mapped["PermissionTemplateRevision | None"] = (
        relationship(lazy="selectin")
    )
    allocated_resource: Mapped["DBInstanceResource | None"] = relationship(
        lazy="selectin"
    )
