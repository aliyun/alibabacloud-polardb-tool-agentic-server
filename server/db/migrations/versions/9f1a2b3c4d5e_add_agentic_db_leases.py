"""define the agent access target schema

Revision ID: 9f1a2b3c4d5e
Revises: ad71f04a14b5
Create Date: 2026-07-16 12:45:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9f1a2b3c4d5e"
down_revision: Union[str, Sequence[str], None] = "ad71f04a14b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MYSQL_OWNER_FK_SUPPORT_INDEX = (
    "ix_instances_owner_user_id_fk_support"
)


def _create_mysql_owner_fk_support_index() -> None:
    if op.get_bind().dialect.name == "mysql":
        op.create_index(
            _MYSQL_OWNER_FK_SUPPORT_INDEX,
            "instances",
            ["owner_user_id"],
            unique=False,
        )


def _drop_mysql_owner_fk_support_index() -> None:
    if op.get_bind().dialect.name == "mysql":
        op.drop_index(
            _MYSQL_OWNER_FK_SUPPORT_INDEX,
            table_name="instances",
        )


def _drop_mysql_foreign_key(
    batch_op,
    table_name: str,
    constrained_columns: list[str],
) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "mysql":
        return
    matching = [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
        if foreign_key["constrained_columns"] == constrained_columns
    ]
    if len(matching) != 1 or not matching[0].get("name"):
        raise RuntimeError(
            f"expected one named {constrained_columns} foreign key"
        )
    batch_op.drop_constraint(
        matching[0]["name"],
        type_="foreignkey",
    )


def _drop_mysql_db_account_foreign_key(batch_op) -> None:
    _drop_mysql_foreign_key(
        batch_op,
        "user_instance_bindings",
        ["db_account_id"],
    )


def _drop_mysql_audit_user_foreign_key(batch_op) -> None:
    _drop_mysql_foreign_key(
        batch_op,
        "audit_logs",
        ["user_id"],
    )


def upgrade() -> None:
    _create_mysql_owner_fk_support_index()
    op.drop_index("uix_user_active_personal", table_name="instances")
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(
            sa.Column(
                "engine",
                sa.Enum(
                    "POLARDB_MYSQL",
                    name="instanceengine",
                    native_enum=False,
                    length=32,
                ),
                server_default="POLARDB_MYSQL",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "topology",
                sa.Enum(
                    "SINGLE_TENANT",
                    "MULTITENANT",
                    name="instancetopology",
                    native_enum=False,
                    length=32,
                ),
                server_default="SINGLE_TENANT",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "allocation_mode",
                sa.Enum(
                    "AUTO_PROVISIONED",
                    "POOLED",
                    "REGISTERED",
                    name="allocationmode",
                    native_enum=False,
                    length=32,
                ),
                server_default="AUTO_PROVISIONED",
                nullable=False,
            )
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(
                "ACTIVE",
                "STOPPED",
                "CREATING",
                "POOLED",
                "POOL_CREATING",
                "FAILED",
                name="instancestatus",
                native_enum=False,
            ),
            type_=sa.Enum(
                "CREATING",
                "ACTIVE",
                "STOPPED",
                "FAILED",
                name="instancestatus",
                native_enum=False,
                length=32,
            ),
            existing_nullable=False,
            postgresql_using="status::text",
        )
        batch_op.drop_column("type")
    op.create_index(
        "uix_user_active_personal",
        "instances",
        ["owner_user_id"],
        unique=True,
        sqlite_where=sa.text("allocation_mode = 'AUTO_PROVISIONED' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"),
        postgresql_where=sa.text(
            "allocation_mode = 'AUTO_PROVISIONED' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"
        ),
    )
    _drop_mysql_owner_fk_support_index()

    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "DISABLED",
                name="agentstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("max_active_resources", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "max_active_resources IS NULL OR max_active_resources > 0",
            name="ck_agents_max_active_resources_positive",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )

    op.create_table(
        "instance_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("instance_id", sa.String(length=36), nullable=True),
        sa.Column("resource_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "purpose",
            sa.Enum(
                "PROVISIONING_ADMIN",
                "DIRECT_ACCESS",
                "RESOURCE_ACCESS",
                name="credentialpurpose",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "capability",
            sa.Enum(
                "READONLY",
                "READWRITE",
                "ADMIN",
                name="credentialcapability",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("username_ciphertext", sa.String(length=1024), nullable=True),
        sa.Column("password_ciphertext", sa.String(length=2048), nullable=True),
        sa.Column("database_name", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "REVOKED",
                name="credentialstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(instance_id IS NOT NULL AND resource_id IS NULL) OR (instance_id IS NULL AND resource_id IS NOT NULL)",
            name="ck_instance_credentials_exactly_one_owner",
        ),
        sa.CheckConstraint(
            "status != 'ACTIVE' OR (username_ciphertext IS NOT NULL AND password_ciphertext IS NOT NULL)",
            name="ck_instance_credentials_active_has_ciphertext",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_instance_credentials_version_positive",
        ),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instance_id",
            "name",
            name="uq_instance_credentials_instance_name",
        ),
        sa.UniqueConstraint(
            "resource_id",
            "purpose",
            name="uq_instance_credentials_resource_purpose",
        ),
    )
    op.create_index(
        op.f("ix_instance_credentials_instance_id"),
        "instance_credentials",
        ["instance_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_instance_credentials_resource_id"),
        "instance_credentials",
        ["resource_id"],
        unique=False,
    )

    op.create_table(
        "agent_api_tokens",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("token_prefix", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("token_ciphertext", sa.String(length=2048), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", name="uq_agent_api_tokens_agent_id"),
        sa.UniqueConstraint("token_hash", name="uq_agent_api_tokens_token_hash"),
    )
    op.create_index(
        op.f("ix_agent_api_tokens_agent_id"),
        "agent_api_tokens",
        ["agent_id"],
        unique=False,
    )
    op.create_table(
        "agent_token_reveal_limits",
        sa.Column("admin_id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "request_count > 0 AND request_count <= 5",
            name="ck_agent_token_reveal_limits_count",
        ),
        sa.ForeignKeyConstraint(
            ["admin_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"], ["agents.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("admin_id", "agent_id"),
    )
    op.create_index(
        "ix_agent_token_reveal_limits_window_started_at",
        "agent_token_reveal_limits",
        ["window_started_at"],
        unique=False,
    )

    op.create_table(
        "provisioning_backends",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("instance_id", sa.String(length=36), nullable=False),
        sa.Column("admin_credential_id", sa.String(length=36), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "ACTIVE",
                "DRAINING",
                "DISABLED",
                name="provisioningbackendstatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_active_resources", sa.Integer(), nullable=False),
        sa.Column(
            "resource_min_cpu",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "resource_max_cpu",
            sa.Integer(),
            server_default="2",
            nullable=False,
        ),
        sa.Column(
            "ddl_concurrency",
            sa.Integer(),
            server_default="4",
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "max_active_resources > 0",
            name="ck_provisioning_backends_max_active_resources_positive",
        ),
        sa.CheckConstraint(
            "resource_min_cpu >= 0 AND resource_max_cpu > 0 AND resource_min_cpu <= resource_max_cpu",
            name="ck_provisioning_backends_resource_cpu_range",
        ),
        sa.CheckConstraint(
            "ddl_concurrency > 0",
            name="ck_provisioning_backends_ddl_concurrency_positive",
        ),
        sa.ForeignKeyConstraint(
            ["admin_credential_id"],
            ["instance_credentials.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("admin_credential_id"),
    )
    op.create_index(
        op.f("ix_provisioning_backends_instance_id"),
        "provisioning_backends",
        ["instance_id"],
        unique=True,
    )

    op.create_table(
        "db_instance_resources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_agent_id", sa.String(length=36), nullable=False),
        sa.Column("backend_id", sa.String(length=36), nullable=False),
        sa.Column("client_token", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column(
            "fingerprint_version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column(
            "engine",
            sa.Enum(
                "POLARDB_MYSQL",
                name="instanceengine",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "CREATING",
                "READY",
                "FAILED",
                "DELETING",
                "DELETED",
                "DELETE_FAILED",
                name="dbinstancestatus",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("tenant_name", sa.String(length=32), nullable=True),
        sa.Column("resource_config_name", sa.String(length=64), nullable=True),
        sa.Column("database_name", sa.String(length=255), nullable=True),
        sa.Column(
            "provisioning_step",
            sa.Enum(
                "PENDING",
                "RESOURCE_CONFIG_CREATED",
                "TENANT_CREATED",
                "USER_CREATED",
                "DATABASE_CREATED",
                "GRANTED",
                "VERIFIED",
                name="leaseprovisioningstep",
                native_enum=False,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column(
            "cleanup_step",
            sa.Enum(
                "PENDING",
                "DATABASE_DROPPED",
                "TENANT_DROPPED",
                "RESOURCE_CONFIG_DROPPED",
                "RESIDUE_VERIFIED",
                name="leasecleanupstep",
                native_enum=False,
                length=64,
            ),
            nullable=False,
        ),
        sa.Column(
            "cleanup_required",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.String(length=2048), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("worker_lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capacity_released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["backend_id"], ["provisioning_backends.id"]),
        sa.ForeignKeyConstraint(["owner_agent_id"], ["agents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_agent_id",
            "client_token",
            name="uq_db_instance_resources_agent_client_token",
        ),
        sa.UniqueConstraint("tenant_name", name="uq_db_instance_resources_tenant_name"),
    )
    op.create_index(
        op.f("ix_db_instance_resources_backend_id"),
        "db_instance_resources",
        ["backend_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_db_instance_resources_owner_agent_id"),
        "db_instance_resources",
        ["owner_agent_id"],
        unique=False,
    )
    op.create_index(
        "ix_db_instance_resources_worker_scan",
        "db_instance_resources",
        ["status", "next_retry_at"],
        unique=False,
    )
    op.create_index(
        "ix_db_instance_resources_capacity",
        "db_instance_resources",
        ["backend_id", "status"],
        unique=False,
    )
    with op.batch_alter_table("instance_credentials") as batch_op:
        batch_op.create_foreign_key(
            "fk_instance_credentials_resource_id",
            "db_instance_resources",
            ["resource_id"],
            ["id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("user_instance_bindings") as batch_op:
        batch_op.alter_column(
            "permission",
            existing_type=sa.Enum(
                "READONLY",
                "READWRITE",
                name="permission",
                native_enum=False,
            ),
            type_=sa.Enum(
                "READONLY",
                "READWRITE",
                name="permission",
                native_enum=False,
                length=32,
            ),
            existing_nullable=False,
            postgresql_using="permission::text",
        )
        batch_op.add_column(sa.Column("credential_id", sa.String(length=36), nullable=True))
        batch_op.add_column(
            sa.Column(
                "enabled",
                sa.Boolean(),
                server_default=sa.true(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "origin",
                sa.Enum(
                    "SYSTEM",
                    "ADMIN",
                    name="bindingorigin",
                    native_enum=False,
                    length=32,
                ),
                server_default="ADMIN",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("tenant_name", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "provisioning_step",
                sa.Enum(
                    "PENDING",
                    "RESOURCE_CONFIG",
                    "TENANT",
                    "USER",
                    "DATABASE",
                    "GRANT",
                    name="tenantprovisioningstep",
                    native_enum=False,
                    length=64,
                ),
                nullable=True,
            )
        )
        batch_op.create_foreign_key(
            "fk_user_instance_bindings_credential_id",
            "instance_credentials",
            ["credential_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        _drop_mysql_db_account_foreign_key(batch_op)
        batch_op.drop_column("db_account_id")

    with op.batch_alter_table("department_instance_bindings") as batch_op:
        batch_op.alter_column(
            "default_permission",
            existing_type=sa.Enum(
                "READONLY",
                "READWRITE",
                name="permission",
                native_enum=False,
            ),
            type_=sa.Enum(
                "READONLY",
                "READWRITE",
                name="permission",
                native_enum=False,
                length=32,
            ),
            existing_nullable=False,
            postgresql_using="default_permission::text",
        )

    op.create_table(
        "user_instance_binding_capabilities",
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column(
            "capability",
            sa.Enum(
                "db_instance:list",
                "db_instance:describe",
                "db_instance:credentials:read",
                "sql:read",
                "sql:write",
                name="bindingcapability",
                native_enum=False,
                length=64,
            ),
            nullable=False,
        ),
        sa.CheckConstraint(
            "capability IN ('db_instance:list', 'db_instance:describe', "
            "'db_instance:credentials:read', 'sql:read', 'sql:write')",
            name="ck_user_instance_binding_capabilities_value",
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["user_instance_bindings.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("binding_id", "capability"),
    )

    op.create_table(
        "agent_instance_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("instance_id", sa.String(length=36), nullable=False),
        sa.Column("credential_id", sa.String(length=36), nullable=False),
        sa.Column(
            "permission",
            sa.Enum(
                "READONLY",
                "READWRITE",
                name="permission",
                native_enum=False,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["instance_credentials.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "instance_id", name="uq_agent_instance_binding"),
    )
    op.create_index(
        op.f("ix_agent_instance_bindings_agent_id"),
        "agent_instance_bindings",
        ["agent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_instance_bindings_instance_id"),
        "agent_instance_bindings",
        ["instance_id"],
        unique=False,
    )

    op.create_table(
        "agent_instance_binding_capabilities",
        sa.Column("binding_id", sa.String(length=36), nullable=False),
        sa.Column(
            "capability",
            sa.Enum(
                "db_instance:list",
                "db_instance:describe",
                "db_instance:credentials:read",
                "sql:read",
                "sql:write",
                name="bindingcapability",
                native_enum=False,
                length=64,
            ),
            nullable=False,
        ),
        sa.CheckConstraint(
            "capability IN ('db_instance:list', 'db_instance:describe', "
            "'db_instance:credentials:read', 'sql:read', 'sql:write')",
            name="ck_agent_instance_binding_capabilities_value",
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"],
            ["agent_instance_bindings.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("binding_id", "capability"),
    )

    op.create_table(
        "agent_provisioning_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("backend_id", sa.String(length=36), nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"]),
        sa.ForeignKeyConstraint(["backend_id"], ["provisioning_backends.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_id",
            "backend_id",
            name="uq_agent_provisioning_binding",
        ),
    )
    op.create_index(
        op.f("ix_agent_provisioning_bindings_agent_id"),
        "agent_provisioning_bindings",
        ["agent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_agent_provisioning_bindings_backend_id"),
        "agent_provisioning_bindings",
        ["backend_id"],
        unique=False,
    )

    op.create_table(
        "provisioning_backend_health",
        sa.Column("backend_id", sa.String(length=36), nullable=False),
        sa.Column(
            "healthy",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["backend_id"], ["provisioning_backends.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("backend_id"),
    )

    op.create_table(
        "provisioning_capacities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=64), nullable=False),
        sa.Column("active_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "active_count >= 0",
            name="ck_provisioning_capacities_active_count_nonnegative",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            name="uq_provisioning_capacities_scope",
        ),
    )

    with op.batch_alter_table("audit_logs") as batch_op:
        _drop_mysql_audit_user_foreign_key(batch_op)
    op.drop_index(op.f("ix_audit_logs_user_id"), table_name="audit_logs")
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.add_column(sa.Column("actor_user_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("actor_agent_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("target_type", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("target_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("request_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("error_code", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("metadata_json", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_audit_logs_actor_user_id",
            "users",
            ["actor_user_id"],
            ["id"],
        )
        batch_op.create_foreign_key(
            "fk_audit_logs_actor_agent_id",
            "agents",
            ["actor_agent_id"],
            ["id"],
        )
        batch_op.create_check_constraint(
            "ck_audit_logs_exactly_one_actor",
            "(actor_user_id IS NOT NULL AND actor_agent_id IS NULL) OR "
            "(actor_user_id IS NULL AND actor_agent_id IS NOT NULL)",
        )
        batch_op.drop_column("user_id")
        batch_op.drop_column("sql_text")
        batch_op.drop_column("error_message")
        batch_op.drop_column("row_count")
        batch_op.drop_column("sql_type")
        batch_op.drop_column("user_name")
        batch_op.drop_column("instance_name")
        batch_op.drop_column("db_name")
        batch_op.drop_column("client_info")
    op.create_index(
        op.f("ix_audit_logs_actor_user_id"),
        "audit_logs",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_logs_actor_agent_id"),
        "audit_logs",
        ["actor_agent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_audit_logs_request_id"),
        "audit_logs",
        ["request_id"],
        unique=False,
    )

    op.drop_table("db_accounts")


def downgrade() -> None:
    op.create_table(
        "db_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("instance_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("account_name", sa.String(length=255), nullable=False),
        sa.Column("account_password_enc", sa.String(length=512), nullable=False),
        sa.Column(
            "account_type",
            sa.Enum("NORMAL", "SUPER", name="accounttype", native_enum=False),
            nullable=False,
        ),
        sa.Column("tenant_name", sa.String(length=10), nullable=True),
        sa.Column(
            "provisioning_step",
            sa.Enum(
                "PENDING",
                "RESOURCE_CONFIG",
                "TENANT",
                "USER",
                "DATABASE",
                "GRANT",
                name="tenantprovisioningstep",
                native_enum=False,
            ),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["instance_id"], ["instances.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instance_id", "account_name", name="uq_instance_account_name"),
        sa.UniqueConstraint("instance_id", "user_id", name="uq_instance_user_account"),
    )
    op.create_index(
        op.f("ix_db_accounts_instance_id"),
        "db_accounts",
        ["instance_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_db_accounts_user_id"),
        "db_accounts",
        ["user_id"],
        unique=False,
    )

    op.drop_index(op.f("ix_audit_logs_request_id"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_actor_agent_id"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_actor_user_id"), table_name="audit_logs")
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.String(length=36), nullable=False))
        batch_op.add_column(sa.Column("sql_text", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("row_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("sql_type", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("user_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("instance_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("db_name", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("client_info", sa.String(length=255), nullable=True))
        batch_op.create_foreign_key("fk_audit_logs_user_id", "users", ["user_id"], ["id"])
        batch_op.drop_constraint("ck_audit_logs_exactly_one_actor", type_="check")
        batch_op.drop_constraint(
            "fk_audit_logs_actor_user_id",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "fk_audit_logs_actor_agent_id",
            type_="foreignkey",
        )
        batch_op.drop_column("actor_user_id")
        batch_op.drop_column("actor_agent_id")
        batch_op.drop_column("target_type")
        batch_op.drop_column("target_id")
        batch_op.drop_column("request_id")
        batch_op.drop_column("error_code")
        batch_op.drop_column("metadata_json")
    op.create_index(
        op.f("ix_audit_logs_user_id"),
        "audit_logs",
        ["user_id"],
        unique=False,
    )

    op.drop_table("provisioning_capacities")
    op.drop_table("provisioning_backend_health")
    op.drop_index(
        op.f("ix_agent_provisioning_bindings_backend_id"),
        table_name="agent_provisioning_bindings",
    )
    op.drop_index(
        op.f("ix_agent_provisioning_bindings_agent_id"),
        table_name="agent_provisioning_bindings",
    )
    op.drop_table("agent_provisioning_bindings")
    op.drop_table("agent_instance_binding_capabilities")
    op.drop_index(
        op.f("ix_agent_instance_bindings_instance_id"),
        table_name="agent_instance_bindings",
    )
    op.drop_index(
        op.f("ix_agent_instance_bindings_agent_id"),
        table_name="agent_instance_bindings",
    )
    op.drop_table("agent_instance_bindings")
    op.drop_table("user_instance_binding_capabilities")

    with op.batch_alter_table("user_instance_bindings") as batch_op:
        batch_op.add_column(sa.Column("db_account_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key(
            "fk_user_instance_bindings_db_account_id",
            "db_accounts",
            ["db_account_id"],
            ["id"],
        )
        batch_op.drop_constraint(
            "fk_user_instance_bindings_credential_id",
            type_="foreignkey",
        )
        batch_op.drop_column("credential_id")
        batch_op.drop_column("enabled")
        batch_op.drop_column("origin")
        batch_op.drop_column("tenant_name")
        batch_op.drop_column("provisioning_step")

    with op.batch_alter_table("instance_credentials") as batch_op:
        batch_op.drop_constraint("fk_instance_credentials_resource_id", type_="foreignkey")
    op.drop_index(
        "ix_db_instance_resources_capacity",
        table_name="db_instance_resources",
    )
    op.drop_index(
        "ix_db_instance_resources_worker_scan",
        table_name="db_instance_resources",
    )
    op.drop_index(
        op.f("ix_db_instance_resources_owner_agent_id"),
        table_name="db_instance_resources",
    )
    op.drop_index(
        op.f("ix_db_instance_resources_backend_id"),
        table_name="db_instance_resources",
    )
    op.drop_table("db_instance_resources")
    op.drop_index(
        op.f("ix_provisioning_backends_instance_id"),
        table_name="provisioning_backends",
    )
    op.drop_table("provisioning_backends")
    op.drop_index(
        "ix_agent_token_reveal_limits_window_started_at",
        table_name="agent_token_reveal_limits",
    )
    op.drop_table("agent_token_reveal_limits")
    op.drop_index(op.f("ix_agent_api_tokens_agent_id"), table_name="agent_api_tokens")
    op.drop_table("agent_api_tokens")
    op.drop_index(
        op.f("ix_instance_credentials_resource_id"),
        table_name="instance_credentials",
    )
    op.drop_index(
        op.f("ix_instance_credentials_instance_id"),
        table_name="instance_credentials",
    )
    op.drop_table("instance_credentials")
    op.drop_table("agents")

    _create_mysql_owner_fk_support_index()
    op.drop_index("uix_user_active_personal", table_name="instances")
    with op.batch_alter_table("instances") as batch_op:
        batch_op.add_column(
            sa.Column(
                "type",
                sa.Enum(
                    "PERSONAL",
                    "SHARED",
                    "MULTITENANT",
                    name="instancetype",
                    native_enum=False,
                ),
                server_default="PERSONAL",
                nullable=False,
            )
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.Enum(
                "CREATING",
                "ACTIVE",
                "STOPPED",
                "FAILED",
                name="instancestatus",
                native_enum=False,
            ),
            type_=sa.Enum(
                "ACTIVE",
                "STOPPED",
                "CREATING",
                "POOLED",
                "POOL_CREATING",
                "FAILED",
                name="instancestatus",
                native_enum=False,
            ),
            existing_nullable=False,
        )
        batch_op.drop_column("allocation_mode")
        batch_op.drop_column("topology")
        batch_op.drop_column("engine")
    op.create_index(
        "uix_user_active_personal",
        "instances",
        ["owner_user_id"],
        unique=True,
        sqlite_where=sa.text("type = 'PERSONAL' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"),
        postgresql_where=sa.text("type = 'PERSONAL' AND status IN ('CREATING', 'ACTIVE', 'STOPPED')"),
    )
    _drop_mysql_owner_fk_support_index()
