"""add PolarRAG Space OSS upload configuration

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-06
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "a3b4c5d6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns: tuple[sa.Column[Any], ...] = (
        sa.Column("oss_bucket", sa.String(length=255), nullable=True),
        sa.Column("oss_endpoint", sa.String(length=512), nullable=True),
        sa.Column("oss_access_key_id_ciphertext", sa.Text(), nullable=True),
        sa.Column("oss_access_key_secret_ciphertext", sa.Text(), nullable=True),
        sa.Column("oss_object_prefix", sa.String(length=512), nullable=True),
        sa.Column(
            "oss_config_validated",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("oss_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("oss_last_error_code", sa.String(length=64), nullable=True),
    )
    for column in columns:
        op.add_column("polarrag_spaces", column)


def downgrade() -> None:
    with op.batch_alter_table("polarrag_spaces") as batch_op:
        for name in (
            "oss_last_error_code",
            "oss_validated_at",
            "oss_config_validated",
            "oss_object_prefix",
            "oss_access_key_secret_ciphertext",
            "oss_access_key_id_ciphertext",
            "oss_endpoint",
            "oss_bucket",
        ):
            batch_op.drop_column(name)
