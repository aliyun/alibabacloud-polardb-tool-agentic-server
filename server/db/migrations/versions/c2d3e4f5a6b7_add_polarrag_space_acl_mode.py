"""add PolarRAG Space ACL mode

Revision ID: c2d3e4f5a6b7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2d3e4f5a6b7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "polarrag_spaces",
        sa.Column(
            "acl_mode",
            sa.String(length=16),
            nullable=False,
            server_default="ENFORCED",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("polarrag_spaces") as batch_op:
        batch_op.drop_column("acl_mode")
