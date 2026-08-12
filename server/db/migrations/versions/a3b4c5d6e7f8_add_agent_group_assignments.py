"""add Agent group assignments

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3b4c5d6e7f8"
down_revision: str | Sequence[str] | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_user_assignments",
        sa.Column(
            "is_direct",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.create_table(
        "agent_group_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("group_kind", sa.String(length=16), nullable=False),
        sa.Column("group_key", sa.String(length=64), nullable=False),
        sa.Column("department_id", sa.String(length=36), nullable=True),
        sa.Column("identity_domain", sa.String(length=255), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("principal_id", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(group_kind = 'department' AND department_id IS NOT NULL "
            "AND identity_domain IS NULL AND provider IS NULL "
            "AND principal_id IS NULL) OR "
            "(group_kind = 'enterprise' AND department_id IS NULL "
            "AND identity_domain IS NOT NULL AND provider IS NOT NULL "
            "AND principal_id IS NOT NULL)",
            name="ck_agent_group_assignment_shape",
        ),
        sa.CheckConstraint(
            "provider IS NULL OR provider IN ('feishu', 'sharepoint')",
            name="ck_agent_group_assignment_provider",
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["department_id"],
            ["departments.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id",
            "group_key",
            name="uq_agent_group_assignment",
        ),
    )
    op.create_index(
        "ix_agent_group_assignments_agent_id",
        "agent_group_assignments",
        ["agent_id"],
    )
    op.create_index(
        "ix_agent_group_assignments_department_id",
        "agent_group_assignments",
        ["department_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_group_assignments_department_id",
        table_name="agent_group_assignments",
    )
    op.drop_index(
        "ix_agent_group_assignments_agent_id",
        table_name="agent_group_assignments",
    )
    op.drop_table("agent_group_assignments")
    with op.batch_alter_table("agent_user_assignments") as batch_op:
        batch_op.drop_column("is_direct")
