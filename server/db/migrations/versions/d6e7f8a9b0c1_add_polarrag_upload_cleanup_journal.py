"""add PolarRAG upload cleanup journal

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6e7f8a9b0c1"
down_revision: str | Sequence[str] | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "polarrag_upload_cleanups",
        sa.Column("upload_session_id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_space_id", sa.String(length=36), nullable=False),
        sa.Column("oss_bucket", sa.String(length=255), nullable=False),
        sa.Column("oss_endpoint", sa.String(length=512), nullable=False),
        sa.Column("oss_object_key", sa.String(length=1024), nullable=False),
        sa.Column(
            "oss_multipart_upload_id", sa.String(length=512), nullable=False
        ),
        sa.Column("object_state", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "cleanup_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("cleanup_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cleanup_lease_until", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("cleanup_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "object_state IN ('multipart', 'finalizing', 'object')",
            name="ck_polarrag_upload_cleanups_object_state",
        ),
        sa.PrimaryKeyConstraint("upload_session_id"),
    )
    op.create_index(
        "ix_polarrag_upload_cleanups_due",
        "polarrag_upload_cleanups",
        ["expires_at", "cleanup_after", "cleanup_lease_until"],
    )
    op.create_index(
        "ix_polarrag_upload_cleanups_knowledge_space_id",
        "polarrag_upload_cleanups",
        ["knowledge_space_id"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO polarrag_upload_cleanups (
                upload_session_id, knowledge_space_id, oss_bucket,
                oss_endpoint, oss_object_key, oss_multipart_upload_id,
                object_state, expires_at, cleanup_attempts, created_at,
                updated_at
            )
            SELECT s.id, r.knowledge_space_id, s.oss_bucket,
                   s.oss_endpoint, s.oss_object_key,
                   s.oss_multipart_upload_id,
                   CASE WHEN s.status = 'prepared'
                        THEN 'multipart' ELSE 'object' END,
                   s.expires_at, 0, s.created_at, s.updated_at
              FROM polarrag_upload_sessions AS s
              JOIN knowledge_resources AS r
                ON r.id = s.knowledge_resource_id
             WHERE s.status IN ('prepared', 'uploaded')
            """
        )
    )
    op.drop_index(
        "ix_polarrag_upload_sessions_owner_status",
        table_name="polarrag_upload_sessions",
    )


def downgrade() -> None:
    op.create_index(
        "ix_polarrag_upload_sessions_owner_status",
        "polarrag_upload_sessions",
        ["pas_user_id", "agent_id", "status"],
    )
    op.drop_table("polarrag_upload_cleanups")
