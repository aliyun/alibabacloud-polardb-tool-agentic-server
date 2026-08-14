"""add upload cleanup fencing

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8a9b0c1d2e3"
down_revision: str | Sequence[str] | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "polarrag_upload_cleanups",
        sa.Column("operation_kind", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "polarrag_upload_cleanups",
        sa.Column("operation_token", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "polarrag_upload_cleanups",
        sa.Column("polarrag_instance_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "polarrag_upload_cleanups",
        sa.Column("submission_payload_ciphertext", sa.Text(), nullable=True),
    )
    op.add_column(
        "polarrag_upload_cleanups",
        sa.Column(
            "reconcile_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("polarrag_upload_cleanups") as batch_op:
        batch_op.drop_column("reconcile_required")
        batch_op.drop_column("submission_payload_ciphertext")
        batch_op.drop_column("polarrag_instance_id")
        batch_op.drop_column("operation_token")
        batch_op.drop_column("operation_kind")
