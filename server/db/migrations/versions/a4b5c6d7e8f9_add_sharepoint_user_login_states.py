"""add SharePoint user login state

Revision ID: a4b5c6d7e8f9
Revises: d5e6f7a8b9c0
Create Date: 2026-08-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "a4b5c6d7e8f9"
down_revision: str | Sequence[str] | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sharepoint_user_login_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("nonce_ciphertext", sa.Text(), nullable=False),
        sa.Column("code_verifier_ciphertext", sa.Text(), nullable=False),
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
        "ix_sharepoint_user_login_states_identity_source_id",
        "sharepoint_user_login_states",
        ["identity_source_id"],
        unique=False,
    )
    op.create_index(
        "ix_sharepoint_user_login_states_state_hash",
        "sharepoint_user_login_states",
        ["state_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sharepoint_user_login_states_state_hash",
        table_name="sharepoint_user_login_states",
    )
    op.drop_index(
        "ix_sharepoint_user_login_states_identity_source_id",
        table_name="sharepoint_user_login_states",
    )
    op.drop_table("sharepoint_user_login_states")
