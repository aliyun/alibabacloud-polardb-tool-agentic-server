"""add external knowledge scope snapshot projection after Feishu directory sync

Revision ID: b3c4d5e6f7a8
Revises: 2c3d4e5f6a7b
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "b3c4d5e6f7a8"
down_revision: str | Sequence[str] | None = "2c3d4e5f6a7b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "external_knowledge_auto_bind_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_table(
        "external_knowledge_scope_snapshots",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("source_scope_type", sa.String(length=32), nullable=False),
        sa.Column("source_scope_id", sa.String(length=255), nullable=False),
        sa.Column("source_scope_name", sa.String(length=512), nullable=False),
        sa.Column("knowledge_resource_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_external_knowledge_scope_snapshots_identity_source_id", "external_knowledge_scope_snapshots", ["identity_source_id"])
    op.create_index("ix_external_knowledge_scope_snapshots_knowledge_resource_id", "external_knowledge_scope_snapshots", ["knowledge_resource_id"])
    op.create_index("ix_external_knowledge_scope_source", "external_knowledge_scope_snapshots", ["identity_source_id", "source_scope_id"])
    op.create_index("ix_external_knowledge_scope_resource", "external_knowledge_scope_snapshots", ["knowledge_resource_id", "status"])
    op.create_table(
        "external_knowledge_scope_grants",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("subject_type", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_external_knowledge_scope_grant_subject", "external_knowledge_scope_grants", ["identity_source_id", "subject_type", "subject_id"])
    op.create_index("ix_external_knowledge_scope_grant_snapshot", "external_knowledge_scope_grants", ["snapshot_id"])


def downgrade() -> None:
    raise RuntimeError(
        "Restore the pre-upgrade metadata backup before removing external knowledge scopes"
    )
