"""add native PolarRAG user principals

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b0c1d2e3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("enterprise_principal_assignments") as batch_op:
        batch_op.drop_constraint(
            "ck_enterprise_principal_provider",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_enterprise_principal_provider",
            "provider IN ('feishu', 'sharepoint', 'polarrag')",
        )
        batch_op.create_check_constraint(
            "ck_enterprise_principal_native_user",
            "provider != 'polarrag' OR principal_type = 'user'",
        )
        batch_op.create_check_constraint(
            "ck_enterprise_principal_native_admin",
            "provider != 'polarrag' OR source = 'admin_managed'",
        )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM enterprise_principal_assignments "
            "WHERE provider = 'polarrag'"
        )
    )
    with op.batch_alter_table("enterprise_principal_assignments") as batch_op:
        batch_op.drop_constraint(
            "ck_enterprise_principal_native_admin",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_enterprise_principal_native_user",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_enterprise_principal_provider",
            type_="check",
        )
        batch_op.create_check_constraint(
            "ck_enterprise_principal_provider",
            "provider IN ('feishu', 'sharepoint')",
        )
