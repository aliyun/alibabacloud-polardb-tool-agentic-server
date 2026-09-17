from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from importlib import import_module
from typing import Any

import sqlalchemy as sa

from server.db.mysql_check_compat import supports_check_constraints

LEGACY_MANAGED_HEAD = "f6a7b8c9d0e1"

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RepairStep:
    revision: str
    module: str
    tables: tuple[str, ...] = ()
    columns: tuple[tuple[str, tuple[str, ...]], ...] = ()
    column_lengths: tuple[tuple[str, str, int], ...] = ()
    check_constraints: tuple[tuple[str, str, str], ...] = ()


class LegacyF6SchemaState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    COMPLETE = "complete"
    REPAIR_REQUIRED = "repair_required"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


_MANAGED_F6_FINGERPRINT = _RepairStep(
    revision="legacy-managed-f6",
    module="",
    tables=("managed_instance_bindings",),
    columns=(
        ("users", ("password_state", "credential_epoch")),
        (
            "config_operation_receipts",
            (
                "lease_owner",
                "lease_expires_at",
                "instance_id",
                "instance_generation",
            ),
        ),
    ),
)

_REPAIR_STEPS = (
    _RepairStep(
        revision="e1f2a3b4c5d6",
        module="e1f2a3b4c5d6_add_polarrag_mcp_catalog",
        tables=(
            "polarrag_instances",
            "polarrag_spaces",
            "enterprise_principal_assignments",
            "knowledge_resources",
        ),
    ),
    _RepairStep(
        revision="f2a3b4c5d6e7",
        module="f2a3b4c5d6e7_add_agent_polarrag_user_tokens",
        tables=(
            "agent_polarrag_instance_bindings",
            "agent_user_assignments",
            "agent_user_tokens",
        ),
    ),
    _RepairStep(
        revision="a3b4c5d6e7f8",
        module="a3b4c5d6e7f8_add_agent_group_assignments",
        tables=("agent_group_assignments",),
        columns=(("agent_user_assignments", ("is_direct",)),),
    ),
    _RepairStep(
        revision="b4c5d6e7f8a9",
        module="b4c5d6e7f8a9_add_polarrag_space_oss_upload",
        columns=(
            (
                "polarrag_spaces",
                (
                    "oss_bucket",
                    "oss_endpoint",
                    "oss_access_key_id_ciphertext",
                    "oss_access_key_secret_ciphertext",
                    "oss_object_prefix",
                    "oss_config_validated",
                    "oss_validated_at",
                    "oss_last_error_code",
                ),
            ),
        ),
    ),
    _RepairStep(
        revision="c5d6e7f8a9b0",
        module="c5d6e7f8a9b0_add_polarrag_upload_sessions",
        tables=("polarrag_upload_sessions",),
    ),
    _RepairStep(
        revision="d6e7f8a9b0c1",
        module="d6e7f8a9b0c1_add_polarrag_upload_cleanup_journal",
        tables=("polarrag_upload_cleanups",),
    ),
    _RepairStep(
        revision="e7f8a9b0c1d2",
        module="e7f8a9b0c1d2_snapshot_upload_cleanup_credentials",
        columns=(
            (
                "polarrag_upload_cleanups",
                (
                    "oss_access_key_id_ciphertext",
                    "oss_access_key_secret_ciphertext",
                ),
            ),
        ),
    ),
    _RepairStep(
        revision="f8a9b0c1d2e3",
        module="f8a9b0c1d2e3_add_upload_cleanup_fencing",
        columns=(
            (
                "polarrag_upload_cleanups",
                (
                    "operation_kind",
                    "operation_token",
                    "polarrag_instance_id",
                    "submission_payload_ciphertext",
                    "reconcile_required",
                ),
            ),
        ),
    ),
    _RepairStep(
        revision="f3a4b5c6d7e8",
        module="f3a4b5c6d7e8_add_dedicated_hot_pool",
        tables=(
            "permission_templates",
            "permission_template_revisions",
            "permission_sync_jobs",
            "dedicated_pools",
            "dedicated_pool_members",
            "permission_sync_targets",
            "provisioning_operation_budgets",
            "dedicated_worker_heartbeats",
        ),
        columns=(
            (
                "provisioning_backends",
                (
                    "backend_type",
                    "dedicated_pool_id",
                    "delete_cooldown_duration_hours",
                    "permission_template_revision_id",
                ),
            ),
            (
                "db_instance_resources",
                (
                    "provisioning_mode",
                    "allocated_instance_id",
                    "permission_template_id",
                    "permission_template_revision_id",
                    "permission_snapshot_json",
                    "delete_requested_at",
                    "disconnected_at",
                    "effective_delete_cooldown_duration_hours",
                    "cooldown_until",
                    "reclaim_policy",
                    "delete_step",
                    "restore_source_status",
                    "restore_failure_reason",
                ),
            ),
            ("agent_provisioning_bindings", ("routing_order",)),
        ),
    ),
    _RepairStep(
        revision="b0c1d2e3f4a5",
        module="b0c1d2e3f4a5_add_agent_public_kb_scope",
        columns=(
            (
                "agent_polarrag_instance_bindings",
                ("public_knowledge_resource_ids_json",),
            ),
        ),
    ),
    _RepairStep(
        revision="c1d2e3f4a5b6",
        module="c1d2e3f4a5b6_add_native_polarrag_principals",
        check_constraints=(
            (
                "enterprise_principal_assignments",
                "ck_enterprise_principal_native_user",
                "provider != 'polarrag'",
            ),
        ),
    ),
    _RepairStep(
        revision="c2d3e4f5a6b7",
        module="c2d3e4f5a6b7_add_polarrag_space_acl_mode",
        columns=(("polarrag_spaces", ("acl_mode",)),),
    ),
    _RepairStep(
        revision="d2e3f4a5b6c7",
        module="d2e3f4a5b6c7_add_enterprise_identity_sources",
        tables=(
            "enterprise_identity_sources",
            "enterprise_identity_source_space_bindings",
            "enterprise_directory_users",
            "enterprise_directory_groups",
            "enterprise_directory_memberships",
        ),
        columns=(("agent_group_assignments", ("identity_source_id",)),),
    ),
    _RepairStep(
        revision="e3f4a5b6c7d8",
        module="e3f4a5b6c7d8_add_feishu_tenant_verification",
        tables=("feishu_tenant_verification_states",),
    ),
    _RepairStep(
        revision="e4f5a6b7c8d9",
        module="e4f5a6b7c8d9_add_feishu_user_login_states",
        tables=("feishu_user_login_states",),
    ),
    _RepairStep(
        revision="f5a6b7c8d9e0",
        module="f5a6b7c8d9e0_add_identity_source_all_agent_access",
        column_lengths=(("agent_group_assignments", "group_kind", 32),),
        check_constraints=(
            (
                "agent_group_assignments",
                "ck_agent_group_assignment_shape",
                "identity_source_all",
            ),
        ),
    ),
    _RepairStep(
        revision="a4b5c6d7e8f9",
        module="a4b5c6d7e8f9_add_sharepoint_user_login_states",
        tables=("sharepoint_user_login_states",),
    ),
)


