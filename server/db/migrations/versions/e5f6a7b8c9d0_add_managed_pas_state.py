"""add managed PAS state

Revision ID: e5f6a7b8c9d0
Revises: a9b0c1d2e3f4
Create Date: 2026-08-03 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | Sequence[str] | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "config_operation_receipts",
        sa.Column("lease_owner", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "config_operation_receipts",
        sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.add_column(
        "config_operation_receipts",
        sa.Column("instance_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "config_operation_receipts",
        sa.Column("instance_generation", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "managed_instance_bindings",
        sa.Column("instance_id", sa.String(length=255), nullable=False),
        sa.Column("instance_generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "bound_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.PrimaryKeyConstraint("instance_id"),
    )


def downgrade() -> None:
    op.drop_table("managed_instance_bindings")
    op.drop_column("config_operation_receipts", "instance_generation")
    op.drop_column("config_operation_receipts", "instance_id")
    op.drop_column("config_operation_receipts", "lease_expires_at")
    op.drop_column("config_operation_receipts", "lease_owner")
    op.drop_column("users", "password_state")
