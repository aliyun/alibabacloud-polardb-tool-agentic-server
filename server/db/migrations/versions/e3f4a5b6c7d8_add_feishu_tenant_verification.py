"""add Feishu tenant verification state

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("enterprise_identity_sources") as batch_op:
        batch_op.alter_column(
            "tenant_id",
            existing_type=sa.String(length=255),
            nullable=True,
        )
    op.create_table(
        "feishu_tenant_verification_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["identity_source_id"],
            ["enterprise_identity_sources.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_feishu_tenant_verification_states_identity_source_id",
        "feishu_tenant_verification_states",
        ["identity_source_id"],
        unique=False,
    )
    op.create_index(
        "ix_feishu_tenant_verification_states_created_by_user_id",
        "feishu_tenant_verification_states",
        ["created_by_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_feishu_tenant_verification_states_state_hash",
        "feishu_tenant_verification_states",
        ["state_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_feishu_tenant_verification_states_state_hash",
        table_name="feishu_tenant_verification_states",
    )
    op.drop_index(
        "ix_feishu_tenant_verification_states_created_by_user_id",
        table_name="feishu_tenant_verification_states",
    )
    op.drop_index(
        "ix_feishu_tenant_verification_states_identity_source_id",
        table_name="feishu_tenant_verification_states",
    )
    op.drop_table("feishu_tenant_verification_states")
    with op.batch_alter_table("enterprise_identity_sources") as batch_op:
        batch_op.alter_column(
            "tenant_id",
            existing_type=sa.String(length=255),
            nullable=False,
        )
