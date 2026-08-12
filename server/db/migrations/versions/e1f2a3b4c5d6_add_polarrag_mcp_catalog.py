"""add PolarRAG MCP catalog

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "polarrag_instances",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("scheme", sa.String(length=8), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username_ciphertext", sa.Text(), nullable=False),
        sa.Column("password_ciphertext", sa.Text(), nullable=False),
        sa.Column("tls_verify", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("ca_bundle_ciphertext", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("plugin_version", sa.String(length=128), nullable=True),
        sa.Column("capabilities_json", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("scheme IN ('http', 'https')", name="ck_polarrag_instances_scheme"),
        sa.CheckConstraint("port >= 1 AND port <= 65535", name="ck_polarrag_instances_port"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "polarrag_spaces",
        sa.Column("knowledge_space_id", sa.String(length=36), nullable=False),
        sa.Column("polarrag_instance_id", sa.String(length=36), nullable=False),
        sa.Column("space_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("identity_domain", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["polarrag_instance_id"],
            ["polarrag_instances.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("knowledge_space_id"),
        sa.UniqueConstraint(
            "polarrag_instance_id",
            "space_id",
            name="uq_polarrag_spaces_instance_space",
        ),
    )
    op.create_index(
        "ix_polarrag_spaces_polarrag_instance_id",
        "polarrag_spaces",
        ["polarrag_instance_id"],
        unique=False,
    )
    op.create_table(
        "enterprise_principal_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("pas_user_id", sa.String(length=36), nullable=False),
        sa.Column("identity_domain", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("principal_type", sa.String(length=16), nullable=False),
        sa.Column("principal_id", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("user_principal_key", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "provider IN ('feishu', 'sharepoint')",
            name="ck_enterprise_principal_provider",
        ),
        sa.ForeignKeyConstraint(["pas_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "pas_user_id",
            "identity_domain",
            "provider",
            "principal_type",
            "principal_id",
            name="uq_enterprise_principal_assignment",
        ),
        sa.UniqueConstraint(
            "user_principal_key",
            name="uq_enterprise_user_principal_key",
        ),
    )
    op.create_index(
        "ix_enterprise_principal_assignments_pas_user_id",
        "enterprise_principal_assignments",
        ["pas_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_enterprise_principal_resolution",
        "enterprise_principal_assignments",
        ["pas_user_id", "identity_domain", "status"],
        unique=False,
    )
    op.create_table(
        "knowledge_resources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("knowledge_space_id", sa.String(length=36), nullable=False),
        sa.Column("polarrag_instance_id", sa.String(length=36), nullable=False),
        sa.Column("space_id", sa.String(length=255), nullable=False),
        sa.Column("kb_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("usage", sa.String(length=1024), nullable=True),
        sa.Column("kb_type", sa.String(length=32), nullable=False),
        sa.Column("identity_domain", sa.String(length=255), nullable=False),
        sa.Column("binding_mode", sa.String(length=16), nullable=True),
        sa.Column("owner_pas_user_id", sa.String(length=36), nullable=True),
        sa.Column("sync_status", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("upstream_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["knowledge_space_id"],
            ["polarrag_spaces.knowledge_space_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_pas_user_id"],
            ["users.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["polarrag_instance_id"],
            ["polarrag_instances.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "polarrag_instance_id",
            "space_id",
            "kb_id",
            name="uq_knowledge_resources_upstream",
        ),
    )
    op.create_index(
        "ix_knowledge_resources_knowledge_space_id",
        "knowledge_resources",
        ["knowledge_space_id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_resources_polarrag_instance_id",
        "knowledge_resources",
        ["polarrag_instance_id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_resources_identity_domain",
        "knowledge_resources",
        ["identity_domain"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_resources_owner_pas_user_id",
        "knowledge_resources",
        ["owner_pas_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_resources_discovery",
        "knowledge_resources",
        ["enabled", "sync_status", "identity_domain"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_resources_discovery", table_name="knowledge_resources")
    op.drop_index("ix_knowledge_resources_owner_pas_user_id", table_name="knowledge_resources")
    op.drop_index("ix_knowledge_resources_identity_domain", table_name="knowledge_resources")
    op.drop_index("ix_knowledge_resources_polarrag_instance_id", table_name="knowledge_resources")
    op.drop_index("ix_knowledge_resources_knowledge_space_id", table_name="knowledge_resources")
    op.drop_table("knowledge_resources")
    op.drop_index(
        "ix_enterprise_principal_resolution",
        table_name="enterprise_principal_assignments",
    )
    op.drop_index(
        "ix_enterprise_principal_assignments_pas_user_id",
        table_name="enterprise_principal_assignments",
    )
    op.drop_table("enterprise_principal_assignments")
    op.drop_index("ix_polarrag_spaces_polarrag_instance_id", table_name="polarrag_spaces")
    op.drop_table("polarrag_spaces")
    op.drop_table("polarrag_instances")