def _current_revisions(connection: Any) -> tuple[str, ...]:
    inspector = sa.inspect(connection)
    if not inspector.has_table("alembic_version"):
        return ()
    rows = connection.execute(
        sa.text("SELECT version_num FROM alembic_version")
    )
    return tuple(sorted(str(row[0]) for row in rows))


def _artifact_state(connection: Any, step: _RepairStep) -> str:
    inspector = sa.inspect(connection)
    existing_tables = set(inspector.get_table_names())
    checks: list[bool] = [
        table_name in existing_tables for table_name in step.tables
    ]
    for table_name, expected_columns in step.columns:
        if table_name not in existing_tables:
            checks.extend(False for _ in expected_columns)
            continue
        existing_columns = {
            column["name"] for column in inspector.get_columns(table_name)
        }
        checks.extend(
            column_name in existing_columns
            for column_name in expected_columns
        )
    for table_name, column_name, expected_length in step.column_lengths:
        if table_name not in existing_tables:
            checks.append(False)
            continue
        columns = {
            column["name"]: column
            for column in inspector.get_columns(table_name)
        }
        column = columns.get(column_name)
        checks.append(
            column is not None
            and getattr(column["type"], "length", None) == expected_length
        )
    if supports_check_constraints(connection.dialect):
        for table_name, constraint_name, sql_fragment in (
            step.check_constraints
        ):
            if table_name not in existing_tables:
                checks.append(False)
                continue
            constraints = {
                constraint.get("name"): str(
                    constraint.get("sqltext") or ""
                )
                for constraint in inspector.get_check_constraints(table_name)
            }
            checks.append(
                sql_fragment in constraints.get(constraint_name, "")
            )
    if not checks:
        return "complete"
    if checks and all(checks):
        return "complete"
    if not any(checks):
        return "absent"
    return "partial"


