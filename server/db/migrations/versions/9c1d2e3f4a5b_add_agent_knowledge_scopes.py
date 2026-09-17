"""add Agent knowledge scopes and external principal memberships

Revision ID: 9c1d2e3f4a5b
Revises: fb1c2d3e4f5a
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "9c1d2e3f4a5b"
down_revision: str | Sequence[str] | None = "fb1c2d3e4f5a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, length: int) -> sa.Enum:
    return sa.Enum(*values, native_enum=False, length=length)


def upgrade() -> None:
    op.create_index(
        "ix_identity_source_group_memberships_prune",
        "enterprise_directory_memberships",
        ["identity_source_id", "updated_at"],
    )
    op.add_column(
        "agents",
        sa.Column(
            "knowledge_scope_mode",
            _enum("LEGACY_ALL", "SCOPED", length=16),
            nullable=False,
            server_default="LEGACY_ALL",
        ),
    )
    op.add_column(
        "knowledge_resources",
        sa.Column(
            "management_mode",
            _enum("NATIVE", "EXTERNAL_SYNC", length=32),
            nullable=False,
            server_default="NATIVE",
        ),
    )
    op.create_table(
        "agent_knowledge_scopes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "origin",
            _enum("MANUAL", "EXTERNAL_SYNC", length=32),
            nullable=False,
        ),
        sa.Column("identity_source_id", sa.String(length=36), nullable=True),
        sa.Column("external_scope_id", sa.String(length=255), nullable=True),
        sa.Column(
            "knowledge_resource_ids_json",
            sa.Text().with_variant(mysql.LONGTEXT(), "mysql"),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_knowledge_scopes_identity_source_id",
        "agent_knowledge_scopes",
        ["identity_source_id"],
    )
    op.create_index(
        "ix_agent_knowledge_scope_external",
        "agent_knowledge_scopes",
        ["agent_id", "identity_source_id", "external_scope_id"],
    )
    op.create_table(
        "agent_knowledge_scope_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("group_assignment_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_knowledge_scope_bindings_scope_id",
        "agent_knowledge_scope_bindings",
        ["scope_id"],
    )
    op.create_index(
        "ix_agent_knowledge_scope_binding_user",
        "agent_knowledge_scope_bindings",
        ["user_id", "scope_id"],
    )
    op.create_index(
        "ix_agent_knowledge_scope_binding_group",
        "agent_knowledge_scope_bindings",
        ["group_assignment_id", "scope_id"],
    )
    op.create_table(
        "identity_source_user_principal_snapshot_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column(
            "principal_type",
            _enum("group", "department", "acl_group", length=16),
            nullable=False,
        ),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_identity_source_user_principal_snapshot_key",
        "identity_source_user_principal_snapshot_entries",
        [
            "identity_source_id",
            "external_user_id",
            "principal_type",
            "principal_id",
        ],
    )
    op.create_index(
        "ix_identity_source_user_principal_snapshot_principal",
        "identity_source_user_principal_snapshot_entries",
        ["identity_source_id", "principal_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_identity_source_user_principal_snapshot_key",
        table_name="identity_source_user_principal_snapshot_entries",
    )
    op.drop_index(
        "ix_identity_source_user_principal_snapshot_principal",
        table_name="identity_source_user_principal_snapshot_entries",
    )
    op.drop_table("identity_source_user_principal_snapshot_entries")
    op.drop_index(
        "ix_agent_knowledge_scope_binding_group",
        table_name="agent_knowledge_scope_bindings",
    )
    op.drop_index(
        "ix_agent_knowledge_scope_binding_user",
        table_name="agent_knowledge_scope_bindings",
    )
    op.drop_index(
        "ix_agent_knowledge_scope_bindings_scope_id",
        table_name="agent_knowledge_scope_bindings",
    )
    op.drop_table("agent_knowledge_scope_bindings")
    op.drop_index(
        "ix_agent_knowledge_scope_external",
        table_name="agent_knowledge_scopes",
    )
    op.drop_index(
        "ix_agent_knowledge_scopes_identity_source_id",
        table_name="agent_knowledge_scopes",
    )
    op.drop_table("agent_knowledge_scopes")
    with op.batch_alter_table("knowledge_resources") as batch_op:
        batch_op.drop_column("management_mode")
    with op.batch_alter_table("agents") as batch_op:
        batch_op.drop_column("knowledge_scope_mode")
    op.drop_index(
        "ix_identity_source_group_memberships_prune",
        table_name="enterprise_directory_memberships",
    )
