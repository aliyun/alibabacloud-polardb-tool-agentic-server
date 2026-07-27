"""replace system settings with modular configuration

Revision ID: c0f1a2b3c4d5
Revises: b81e9c42f6a0
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "c0f1a2b3c4d5"
down_revision: str | Sequence[str] | None = "b81e9c42f6a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _large_text() -> sa.Text:
    return sa.Text().with_variant(mysql.LONGTEXT(), "mysql")


def upgrade() -> None:
    op.drop_table("system_settings")
    op.create_table(
        "system_config",
        sa.Column("config_key", sa.String(length=255), nullable=False),
        sa.Column("config_value", _large_text(), nullable=False),
        sa.Column("config_version", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("config_key"),
    )
    op.create_table(
        "config_bootstrap_claim",
        sa.Column("singleton_key", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "failed_attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "row_version", sa.Integer(), server_default="1", nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("singleton_key"),
    )
    op.create_table(
        "config_operation_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("actor_scope", sa.String(length=255), nullable=False),
        sa.Column(
            "idempotency_key_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("module", sa.String(length=100), nullable=True),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_json", _large_text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "actor_scope",
            "idempotency_key_hash",
            name="uq_config_operation_receipt_actor_key",
        ),
    )
    op.create_index(
        "ix_config_operation_receipts_expires_at",
        "config_operation_receipts",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_config_operation_receipts_expires_at",
        table_name="config_operation_receipts",
    )
    op.drop_table("config_operation_receipts")
    op.drop_table("config_bootstrap_claim")
    op.drop_table("system_config")
    op.create_table(
        "system_settings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_system_settings_key",
        "system_settings",
        ["key"],
        unique=True,
    )

