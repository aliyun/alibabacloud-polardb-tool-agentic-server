"""add Agent PolarRAG user tokens

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_polarrag_instance_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("polarrag_instance_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["polarrag_instance_id"],
            ["polarrag_instances.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id",
            "polarrag_instance_id",
            name="uq_agent_polarrag_instance_binding",
        ),
    )
    op.create_index(
        "ix_agent_polarrag_instance_bindings_agent_id",
        "agent_polarrag_instance_bindings",
        ["agent_id"],
    )
    op.create_index(
        "ix_agent_polarrag_instance_bindings_polarrag_instance_id",
        "agent_polarrag_instance_bindings",
        ["polarrag_instance_id"],
    )
    op.create_table(
        "agent_user_assignments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id", "user_id", name="uq_agent_user_assignment"
        ),
    )
    op.create_index(
        "ix_agent_user_assignments_agent_id",
        "agent_user_assignments",
        ["agent_id"],
    )
    op.create_index(
        "ix_agent_user_assignments_user_id",
        "agent_user_assignments",
        ["user_id"],
    )
    op.create_table(
        "agent_user_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("assignment_id", sa.String(length=36), nullable=False),
        sa.Column("token_prefix", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_ciphertext", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["assignment_id"],
            ["agent_user_assignments.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "assignment_id", name="uq_agent_user_tokens_assignment_id"
        ),
        sa.UniqueConstraint("token_hash", name="uq_agent_user_tokens_token_hash"),
    )
    op.create_index(
        "ix_agent_user_tokens_assignment_id",
        "agent_user_tokens",
        ["assignment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_user_tokens_assignment_id", table_name="agent_user_tokens"
    )
    op.drop_table("agent_user_tokens")
    op.drop_index(
        "ix_agent_user_assignments_user_id",
        table_name="agent_user_assignments",
    )
    op.drop_index(
        "ix_agent_user_assignments_agent_id",
        table_name="agent_user_assignments",
    )
    op.drop_table("agent_user_assignments")
    op.drop_index(
        "ix_agent_polarrag_instance_bindings_polarrag_instance_id",
        table_name="agent_polarrag_instance_bindings",
    )
    op.drop_index(
        "ix_agent_polarrag_instance_bindings_agent_id",
        table_name="agent_polarrag_instance_bindings",
    )
    op.drop_table("agent_polarrag_instance_bindings")
