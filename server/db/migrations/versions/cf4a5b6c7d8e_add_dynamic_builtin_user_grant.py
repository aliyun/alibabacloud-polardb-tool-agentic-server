"""Add dynamic Agent membership for built-in users.

Revision ID: cf4a5b6c7d8e
Revises: be3f4a5b6c7d
"""

import sqlalchemy as sa
from alembic import op

revision = "cf4a5b6c7d8e"
down_revision = "be3f4a5b6c7d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("all_builtin_users", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    raise RuntimeError("Restore the pre-upgrade metadata backup before removing dynamic built-in user grants")
