"""add shared secret reveal limits

Revision ID: a7c04b96d3e1
Revises: 9f1a2b3c4d5e
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7c04b96d3e1"
down_revision: str | Sequence[str] | None = "9f1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secret_reveal_limits",
        sa.Column("admin_id", sa.String(length=36), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column(
            "window_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "request_count > 0 AND request_count <= 5",
            name="ck_secret_reveal_limits_count",
        ),
        sa.ForeignKeyConstraint(
            ["admin_id"],
            ["users.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "admin_id",
            "target_kind",
            "target_id",
        ),
    )
    op.create_index(
        "ix_secret_reveal_limits_window_started_at",
        "secret_reveal_limits",
        ["window_started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_secret_reveal_limits_window_started_at",
        table_name="secret_reveal_limits",
    )
    op.drop_table("secret_reveal_limits")
