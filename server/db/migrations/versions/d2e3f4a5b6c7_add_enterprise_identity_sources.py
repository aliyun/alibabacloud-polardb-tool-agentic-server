"""add enterprise identity sources

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(*values: str, length: int) -> sa.Enum:
    return sa.Enum(*values, native_enum=False, length=length)


def upgrade() -> None:
    op.create_table(
        "enterprise_identity_sources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("provider", _enum("feishu", "sharepoint", length=32), nullable=False),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            _enum("pending_binding", "active", "stale", "disabled", length=32),
            nullable=False,
            server_default="pending_binding",
        ),
        sa.Column("config_ciphertext", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale_after_seconds", sa.Integer(), nullable=False, server_default="1800"),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("provider IN ('feishu', 'sharepoint')", name="ck_identity_source_provider"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
        sa.UniqueConstraint("provider", "tenant_id", name="uq_identity_source_provider_tenant"),
    )
    op.create_table(
        "enterprise_identity_source_space_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_space_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["identity_source_id"], ["enterprise_identity_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["knowledge_space_id"], ["polarrag_spaces.knowledge_space_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_source_id", "knowledge_space_id", name="uq_identity_source_space_binding"),
    )
    op.create_index(
        "ix_identity_source_space_binding_space",
        "enterprise_identity_source_space_bindings",
        ["knowledge_space_id"],
        unique=False,
    )
    op.create_index(
        "ix_enterprise_identity_source_space_bindings_identity_source_id",
        "enterprise_identity_source_space_bindings",
        ["identity_source_id"],
        unique=False,
    )
    with op.batch_alter_table(
        "agent_group_assignments", recreate="always"
    ) as batch_op:
        batch_op.add_column(
            sa.Column("identity_source_id", sa.String(length=36), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_agent_group_assignment_identity_source",
            "enterprise_identity_sources",
            ["identity_source_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_constraint(
            "ck_agent_group_assignment_shape", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_agent_group_assignment_shape",
            "(group_kind = 'department' AND department_id IS NOT NULL "
            "AND identity_domain IS NULL AND provider IS NULL "
            "AND identity_source_id IS NULL AND principal_id IS NULL) OR "
            "(group_kind = 'enterprise' AND department_id IS NULL "
            "AND identity_domain IS NOT NULL AND provider IS NOT NULL "
            "AND identity_source_id IS NULL AND principal_id IS NOT NULL) OR "
            "(group_kind = 'identity_source' AND department_id IS NULL "
            "AND identity_domain IS NULL AND provider IS NULL "
            "AND identity_source_id IS NOT NULL AND principal_id IS NOT NULL)",
        )
    op.create_index(
        "ix_agent_group_assignments_identity_source_id",
        "agent_group_assignments",
        ["identity_source_id"],
        unique=False,
    )
    op.create_table(
        "enterprise_directory_users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("status", _enum("active", "disabled", length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["identity_source_id"], ["enterprise_identity_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_source_id", "external_user_id", name="uq_directory_user_source_external"),
    )
    op.create_index("ix_directory_user_source_status", "enterprise_directory_users", ["identity_source_id", "status"], unique=False)
    op.create_index(
        "ix_enterprise_directory_users_identity_source_id",
        "enterprise_directory_users",
        ["identity_source_id"],
        unique=False,
    )
    op.create_table(
        "enterprise_directory_groups",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("external_group_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column(
            "principal_type",
            _enum("group", "department", "acl_group", length=16),
            nullable=False,
            server_default="group",
        ),
        sa.Column("status", _enum("active", "disabled", length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["identity_source_id"], ["enterprise_identity_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("identity_source_id", "external_group_id", name="uq_directory_group_source_external"),
    )
    op.create_index("ix_directory_group_source_status", "enterprise_directory_groups", ["identity_source_id", "status"], unique=False)
    op.create_index(
        "ix_enterprise_directory_groups_identity_source_id",
        "enterprise_directory_groups",
        ["identity_source_id"],
        unique=False,
    )
    op.create_table(
        "enterprise_directory_memberships",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("identity_source_id", sa.String(length=36), nullable=False),
        sa.Column("external_group_id", sa.String(length=255), nullable=False),
        sa.Column("member_type", _enum("user", "group", length=16), nullable=False),
        sa.Column("external_member_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["identity_source_id"], ["enterprise_identity_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "identity_source_id", "external_group_id", "member_type", "external_member_id",
            name="uq_directory_membership",
        ),
    )
    op.create_index(
        "ix_directory_membership_member",
        "enterprise_directory_memberships",
        ["identity_source_id", "member_type", "external_member_id"],
        unique=False,
    )
    op.create_index(
        "ix_enterprise_directory_memberships_identity_source_id",
        "enterprise_directory_memberships",
        ["identity_source_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_enterprise_directory_memberships_identity_source_id",
        table_name="enterprise_directory_memberships",
    )
    op.drop_index("ix_directory_membership_member", table_name="enterprise_directory_memberships")
    op.drop_table("enterprise_directory_memberships")
    op.drop_index(
        "ix_enterprise_directory_groups_identity_source_id",
        table_name="enterprise_directory_groups",
    )
    op.drop_index("ix_directory_group_source_status", table_name="enterprise_directory_groups")
    op.drop_table("enterprise_directory_groups")
    op.drop_index(
        "ix_enterprise_directory_users_identity_source_id",
        table_name="enterprise_directory_users",
    )
    op.drop_index("ix_directory_user_source_status", table_name="enterprise_directory_users")
    op.drop_table("enterprise_directory_users")
    op.drop_index(
        "ix_agent_group_assignments_identity_source_id",
        table_name="agent_group_assignments",
    )
    with op.batch_alter_table(
        "agent_group_assignments", recreate="always"
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_agent_group_assignment_shape", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_agent_group_assignment_shape",
            "(group_kind = 'department' AND department_id IS NOT NULL "
            "AND identity_domain IS NULL AND provider IS NULL "
            "AND principal_id IS NULL) OR "
            "(group_kind = 'enterprise' AND department_id IS NULL "
            "AND identity_domain IS NOT NULL AND provider IS NOT NULL "
            "AND principal_id IS NOT NULL)",
        )
        batch_op.drop_constraint(
            "fk_agent_group_assignment_identity_source", type_="foreignkey"
        )
        batch_op.drop_column("identity_source_id")
    op.drop_index(
        "ix_enterprise_identity_source_space_bindings_identity_source_id",
        table_name="enterprise_identity_source_space_bindings",
    )
    op.drop_index("ix_identity_source_space_binding_space", table_name="enterprise_identity_source_space_bindings")
    op.drop_table("enterprise_identity_source_space_bindings")
    op.drop_table("enterprise_identity_sources")
