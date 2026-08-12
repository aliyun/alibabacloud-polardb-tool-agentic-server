"""add PolarRAG upload sessions

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b0"
down_revision: str | Sequence[str] | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "polarrag_upload_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("pas_user_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column(
            "knowledge_resource_id", sa.String(length=36), nullable=False
        ),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("file_type", sa.String(length=32), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False),
        sa.Column("file_md5", sa.String(length=32), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("oss_bucket", sa.String(length=255), nullable=False),
        sa.Column("oss_endpoint", sa.String(length=512), nullable=False),
        sa.Column("oss_object_key", sa.String(length=1024), nullable=False),
        sa.Column(
            "oss_multipart_upload_id", sa.String(length=512), nullable=False
        ),
        sa.Column("part_size_bytes", sa.Integer(), nullable=False),
        sa.Column("part_count", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "prepared",
                "uploaded",
                "completed",
                "aborted",
                name="polarraguploadstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("doc_id", sa.String(length=512), nullable=True),
        sa.Column("upstream_status", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "file_size_bytes >= 1 AND file_size_bytes <= 104857600",
            name="ck_polarrag_upload_sessions_file_size",
        ),
        sa.CheckConstraint(
            "part_size_bytes >= 1 AND part_count >= 1 AND part_count <= 10000",
            name="ck_polarrag_upload_sessions_parts",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_resource_id"],
            ["knowledge_resources.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pas_user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("oss_multipart_upload_id"),
    )
    op.create_index(
        "ix_polarrag_upload_sessions_agent_id",
        "polarrag_upload_sessions",
        ["agent_id"],
    )
    op.create_index(
        "ix_polarrag_upload_sessions_expires_at",
        "polarrag_upload_sessions",
        ["expires_at"],
    )
    op.create_index(
        "ix_polarrag_upload_sessions_knowledge_resource_id",
        "polarrag_upload_sessions",
        ["knowledge_resource_id"],
    )
    op.create_index(
        "ix_polarrag_upload_sessions_owner_status",
        "polarrag_upload_sessions",
        ["pas_user_id", "agent_id", "status"],
    )
    op.create_index(
        "ix_polarrag_upload_sessions_pas_user_id",
        "polarrag_upload_sessions",
        ["pas_user_id"],
    )


def downgrade() -> None:
    op.drop_table("polarrag_upload_sessions")
