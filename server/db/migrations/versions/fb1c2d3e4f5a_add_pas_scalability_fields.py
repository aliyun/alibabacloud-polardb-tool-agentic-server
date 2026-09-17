"""add PAS scalability fields

Revision ID: fb1c2d3e4f5a
Revises: c3d4e5f6a7b8
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "fb1c2d3e4f5a"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_resources",
        sa.Column("catalog_sync_token", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_knowledge_resources_catalog_sync_token",
        "knowledge_resources",
        ["catalog_sync_token"],
        unique=False,
    )

    op.add_column(
        "agents",
        sa.Column("oauth_redirect_uri", sa.Text(), nullable=True),
    )

    op.add_column(
        "enterprise_identity_sources",
        sa.Column("sync_warning_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "enterprise_identity_sources",
        sa.Column("sync_worker_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "enterprise_identity_sources",
        sa.Column(
            "sync_lease_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "enterprise_identity_sources",
        sa.Column(
            "sync_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "enterprise_identity_sources",
        sa.Column(
            "sync_next_retry_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.add_column(
        "polarrag_spaces",
        sa.Column(
            "catalog_sync_status",
            sa.String(32),
            nullable=False,
            server_default="idle",
        ),
    )
    op.add_column(
        "polarrag_spaces",
        sa.Column("catalog_sync_result_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "polarrag_spaces",
        sa.Column("catalog_sync_error", sa.String(255), nullable=True),
    )
    op.add_column(
        "polarrag_spaces",
        sa.Column("catalog_sync_worker_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "polarrag_spaces",
        sa.Column(
            "catalog_sync_lease_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    op.add_column(
        "agents",
        sa.Column(
            "bulk_assignment_status",
            sa.String(32),
            nullable=False,
            server_default="completed",
        ),
    )
    op.add_column(
        "agents",
        sa.Column(
            "bulk_assignment_created_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "agents",
        sa.Column("bulk_assignment_error", sa.String(255), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column("bulk_assignment_worker_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "agents",
        sa.Column(
            "bulk_assignment_lease_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("agents") as batch_op:
        for column in (
            "bulk_assignment_lease_until",
            "bulk_assignment_worker_id",
            "bulk_assignment_error",
            "bulk_assignment_created_count",
            "bulk_assignment_status",
        ):
            batch_op.drop_column(column)

    with op.batch_alter_table("polarrag_spaces") as batch_op:
        for column in (
            "catalog_sync_lease_until",
            "catalog_sync_worker_id",
            "catalog_sync_error",
            "catalog_sync_result_json",
            "catalog_sync_status",
        ):
            batch_op.drop_column(column)

    with op.batch_alter_table("enterprise_identity_sources") as batch_op:
        for column in (
            "sync_next_retry_at",
            "sync_retry_count",
            "sync_lease_until",
            "sync_worker_id",
            "sync_warning_json",
        ):
            batch_op.drop_column(column)

    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("oauth_redirect_uri")

    with op.batch_alter_table("knowledge_resources") as batch_op:
        batch_op.drop_index("ix_knowledge_resources_catalog_sync_token")
        batch_op.drop_column("catalog_sync_token")
