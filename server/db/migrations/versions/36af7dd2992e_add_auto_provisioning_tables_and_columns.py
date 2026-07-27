"""add auto-provisioning tables and columns

Revision ID: 36af7dd2992e
Revises: 81ce053ed593
Create Date: 2026-06-11 21:53:23.745417

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

from server.models.base import generate_uuid


# revision identifiers, used by Alembic.
revision: str = '36af7dd2992e'
down_revision: Union[str, Sequence[str], None] = '81ce053ed593'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- New tables ---
    op.create_table('quota_counters',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('scope', sa.String(length=255), nullable=False),
    sa.Column('current_count', sa.Integer(), nullable=False),
    sa.Column('max_limit', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('quota_counters', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_quota_counters_scope'), ['scope'], unique=True)

    op.create_table('system_settings',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('key', sa.String(length=255), nullable=False),
    sa.Column('value', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('system_settings', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_system_settings_key'), ['key'], unique=True)

    # --- Column additions ---
    with op.batch_alter_table('departments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_instances', sa.Integer(), nullable=True))

    with op.batch_alter_table('instances', schema=None) as batch_op:
        batch_op.add_column(sa.Column('provisioning_step', sa.Enum('PENDING', 'CLUSTER_READY', 'PASSWORD_STORED', 'ACCOUNT_CREATED', 'DATABASE_CREATED', 'ENDPOINT_RESOLVED', 'BOUND', 'DONE', name='provisioningstep', native_enum=False), nullable=True))
        batch_op.add_column(sa.Column('quota_held', sa.Boolean(), server_default='0', nullable=False))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('provisioning_mode', sa.Enum('DEDICATED', 'MULTITENANT', name='provisioningmode', native_enum=False), nullable=True))

    # --- Partial unique index ---
    op.create_index(
        "uix_user_active_personal",
        "instances",
        ["owner_user_id"],
        unique=True,
        sqlite_where=text("type = 'PERSONAL' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"),
        postgresql_where=text("type = 'PERSONAL' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"),
    )

    now = datetime.now(timezone.utc)

    # --- Seed quota_counters ---
    counters_table = sa.table(
        "quota_counters",
        sa.column("id", sa.String),
        sa.column("scope", sa.String),
        sa.column("current_count", sa.Integer),
        sa.column("max_limit", sa.Integer),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(counters_table.insert().values(
        id=generate_uuid(), scope="global", current_count=0, max_limit=50,
        created_at=now, updated_at=now,
    ))


def downgrade() -> None:
    """Downgrade schema."""
    # --- Drop index first ---
    op.drop_index("uix_user_active_personal", table_name="instances")

    # --- Drop columns ---
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('provisioning_mode')

    with op.batch_alter_table('instances', schema=None) as batch_op:
        batch_op.drop_column('quota_held')
        batch_op.drop_column('provisioning_step')

    with op.batch_alter_table('departments', schema=None) as batch_op:
        batch_op.drop_column('max_instances')

    # --- Drop tables ---
    with op.batch_alter_table('system_settings', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_system_settings_key'))

    op.drop_table('system_settings')
    with op.batch_alter_table('quota_counters', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_quota_counters_scope'))

    op.drop_table('quota_counters')
