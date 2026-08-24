"""add identity-source all-user Agent access

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "f5a6b7c8d9e0"
down_revision: str | Sequence[str] | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _shape_constraint(include_all_users: bool) -> str:
    value = (
        "(group_kind = 'department' AND department_id IS NOT NULL "
        "AND identity_domain IS NULL AND provider IS NULL "
        "AND identity_source_id IS NULL AND principal_id IS NULL) OR "
        "(group_kind = 'enterprise' AND department_id IS NULL "
        "AND identity_domain IS NOT NULL AND provider IS NOT NULL "
        "AND identity_source_id IS NULL AND principal_id IS NOT NULL) OR "
        "(group_kind = 'identity_source' AND department_id IS NULL "
        "AND identity_domain IS NULL AND provider IS NULL "
        "AND identity_source_id IS NOT NULL AND principal_id IS NOT NULL)"
    )
    if include_all_users:
        value += (
            " OR (group_kind = 'identity_source_all' AND department_id IS NULL "
            "AND identity_domain IS NULL AND provider IS NULL "
            "AND identity_source_id IS NOT NULL AND principal_id IS NULL)"
        )
    return value


def upgrade() -> None:
    with op.batch_alter_table(
        "agent_group_assignments", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_agent_group_assignment_shape", type_="check"
        )
        batch_op.alter_column(
            "group_kind",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_agent_group_assignment_shape", _shape_constraint(True)
        )


def downgrade() -> None:
    with op.batch_alter_table(
        "agent_group_assignments", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_agent_group_assignment_shape", type_="check"
        )
        batch_op.alter_column(
            "group_kind",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_agent_group_assignment_shape", _shape_constraint(False)
        )
