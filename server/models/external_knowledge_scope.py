from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from server.models.base import Base, TimestampMixin, generate_uuid


class ExternalKnowledgeScopeSnapshot(TimestampMixin, Base):
    __tablename__ = "external_knowledge_scope_snapshots"
    __table_args__ = (
        Index(
            "ix_external_knowledge_scope_source",
            "identity_source_id",
            "source_scope_id",
        ),
        Index(
            "ix_external_knowledge_scope_resource",
            "knowledge_resource_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_source_id: Mapped[str] = mapped_column(String(36), index=True)
    source_scope_type: Mapped[str] = mapped_column(String(32))
    source_scope_id: Mapped[str] = mapped_column(String(255))
    source_scope_name: Mapped[str] = mapped_column(String(512), default="")
    knowledge_resource_id: Mapped[str] = mapped_column(String(36), index=True)
    generation: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ExternalKnowledgeScopeGrant(TimestampMixin, Base):
    __tablename__ = "external_knowledge_scope_grants"
    __table_args__ = (
        Index(
            "ix_external_knowledge_scope_grant_subject",
            "identity_source_id",
            "subject_type",
            "subject_id",
        ),
        Index("ix_external_knowledge_scope_grant_snapshot", "snapshot_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_uuid)
    snapshot_id: Mapped[str] = mapped_column(String(64))
    identity_source_id: Mapped[str] = mapped_column(String(36))
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(255))

    @staticmethod
    def deterministic_id(snapshot_id: str, subject_type: str, subject_id: str) -> str:
        material = "\x00".join((snapshot_id, subject_type, subject_id))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
