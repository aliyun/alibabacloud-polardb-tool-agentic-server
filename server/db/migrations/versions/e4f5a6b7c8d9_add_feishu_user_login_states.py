"""add Feishu user login state

Revision ID: e4f5a6b7c8d9
Revises: e3f4a5b6c7d8
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "e4f5a6b7c8d9"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "feishu_user_login_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["identity_source_id"],
            ["enterprise_identity_sources.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_feishu_user_login_states_identity_source_id",
        "feishu_user_login_states",
        ["identity_source_id"],
        unique=False,
    )
    op.create_index(
        "ix_feishu_user_login_states_state_hash",
        "feishu_user_login_states",
        ["state_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_feishu_user_login_states_state_hash",
        table_name="feishu_user_login_states",
    )
    op.drop_index(
        "ix_feishu_user_login_states_identity_source_id",
        table_name="feishu_user_login_states",
    )
    op.drop_table("feishu_user_login_states")
