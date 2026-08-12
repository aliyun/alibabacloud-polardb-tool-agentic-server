"""add consolidated Agent Dedicated hot-pool schema

Revision ID: f3a4b5c6d7e8
Revises: d4e5f6a7b8c9
Create Date: 2026-08-11 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_PERMISSION_SNAPSHOT = (
    '{"grant_option":true,"legacy":true,'
    '"privileges":["ALL PRIVILEGES"],"scope":"tenant"}'
)
_DEFAULT_TEMPLATE_ID = "builtin-mysql-default"
_DEFAULT_REVISION_ID = "builtin-mysql-default-v1"
_DEFAULT_PRIVILEGES_JSON = (
    '["CREATE","DROP","ALTER","INDEX","REFERENCES","CREATE VIEW",'
    '"SHOW VIEW","CREATE ROUTINE","ALTER ROUTINE","EXECUTE","SELECT",'
    '"INSERT","UPDATE","DELETE","CREATE TEMPORARY TABLES","LOCK TABLES"]'
)


def _enum(name: str, *values: str, length: int = 32) -> sa.Enum:
    return sa.Enum(
        *values,
        name=name,
        native_enum=False,
        length=length,
    )


def _seed_default_template() -> None:
    op.execute(
        sa.text(
            "INSERT INTO permission_templates("
            "id, name, description, created_at, updated_at"
            ") VALUES ("
            ":id, :name, :description, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
            ")"
        ).bindparams(
            id=_DEFAULT_TEMPLATE_ID,
            name="Agent MySQL default permissions",
            description=(
                "Built-in safe default without CREATE USER or GRANT OPTION"
            ),
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO permission_template_revisions("
            "id, template_id, revision, privileges_json, grant_option, "
            "created_by_user_id, created_at, updated_at"
            ") VALUES ("
            ":id, :template_id, 1, :privileges_json, :grant_option, "
            "NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
            ")"
        ).bindparams(
            id=_DEFAULT_REVISION_ID,
            template_id=_DEFAULT_TEMPLATE_ID,
            privileges_json=_DEFAULT_PRIVILEGES_JSON,
            grant_option=False,
        )
    )


def upgrade() -> None:
    op.create_table(
        "permission_templates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "permission_template_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("template_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("privileges_json", sa.Text(), nullable=False),
        sa.Column(
            "grant_option",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_permission_template_revisions_revision_positive",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["permission_templates.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "template_id",
            "revision",
            name="uq_permission_template_revisions_template_revision",
        ),
    )
    op.create_index(
        op.f("ix_permission_template_revisions_template_id"),
        "permission_template_revisions",
        ["template_id"],
        unique=False,
    )
    _seed_default_template()
    op.create_table(
        "permission_sync_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("template_revision_id", sa.String(length=36), nullable=False),
        sa.Column("target_scope", sa.String(length=32), nullable=False),
        sa.Column("target_id", sa.String(length=36), nullable=False),
        sa.Column(
            "mode",
            _enum("permissionsyncmode", "DRY_RUN", "APPLY"),
            nullable=False,
        ),
        sa.Column(
            "status",
            _enum(
                "permissionsyncstatus",
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
            ),
            nullable=False,
        ),
        sa.Column("confirmed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("total_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "completed_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("failed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_reason", sa.String(length=2048), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("worker_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "total_count >= 0 AND completed_count >= 0 "
            "AND failed_count >= 0 "
            "AND completed_count + failed_count <= total_count",
            name="ck_permission_sync_jobs_progress",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by_user_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["template_revision_id"],
            ["permission_template_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_permission_sync_jobs_target_id"),
        "permission_sync_jobs",
        ["target_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_permission_sync_jobs_template_revision_id"),
        "permission_sync_jobs",
        ["template_revision_id"],
        unique=False,
    )
    op.create_index(
        "ix_permission_sync_jobs_worker_scan",
        "permission_sync_jobs",
        ["status", "next_retry_at"],
        unique=False,
    )

    op.create_table(
        "dedicated_pools",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            _enum(
                "dedicatedpoolstatus", "ACTIVE", "DRAINING", "DISABLED"
            ),
            nullable=False,
        ),
        sa.Column("target_size", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_total_members", sa.Integer(), nullable=False),
        sa.Column("max_member_purchases_per_hour", sa.Integer(), nullable=False),
        sa.Column(
            "max_create_requests_per_agent_per_hour",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "max_delete_requests_per_agent_per_hour",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column("purchase_config_json", sa.Text(), nullable=False),
        sa.Column("purchase_profile_id", sa.String(length=64), nullable=True),
        sa.Column("purchase_profile_revision", sa.Integer(), nullable=True),
        sa.Column("storage_type", sa.String(length=64), nullable=True),
        sa.Column("region_id", sa.String(length=64), nullable=False),
        sa.Column("vpc_id", sa.String(length=64), nullable=False),
        sa.Column("vswitch_id", sa.String(length=64), nullable=False),
        sa.Column("zone_id", sa.String(length=64), nullable=True),
        sa.Column("security_ip_list", sa.String(length=2048), nullable=True),
        sa.Column(
            "endpoint_net_type",
            sa.String(length=32),
            server_default="Private",
            nullable=False,
        ),
        sa.Column(
            "reclaim_policy",
            _enum("reclaimpolicy", "DESTROY", "SANITIZE_AND_REUSE"),
            server_default="DESTROY",
            nullable=False,
        ),
        sa.Column(
            "lifecycle_admin_policy",
            _enum(
                "lifecycleadministratorpolicy",
                "PAS_MANAGED",
                "ADMIN_PROVIDED",
            ),
            server_default="PAS_MANAGED",
            nullable=False,
        ),
        sa.Column(
            "permission_template_revision_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column(
            "account_name_template",
            sa.String(length=64),
            server_default="agentic",
            nullable=False,
        ),
        sa.Column(
            "database_name_template",
            sa.String(length=64),
            server_default="agentic",
            nullable=False,
        ),
        sa.Column("delete_cooldown_duration_hours", sa.Integer(), nullable=True),
        sa.Column(
            "available_health_check_interval_seconds",
            sa.Integer(),
            server_default="300",
            nullable=False,
        ),
        sa.Column(
            "available_health_stale_after_seconds",
            sa.Integer(),
            server_default="600",
            nullable=False,
        ),
        sa.Column("config_revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "target_size >= 0 AND max_total_members > 0 "
            "AND target_size <= max_total_members",
            name="ck_dedicated_pools_size_range",
        ),
        sa.CheckConstraint(
            "max_member_purchases_per_hour > 0",
            name="ck_dedicated_pools_purchase_budget_positive",
        ),
        sa.CheckConstraint(
            "max_create_requests_per_agent_per_hour > 0",
            name="ck_dedicated_pools_create_budget_positive",
        ),
        sa.CheckConstraint(
            "max_delete_requests_per_agent_per_hour > 0",
            name="ck_dedicated_pools_delete_budget_positive",
        ),
        sa.CheckConstraint(
            "delete_cooldown_duration_hours IS NULL "
            "OR delete_cooldown_duration_hours >= 1",
            name="ck_dedicated_pools_cooldown_positive",
        ),
        sa.CheckConstraint(
            "available_health_check_interval_seconds > 0 "
            "AND available_health_stale_after_seconds > 0",
            name="ck_dedicated_pools_health_intervals_positive",
        ),
        sa.CheckConstraint(
            "config_revision > 0",
            name="ck_dedicated_pools_config_revision_positive",
        ),
        sa.ForeignKeyConstraint(
            ["permission_template_revision_id"],
            ["permission_template_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index(
        op.f("ix_dedicated_pools_permission_template_revision_id"),
        "dedicated_pools",
        ["permission_template_revision_id"],
        unique=False,
    )

    with op.batch_alter_table("provisioning_backends") as batch_op:
        batch_op.add_column(
            sa.Column(
                "backend_type",
                _enum(
                    "provisioningbackendtype",
                    "MULTITENANT",
                    "DEDICATED_POOL",
                ),
                server_default="MULTITENANT",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("dedicated_pool_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "delete_cooldown_duration_hours",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "permission_template_revision_id",
                sa.String(length=36),
                nullable=True,
            )
        )
        batch_op.alter_column(
            "instance_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch_op.alter_column(
            "admin_credential_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
        batch_op.create_foreign_key(
            "fk_provisioning_backends_dedicated_pool_id",
            "dedicated_pools",
            ["dedicated_pool_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_provisioning_backends_target_matches_type",
            "(backend_type = 'MULTITENANT' "
            "AND instance_id IS NOT NULL "
            "AND dedicated_pool_id IS NULL "
            "AND admin_credential_id IS NOT NULL) "
            "OR (backend_type = 'DEDICATED_POOL' "
            "AND instance_id IS NULL "
            "AND dedicated_pool_id IS NOT NULL "
                "AND admin_credential_id IS NULL)",
        )
        batch_op.create_check_constraint(
            "ck_provisioning_backends_cooldown_positive",
            "delete_cooldown_duration_hours IS NULL "
            "OR delete_cooldown_duration_hours >= 1",
        )
        batch_op.create_foreign_key(
            "fk_provisioning_backends_permission_template_revision_id",
            "permission_template_revisions",
            ["permission_template_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        op.f("ix_provisioning_backends_dedicated_pool_id"),
        "provisioning_backends",
        ["dedicated_pool_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_provisioning_backends_permission_template_revision_id"),
        "provisioning_backends",
        ["permission_template_revision_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            "UPDATE provisioning_backends "
            "SET permission_template_revision_id = :revision_id "
            "WHERE backend_type = 'MULTITENANT' "
            "AND permission_template_revision_id IS NULL"
        ).bindparams(revision_id=_DEFAULT_REVISION_ID)
    )

    with op.batch_alter_table("db_instance_resources") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=_enum(
                "dbinstancestatus",
                "CREATING",
                "READY",
                "FAILED",
                "DELETING",
                "DELETED",
                "DELETE_FAILED",
            ),
            type_=_enum(
                "dbinstancestatus",
                "CREATING",
                "READY",
                "FAILED",
                "DELETING",
                "COOLING_DOWN",
                "RESTORING",
                "DELETED",
                "DELETE_FAILED",
            ),
            existing_nullable=False,
            postgresql_using="status::text",
        )
        batch_op.add_column(
            sa.Column(
                "provisioning_mode",
                _enum("provisioningmode", "DEDICATED", "MULTITENANT"),
                server_default="MULTITENANT",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("allocated_instance_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column("permission_template_id", sa.String(length=36), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "permission_template_revision_id",
                sa.String(length=36),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("permission_snapshot_json", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("delete_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "effective_delete_cooldown_duration_hours",
                sa.Integer(),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "reclaim_policy",
                _enum("reclaimpolicy", "DESTROY", "SANITIZE_AND_REUSE"),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "delete_step",
                _enum(
                    "deletelifecyclestep",
                    "PENDING",
                    "ACCOUNT_LOCKED",
                    "SESSIONS_TERMINATED",
                    "DISCONNECTED",
                    "COOLING_DOWN",
                    "LOGICAL_CLEANUP",
                    "PHYSICAL_DESTROY",
                    "SANITIZE_DISPATCH",
                    "COMPLETE",
                    length=64,
                ),
                server_default="PENDING",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "restore_source_status",
                _enum(
                    "dbinstancestatus",
                    "CREATING",
                    "READY",
                    "FAILED",
                    "DELETING",
                    "COOLING_DOWN",
                    "RESTORING",
                    "DELETED",
                    "DELETE_FAILED",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column("restore_failure_reason", sa.String(length=2048), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_db_instance_resources_allocated_instance_id",
            "instances",
            ["allocated_instance_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_db_instance_resources_permission_template_id",
            "permission_templates",
            ["permission_template_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_db_instance_resources_permission_template_revision_id",
            "permission_template_revisions",
            ["permission_template_revision_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        op.f("ix_db_instance_resources_allocated_instance_id"),
        "db_instance_resources",
        ["allocated_instance_id"],
        unique=False,
    )
    op.execute(
        sa.text(
            "UPDATE db_instance_resources "
            "SET permission_snapshot_json = :snapshot "
            "WHERE permission_snapshot_json IS NULL"
        ).bindparams(snapshot=_LEGACY_PERMISSION_SNAPSHOT)
    )

    op.create_table(
        "dedicated_pool_members",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("pool_id", sa.String(length=36), nullable=False),
        sa.Column("instance_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            _enum(
                "dedicatedmemberstatus",
                "REPLENISHING",
                "AVAILABLE",
                "ALLOCATED_PREPARING",
                "ALLOCATED",
                "COOLING_DOWN",
                "SANITIZING",
                "QUARANTINED",
                "DELETING",
                "DELETED",
            ),
            nullable=False,
        ),
        sa.Column(
            "readiness_status",
            _enum("readinessstatus", "FRESH", "STALE", "CHECKING"),
            nullable=False,
        ),
        sa.Column(
            "preparation_step",
            _enum(
                "dedicatedpreparationstep",
                "PENDING",
                "PURCHASE_INTENT_STORED",
                "PURCHASE_REQUESTED",
                "CLUSTER_READY",
                "ENDPOINT_RESOLVED",
                "LIFECYCLE_ACCOUNT_STORED",
                "LIFECYCLE_ACCOUNT_CREATED",
                "SANDBOX_ACCOUNT_STORED",
                "SANDBOX_ACCOUNT_CREATED",
                "DATABASE_CREATED",
                "OPENAPI_READY",
                "PRIVILEGES_GRANTED",
                "VERIFIED",
                length=64,
            ),
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("purchase_token", sa.String(length=128), nullable=True),
        sa.Column("cloud_request_id", sa.String(length=128), nullable=True),
        sa.Column("agentic_db_cluster_id", sa.String(length=255), nullable=True),
        sa.Column("last_ready_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifecycle_username_ciphertext", sa.String(length=1024), nullable=True),
        sa.Column("lifecycle_password_ciphertext", sa.String(length=2048), nullable=True),
        sa.Column("sandbox_username_ciphertext", sa.String(length=1024), nullable=True),
        sa.Column("sandbox_password_ciphertext", sa.String(length=2048), nullable=True),
        sa.Column("host", sa.String(length=255), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("database_name", sa.String(length=255), nullable=True),
        sa.Column("permission_template_revision_id", sa.String(length=36), nullable=True),
        sa.Column("permission_snapshot_json", sa.Text(), nullable=True),
        sa.Column("allocated_resource_id", sa.String(length=36), nullable=True),
        sa.Column("delete_cooldown_duration_hours", sa.Integer(), nullable=True),
        sa.Column("failure_reason", sa.String(length=2048), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("worker_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "delete_cooldown_duration_hours IS NULL "
            "OR delete_cooldown_duration_hours >= 1",
            name="ck_dedicated_pool_members_cooldown_positive",
        ),
        sa.CheckConstraint(
            "retry_count >= 0",
            name="ck_dedicated_pool_members_retry_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["allocated_resource_id"],
            ["db_instance_resources.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["instance_id"], ["instances.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["permission_template_revision_id"],
            ["permission_template_revisions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["pool_id"], ["dedicated_pools.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "allocated_resource_id",
            name="uq_dedicated_pool_members_allocated_resource_id",
        ),
        sa.UniqueConstraint(
            "instance_id", name="uq_dedicated_pool_members_instance_id"
        ),
        sa.UniqueConstraint(
            "purchase_token",
            name="uq_dedicated_pool_members_purchase_token",
        ),
    )
    op.create_index(
        "ix_dedicated_pool_members_allocation",
        "dedicated_pool_members",
        ["pool_id", "status", "readiness_status", "last_ready_verified_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dedicated_pool_members_allocated_resource_id"),
        "dedicated_pool_members",
        ["allocated_resource_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dedicated_pool_members_instance_id"),
        "dedicated_pool_members",
        ["instance_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dedicated_pool_members_permission_template_revision_id"),
        "dedicated_pool_members",
        ["permission_template_revision_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dedicated_pool_members_pool_id"),
        "dedicated_pool_members",
        ["pool_id"],
        unique=False,
    )
    op.create_index(
        "ix_dedicated_pool_members_worker_scan",
        "dedicated_pool_members",
        ["status", "next_retry_at"],
        unique=False,
    )

    op.create_table(
        "permission_sync_targets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("member_id", sa.String(length=36), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=True),
        sa.Column("previous_revision_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status",
            _enum(
                "permissionsynctargetstatus",
                "PENDING",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
            ),
            nullable=False,
        ),
        sa.Column(
            "change_required",
            sa.Boolean(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.String(length=128), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("worker_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "retry_count >= 0",
            name="ck_permission_sync_targets_retry_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["permission_sync_jobs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["member_id"], ["dedicated_pool_members.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"], ["db_instance_resources.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["previous_revision_id"],
            ["permission_template_revisions.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "job_id", "member_id", name="uq_permission_sync_targets_job_member"
        ),
    )
    op.create_index(
        op.f("ix_permission_sync_targets_job_id"),
        "permission_sync_targets",
        ["job_id"],
    )
    op.create_index(
        op.f("ix_permission_sync_targets_member_id"),
        "permission_sync_targets",
        ["member_id"],
    )
    op.create_index(
        op.f("ix_permission_sync_targets_resource_id"),
        "permission_sync_targets",
        ["resource_id"],
    )
    op.create_index(
        "ix_permission_sync_targets_worker_scan",
        "permission_sync_targets",
        ["status", "next_retry_at"],
    )

    op.create_table(
        "provisioning_operation_budgets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "request_count >= 0",
            name="ck_provisioning_operation_budgets_count_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "operation",
            "window_started_at",
            name="uq_provisioning_operation_budgets_window",
        ),
    )
    op.create_index(
        "ix_provisioning_operation_budgets_window_started_at",
        "provisioning_operation_budgets",
        ["window_started_at"],
        unique=False,
    )

    op.create_table(
        "dedicated_worker_heartbeats",
        sa.Column("worker_id", sa.String(length=64), nullable=False),
        sa.Column("config_revision", sa.Integer(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "last_heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "config_revision > 0",
            name="ck_dedicated_worker_heartbeats_config_revision_positive",
        ),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_dedicated_worker_heartbeats_last_heartbeat_at",
        "dedicated_worker_heartbeats",
        ["last_heartbeat_at"],
    )

    with op.batch_alter_table("agent_provisioning_bindings") as batch_op:
        batch_op.add_column(
            sa.Column("routing_order", sa.Integer(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_agent_provisioning_binding_routing_order_nonnegative",
            "routing_order IS NULL OR routing_order >= 0",
        )
        batch_op.create_unique_constraint(
            "uq_agent_provisioning_binding_routing_order",
            ["agent_id", "routing_order"],
        )


def _ensure_downgrade_is_representable() -> None:
    bind = op.get_bind()
    dedicated_backend_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM provisioning_backends "
            "WHERE backend_type = 'DEDICATED_POOL'"
        )
    ).scalar_one()
    dedicated_resource_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM db_instance_resources "
            "WHERE provisioning_mode = 'DEDICATED'"
        )
    ).scalar_one()
    if dedicated_backend_count or dedicated_resource_count:
        raise RuntimeError(
            "cannot downgrade while Dedicated backends or resources exist"
        )


def downgrade() -> None:
    _ensure_downgrade_is_representable()

    with op.batch_alter_table("agent_provisioning_bindings") as batch_op:
        batch_op.drop_constraint(
            "uq_agent_provisioning_binding_routing_order", type_="unique"
        )
        batch_op.drop_constraint(
            "ck_agent_provisioning_binding_routing_order_nonnegative",
            type_="check",
        )
        batch_op.drop_column("routing_order")

    op.drop_index(
        "ix_dedicated_worker_heartbeats_last_heartbeat_at",
        table_name="dedicated_worker_heartbeats",
    )
    op.drop_table("dedicated_worker_heartbeats")

    op.drop_index(
        "ix_permission_sync_targets_worker_scan",
        table_name="permission_sync_targets",
    )
    op.drop_index(
        op.f("ix_permission_sync_targets_resource_id"),
        table_name="permission_sync_targets",
    )
    op.drop_index(
        op.f("ix_permission_sync_targets_member_id"),
        table_name="permission_sync_targets",
    )
    op.drop_index(
        op.f("ix_permission_sync_targets_job_id"),
        table_name="permission_sync_targets",
    )
    op.drop_table("permission_sync_targets")

    op.drop_index(
        "ix_provisioning_operation_budgets_window_started_at",
        table_name="provisioning_operation_budgets",
    )
    op.drop_table("provisioning_operation_budgets")

    op.drop_index(
        "ix_dedicated_pool_members_worker_scan",
        table_name="dedicated_pool_members",
    )
    op.drop_index(
        op.f("ix_dedicated_pool_members_pool_id"),
        table_name="dedicated_pool_members",
    )
    op.drop_index(
        op.f("ix_dedicated_pool_members_permission_template_revision_id"),
        table_name="dedicated_pool_members",
    )
    op.drop_index(
        op.f("ix_dedicated_pool_members_instance_id"),
        table_name="dedicated_pool_members",
    )
    op.drop_index(
        op.f("ix_dedicated_pool_members_allocated_resource_id"),
        table_name="dedicated_pool_members",
    )
    op.drop_index(
        "ix_dedicated_pool_members_allocation",
        table_name="dedicated_pool_members",
    )
    op.drop_table("dedicated_pool_members")

    op.execute(
        "UPDATE db_instance_resources SET status = 'DELETING' "
        "WHERE status = 'COOLING_DOWN'"
    )
    op.execute(
        "UPDATE db_instance_resources SET status = 'DELETE_FAILED' "
        "WHERE status = 'RESTORING'"
    )
    op.drop_index(
        op.f("ix_db_instance_resources_allocated_instance_id"),
        table_name="db_instance_resources",
    )
    with op.batch_alter_table("db_instance_resources") as batch_op:
        batch_op.drop_constraint(
            "fk_db_instance_resources_permission_template_revision_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_db_instance_resources_permission_template_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_db_instance_resources_allocated_instance_id",
            type_="foreignkey",
        )
        for column_name in (
            "restore_failure_reason",
            "restore_source_status",
            "delete_step",
            "reclaim_policy",
            "cooldown_until",
            "effective_delete_cooldown_duration_hours",
            "disconnected_at",
            "delete_requested_at",
            "permission_snapshot_json",
            "permission_template_revision_id",
            "permission_template_id",
            "allocated_instance_id",
            "provisioning_mode",
        ):
            batch_op.drop_column(column_name)
        batch_op.alter_column(
            "status",
            existing_type=_enum(
                "dbinstancestatus",
                "CREATING",
                "READY",
                "FAILED",
                "DELETING",
                "COOLING_DOWN",
                "RESTORING",
                "DELETED",
                "DELETE_FAILED",
            ),
            type_=_enum(
                "dbinstancestatus",
                "CREATING",
                "READY",
                "FAILED",
                "DELETING",
                "DELETED",
                "DELETE_FAILED",
            ),
            existing_nullable=False,
            postgresql_using="status::text",
        )

    op.drop_index(
        op.f("ix_provisioning_backends_permission_template_revision_id"),
        table_name="provisioning_backends",
    )
    op.drop_index(
        op.f("ix_provisioning_backends_dedicated_pool_id"),
        table_name="provisioning_backends",
    )
    with op.batch_alter_table("provisioning_backends") as batch_op:
        batch_op.drop_constraint(
            "fk_provisioning_backends_permission_template_revision_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "ck_provisioning_backends_cooldown_positive",
            type_="check",
        )
        batch_op.drop_constraint(
            "ck_provisioning_backends_target_matches_type",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_provisioning_backends_dedicated_pool_id",
            type_="foreignkey",
        )
        batch_op.alter_column(
            "admin_credential_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.alter_column(
            "instance_id",
            existing_type=sa.String(length=36),
            nullable=False,
        )
        batch_op.drop_column("permission_template_revision_id")
        batch_op.drop_column("delete_cooldown_duration_hours")
        batch_op.drop_column("dedicated_pool_id")
        batch_op.drop_column("backend_type")

    op.drop_index(
        op.f("ix_dedicated_pools_permission_template_revision_id"),
        table_name="dedicated_pools",
    )
    op.drop_table("dedicated_pools")

    op.drop_index(
        "ix_permission_sync_jobs_worker_scan",
        table_name="permission_sync_jobs",
    )
    op.drop_index(
        op.f("ix_permission_sync_jobs_template_revision_id"),
        table_name="permission_sync_jobs",
    )
    op.drop_index(
        op.f("ix_permission_sync_jobs_target_id"),
        table_name="permission_sync_jobs",
    )
    op.drop_table("permission_sync_jobs")
    op.drop_index(
        op.f("ix_permission_template_revisions_template_id"),
        table_name="permission_template_revisions",
    )
    op.drop_table("permission_template_revisions")
    op.drop_table("permission_templates")
