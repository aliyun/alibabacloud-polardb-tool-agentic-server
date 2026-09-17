"""add workspace and authentication tables

Revision ID: c3d4e5f6a7b8
Revises: f6a7b8c9d0e1
Create Date: 2026-09-04
"""

from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


INDEXED_COLUMNS = {
    "user_workspaces": ("default_agent_id", "user_id"),
    "oidc_login_states": (
        "initiator_user_id",
        "purpose",
        "state_hash",
        "status",
    ),
    "external_token_sessions": (
        "provider_fingerprint",
        "token_family",
        "user_id",
    ),
    "oauth_external_applications": (
        "created_by",
        "fixed_agent_id",
        "status",
    ),
}


def upgrade() -> None:
    op.create_table(
        "user_workspaces",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("default_agent_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "oidc_login_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column(
            "initiator_user_id", sa.String(length=36), nullable=True
        ),
        sa.Column("config_revision", sa.Integer(), nullable=True),
        sa.Column("config_digest", sa.String(length=64), nullable=True),
        sa.Column("nonce_ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "code_verifier_ciphertext", sa.Text(), nullable=True
        ),
        sa.Column(
            "identity_snapshot_ciphertext", sa.Text(), nullable=True
        ),
        sa.Column("redirect_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "consumed_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "external_token_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("token_family", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("provider_key", sa.String(length=255), nullable=False),
        sa.Column(
            "provider_fingerprint", sa.String(length=64), nullable=False
        ),
        sa.Column(
            "external_subject", sa.String(length=255), nullable=False
        ),
        sa.Column("subject_token_ciphertext", sa.Text(), nullable=False),
        sa.Column(
            "external_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_validated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "revoked_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "oauth_external_applications",
        sa.Column("client_id", sa.String(length=255), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("provider_type", sa.String(length=32), nullable=False),
        sa.Column("targets", sa.Text(), nullable=False),
        sa.Column("agent_policy", sa.String(length=32), nullable=False),
        sa.Column("fixed_agent_id", sa.String(length=36), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "secret_created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "disabled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("client_id"),
    )
    for table_name, columns in INDEXED_COLUMNS.items():
        for column in columns:
            op.create_index(
                op.f(f"ix_{table_name}_{column}"),
                table_name,
                [column],
                unique=False,
            )


def downgrade() -> None:
    for table_name, columns in reversed(INDEXED_COLUMNS.items()):
        for column in reversed(columns):
            op.drop_index(
                op.f(f"ix_{table_name}_{column}"),
                table_name=table_name,
            )
        op.drop_table(table_name)
