"""add provisioning backend configuration revision

Revision ID: b81e9c42f6a0
Revises: a7c04b96d3e1
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b81e9c42f6a0"
down_revision: str | Sequence[str] | None = "a7c04b96d3e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("provisioning_backends") as batch_op:
        batch_op.add_column(
            sa.Column(
                "config_revision",
                sa.Integer(),
                server_default="1",
                nullable=False,
            )
        )
        batch_op.create_check_constraint(
            "ck_provisioning_backends_config_revision_positive",
            "config_revision > 0",
        )


def downgrade() -> None:
    with op.batch_alter_table("provisioning_backends") as batch_op:
        batch_op.drop_constraint(
            "ck_provisioning_backends_config_revision_positive",
            type_="check",
        )
        batch_op.drop_column("config_revision")
