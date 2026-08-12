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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from server.models.base import Base, TimestampMixin, generate_uuid

if TYPE_CHECKING:
    from server.models.user import User


class PermissionSyncMode(str, enum.Enum):
    DRY_RUN = "dry_run"
    APPLY = "apply"


class PermissionSyncStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PermissionSyncTargetStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class PermissionTemplate(TimestampMixin, Base):
    __tablename__ = "permission_templates"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    revisions: Mapped[list["PermissionTemplateRevision"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class PermissionTemplateRevision(TimestampMixin, Base):
    __tablename__ = "permission_template_revisions"
    __table_args__ = (
        UniqueConstraint(
            "template_id",
            "revision",
            name="uq_permission_template_revisions_template_revision",
        ),
        CheckConstraint(
            "revision > 0",
            name="ck_permission_template_revisions_revision_positive",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    template_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("permission_templates.id", ondelete="CASCADE"),
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer)
    privileges_json: Mapped[str] = mapped_column(Text)
    grant_option: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    template: Mapped[PermissionTemplate] = relationship(
        back_populates="revisions", lazy="selectin"
    )
    created_by: Mapped["User | None"] = relationship(lazy="selectin")


class PermissionSyncJob(TimestampMixin, Base):
    __tablename__ = "permission_sync_jobs"
    __table_args__ = (
        CheckConstraint(
            "total_count >= 0 AND completed_count >= 0 "
            "AND failed_count >= 0 "
            "AND completed_count + failed_count <= total_count",
            name="ck_permission_sync_jobs_progress",
        ),
        Index(
            "ix_permission_sync_jobs_worker_scan",
            "status",
            "next_retry_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    template_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("permission_template_revisions.id", ondelete="RESTRICT"),
        index=True,
    )
    target_scope: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str] = mapped_column(String(36), index=True)
    mode: Mapped[PermissionSyncMode] = mapped_column(
        Enum(PermissionSyncMode, native_enum=False, length=32)
    )
    status: Mapped[PermissionSyncStatus] = mapped_column(
        Enum(PermissionSyncStatus, native_enum=False, length=32),
        default=PermissionSyncStatus.PENDING,
    )
    confirmed_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    total_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    completed_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    failure_reason: Mapped[str | None] = mapped_column(
        String(2048), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    template_revision: Mapped[PermissionTemplateRevision] = relationship(
        lazy="selectin"
    )
    confirmed_by: Mapped["User | None"] = relationship(lazy="selectin")
    targets: Mapped[list["PermissionSyncTarget"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class PermissionSyncTarget(TimestampMixin, Base):
    __tablename__ = "permission_sync_targets"
    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "member_id",
            name="uq_permission_sync_targets_job_member",
        ),
        CheckConstraint(
            "retry_count >= 0",
            name="ck_permission_sync_targets_retry_nonnegative",
        ),
        Index(
            "ix_permission_sync_targets_worker_scan",
            "status",
            "next_retry_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("permission_sync_jobs.id", ondelete="CASCADE"),
        index=True,
    )
    member_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("dedicated_pool_members.id", ondelete="RESTRICT"),
        index=True,
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("db_instance_resources.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    previous_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("permission_template_revisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[PermissionSyncTargetStatus] = mapped_column(
        Enum(PermissionSyncTargetStatus, native_enum=False, length=32),
        default=PermissionSyncTargetStatus.PENDING,
    )
    change_required: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1"
    )
    retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(
        String(128), nullable=True
    )
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    worker_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    job: Mapped[PermissionSyncJob] = relationship(
        back_populates="targets", lazy="selectin"
    )
