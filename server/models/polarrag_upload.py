from __future__ import annotations

import enum
from datetime import datetime

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
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid


class PolarRAGUploadStatus(str, enum.Enum):
    PREPARED = "prepared"
    UPLOADED = "uploaded"
    COMPLETED = "completed"
    ABORTED = "aborted"


class PolarRAGUploadSession(TimestampMixin, Base):
    __tablename__ = "polarrag_upload_sessions"
    __table_args__ = (
        CheckConstraint(
            "file_size_bytes >= 1 AND file_size_bytes <= 104857600",
            name="ck_polarrag_upload_sessions_file_size",
        ),
        CheckConstraint(
            "part_size_bytes >= 1 AND part_count >= 1 AND part_count <= 10000",
            name="ck_polarrag_upload_sessions_parts",
        ),
        Index("ix_polarrag_upload_sessions_expires_at", "expires_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=generate_uuid
    )
    pas_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    agent_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agents.id", ondelete="CASCADE"), index=True
    )
    knowledge_resource_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_resources.id", ondelete="CASCADE"),
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(512))
    file_type: Mapped[str] = mapped_column(String(32))
    content_type: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    file_md5: Mapped[str] = mapped_column(String(32))
    file_sha256: Mapped[str] = mapped_column(String(64))
    oss_bucket: Mapped[str] = mapped_column(String(255))
    oss_endpoint: Mapped[str] = mapped_column(String(512))
    oss_object_key: Mapped[str] = mapped_column(String(1024))
    oss_multipart_upload_id: Mapped[str] = mapped_column(
        String(512), unique=True
    )
    part_size_bytes: Mapped[int] = mapped_column(Integer)
    part_count: Mapped[int] = mapped_column(Integer)
    status: Mapped[PolarRAGUploadStatus] = mapped_column(
        Enum(
            PolarRAGUploadStatus,
            native_enum=False,
            length=16,
            validate_strings=True,
            values_callable=lambda enum_type: [item.value for item in enum_type],
        ),
        default=PolarRAGUploadStatus.PREPARED,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    doc_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    upstream_status: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )


class PolarRAGUploadCleanup(TimestampMixin, Base):
    __tablename__ = "polarrag_upload_cleanups"
    __table_args__ = (
        CheckConstraint(
            "object_state IN ('multipart', 'finalizing', 'object')",
            name="ck_polarrag_upload_cleanups_object_state",
        ),
        Index(
            "ix_polarrag_upload_cleanups_due",
            "expires_at",
            "cleanup_after",
            "cleanup_lease_until",
        ),
    )

    upload_session_id: Mapped[str] = mapped_column(
        String(36), primary_key=True
    )
    knowledge_space_id: Mapped[str] = mapped_column(String(36), index=True)
    polarrag_instance_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    oss_bucket: Mapped[str] = mapped_column(String(255))
    oss_endpoint: Mapped[str] = mapped_column(String(512))
    oss_object_key: Mapped[str] = mapped_column(String(1024))
    oss_multipart_upload_id: Mapped[str] = mapped_column(String(512))
    oss_access_key_id_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    oss_access_key_secret_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    object_state: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    cleanup_attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0"
    )
    cleanup_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cleanup_lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    operation_kind: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    operation_token: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    submission_payload_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    reconcile_required: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    cleanup_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
