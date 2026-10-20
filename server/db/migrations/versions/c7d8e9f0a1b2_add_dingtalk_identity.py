"""DingTalk identity source: maintenance-only CONTRACT (provider values).

All older readers/writers must be stopped before enabling DingTalk writes.
Rollback restores the paired pre-migration metadata backup and original image.
No new FKs, views or unique constraints are introduced.
"""
import sqlalchemy as sa
from alembic import op
revision = "c7d8e9f0a1b2"
down_revision = "b3c4d5e6f7a8"
branch_labels = depends_on = None


def upgrade():
    for table, name, expression in (
        ("enterprise_identity_sources", "ck_identity_source_provider", "provider IN ('feishu', 'sharepoint', 'dingtalk')"),
        ("enterprise_principal_assignments", "ck_enterprise_principal_provider", "provider IN ('feishu', 'sharepoint', 'dingtalk', 'polarrag')"),
    ):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(name, type_="check")
            batch.create_check_constraint(name, expression)
    op.add_column("enterprise_identity_sources", sa.Column("permission_observed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("enterprise_identity_sources", sa.Column("permission_error", sa.String(64), nullable=True))
    op.create_table("dingtalk_login_states",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("identity_source_id", sa.String(36), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=True),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    for column in ("identity_source_id", "state_hash"):
        op.create_index("ix_dingtalk_login_states_" + column, "dingtalk_login_states", [column])


def downgrade():
    raise RuntimeError("Restore the pre-maintenance metadata backup and source image together")
