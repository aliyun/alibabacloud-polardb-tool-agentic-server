"""Add per-KB policies and preserve dynamic visibility exclusions.

Revision ID: be3f4a5b6c7d
Revises: 9c1d2e3f4a5b
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "be3f4a5b6c7d"
down_revision = "9c1d2e3f4a5b"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_polarrag_instance_bindings",
        sa.Column("personal_knowledge_resource_ids_json", sa.Text(), nullable=True),
    )
    op.create_table(
        "agent_knowledge_policies",
        sa.Column("agent_id", sa.String(36), primary_key=True),
        sa.Column("knowledge_resource_id", sa.String(36), primary_key=True),
        sa.Column("select_all", sa.Boolean(), nullable=False),
        sa.Column("excluded_subjects_json", sa.Text().with_variant(mysql.LONGTEXT(), "mysql"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    for name in ("public_excluded_resource_ids_json", "personal_excluded_resource_ids_json"):
        op.add_column("agent_polarrag_instance_bindings", sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    raise RuntimeError("Restore the pre-upgrade metadata backup before running a version without KB exclusions")
