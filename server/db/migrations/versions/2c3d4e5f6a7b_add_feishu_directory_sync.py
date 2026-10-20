"""Add Feishu directory event synchronization and reconciliation state.

Revision ID: 2c3d4e5f6a7b
Revises: cf4a5b6c7d8e
Create Date: 2026-09-21
"""

import sqlalchemy as sa
from alembic import op


revision = "2c3d4e5f6a7b"
down_revision = "cf4a5b6c7d8e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("event_listener_status", sa.String(32), nullable=True),
        sa.Column("event_listener_worker_id", sa.String(64), nullable=True),
        sa.Column("event_listener_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_unavailable_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_group_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_group_error", sa.String(128), nullable=True),
        sa.Column("group_next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sync_kind", sa.String(16), nullable=True),
        sa.Column("full_sync_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_listener_retry_count", sa.Integer(), nullable=False, server_default="0"),
    ):
        op.add_column("enterprise_identity_sources", column)

    op.create_table(
        "enterprise_identity_source_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("identity_source_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_enterprise_identity_source_events_identity_source_id",
        "enterprise_identity_source_events",
        ["identity_source_id"],
        unique=False,
    )
    op.create_index(
        "ix_identity_source_event_created_at",
        "enterprise_identity_source_events",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_identity_source_event_pending",
        "enterprise_identity_source_events",
        ["status", "next_retry_at"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError(
        "Restore the pre-upgrade metadata backup before removing Feishu directory synchronization"
    )
