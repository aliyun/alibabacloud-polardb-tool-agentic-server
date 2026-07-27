"""add instance usage

Revision ID: d4e5f6a7b8c9
Revises: c0f1a2b3c4d5
Create Date: 2026-07-26 23:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c0f1a2b3c4d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("usage", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instances", "usage")
