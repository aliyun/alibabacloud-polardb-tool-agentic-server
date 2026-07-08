"""add department agentic db cluster fields

Revision ID: ad71f04a14b5
Revises: 535a213beae6
Create Date: 2026-06-27 16:47:27.323663

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ad71f04a14b5'
down_revision: Union[str, Sequence[str], None] = '535a213beae6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('departments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('agentic_db_cluster_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('agentic_db_cluster_description', sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('departments', schema=None) as batch_op:
        batch_op.drop_column('agentic_db_cluster_description')
        batch_op.drop_column('agentic_db_cluster_id')