def _run_upgrade(operations: Any, step: _RepairStep) -> None:
    module = import_module(f"server.db.migrations.versions.{step.module}")
    original_operations = module.op
    module.op = operations
    try:
        module.upgrade()
    finally:
        module.op = original_operations


def inspect_legacy_managed_f6_schema(
    connection: Any,
) -> LegacyF6SchemaState:
    """Classify same-revision physical drift without changing the database."""
    if _current_revisions(connection) != (LEGACY_MANAGED_HEAD,):
        return LegacyF6SchemaState.NOT_APPLICABLE

    states = tuple(
        _artifact_state(connection, step) for step in _REPAIR_STEPS
    )
    if all(state == "complete" for state in states):
        return LegacyF6SchemaState.COMPLETE

    fingerprint_state = _artifact_state(
        connection, _MANAGED_F6_FINGERPRINT
    )
    if fingerprint_state != "complete":
        return LegacyF6SchemaState.UNKNOWN
    if any(state == "partial" for state in states):
        return LegacyF6SchemaState.PARTIAL
    return LegacyF6SchemaState.REPAIR_REQUIRED


def repair_legacy_managed_f6_schema(
    connection: Any,
    operations: Any,
) -> bool:
    """Repair the pre-rebase physical schema represented by the same f6 head.

    The pre-rebase managed PAS image reached f6 through d4 -> e5 -> f6.
    After rebasing, f6 reaches e5 through a4 and therefore includes additional
    PolarRAG, dedicated-pool, and enterprise-identity revisions. Alembic cannot
    discover that physical drift because the revision identifier did not
    change. Only the exact legacy fingerprint is eligible for repair.
    """
    schema_state = inspect_legacy_managed_f6_schema(connection)
    if schema_state in {
        LegacyF6SchemaState.NOT_APPLICABLE,
        LegacyF6SchemaState.COMPLETE,
    }:
        return False
    if schema_state == LegacyF6SchemaState.UNKNOWN:
        raise RuntimeError(
            "LEGACY_F6_SCHEMA_UNKNOWN: revision f6a7b8c9d0e1 does not "
            "match the supported pre-rebase managed PAS schema"
        )
    if schema_state == LegacyF6SchemaState.PARTIAL:
        raise RuntimeError(
            "LEGACY_F6_SCHEMA_PARTIAL: revision f6a7b8c9d0e1 contains "
            "a partially applied skipped migration"
        )

    repaired = False
    for step in _REPAIR_STEPS:
        state = _artifact_state(connection, step)
        if state == "complete":
            continue
        if state == "partial":
            raise RuntimeError(
                "LEGACY_F6_SCHEMA_PARTIAL: revision "
                f"{step.revision} is only partially present"
            )
        logger.warning(
            "repairing skipped legacy schema revision %s",
            step.revision,
        )
        _run_upgrade(operations, step)
        if _artifact_state(connection, step) != "complete":
            raise RuntimeError(
                "LEGACY_F6_SCHEMA_REPAIR_INCOMPLETE: revision "
                f"{step.revision} did not produce its required schema"
            )
        repaired = True
    return repaired
