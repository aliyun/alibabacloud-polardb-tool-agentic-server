from __future__ import annotations

import enum
import json
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    DBInstanceResource,
    DedicatedPool,
    DedicatedPoolMember,
    PermissionSyncJob,
    PermissionSyncMode,
    PermissionSyncStatus,
    PermissionSyncTarget,
    PermissionSyncTargetStatus,
)
from server.models.permission_template import PermissionTemplateRevision


class PermissionSyncError(ValueError):
    pass


class PermissionSyncConfirmationRequired(PermissionSyncError):
    pass


class PermissionSyncTargetNotFound(PermissionSyncError):
    pass


class DatabasePrivilege(str, enum.Enum):
    CREATE = "CREATE"
    DROP = "DROP"
    ALTER = "ALTER"
    INDEX = "INDEX"
    REFERENCES = "REFERENCES"
    CREATE_VIEW = "CREATE VIEW"
    SHOW_VIEW = "SHOW VIEW"
    CREATE_ROUTINE = "CREATE ROUTINE"
    ALTER_ROUTINE = "ALTER ROUTINE"
    EXECUTE = "EXECUTE"
    SELECT = "SELECT"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    CREATE_TEMPORARY_TABLES = "CREATE TEMPORARY TABLES"
    LOCK_TABLES = "LOCK TABLES"
    CREATE_USER = "CREATE USER"


DEFAULT_DATABASE_PRIVILEGES = tuple(
    privilege
    for privilege in DatabasePrivilege
    if privilege is not DatabasePrivilege.CREATE_USER
)


class PermissionScope(str, enum.Enum):
    MULTITENANT = "tenant"
    DEDICATED = "global"


@dataclass(frozen=True, slots=True)
class CompiledPermissionSnapshot:
    template_id: str | None
    revision_id: str | None
    privileges: tuple[DatabasePrivilege, ...]
    grant_option: bool
    scope: PermissionScope
    legacy_all_privileges: bool = False


def effective_delete_cooldown_hours(
    *,
    resource_override: int | None,
    pool_or_backend_override: int | None,
    global_default: int,
) -> int:
    selected = next(
        value
        for value in (
            resource_override,
            pool_or_backend_override,
            global_default,
        )
        if value is not None
    )
    if isinstance(selected, bool) or selected < 1:
        raise ValueError("delete cooldown must be at least 1 hour")
    return selected


def _parse_privileges(privileges_json: str) -> tuple[DatabasePrivilege, ...]:
    try:
        raw = json.loads(privileges_json)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("privilege list must be valid JSON") from error
    if not isinstance(raw, list) or not raw:
        raise ValueError("privilege list must be a non-empty array")
    selected: set[DatabasePrivilege] = set()
    for value in raw:
        if not isinstance(value, str):
            raise ValueError("privilege values must be strings")
        try:
            selected.add(DatabasePrivilege(value))
        except ValueError as error:
            raise ValueError(f"unsupported privilege: {value}") from error
    return tuple(
        privilege for privilege in DatabasePrivilege if privilege in selected
    )


def compile_permission_snapshot(
    *,
    revision_id: str,
    template_id: str,
    privileges_json: str,
    grant_option: bool,
    scope: PermissionScope,
) -> CompiledPermissionSnapshot:
    privileges = _parse_privileges(privileges_json)
    if (
        scope is PermissionScope.MULTITENANT
        and DatabasePrivilege.CREATE_USER in privileges
    ):
        raise ValueError("CREATE USER requires Dedicated scope")
    return CompiledPermissionSnapshot(
        template_id=template_id,
        revision_id=revision_id,
        privileges=privileges,
        grant_option=grant_option,
        scope=scope,
    )


def default_permission_snapshot(
    scope: PermissionScope,
) -> CompiledPermissionSnapshot:
    return CompiledPermissionSnapshot(
        template_id="builtin-mysql-default",
        revision_id="builtin-mysql-default-v1",
        privileges=DEFAULT_DATABASE_PRIVILEGES,
        grant_option=False,
        scope=scope,
    )


def legacy_permission_snapshot(
    scope: PermissionScope,
) -> CompiledPermissionSnapshot:
    return CompiledPermissionSnapshot(
        template_id=None,
        revision_id=None,
        privileges=(),
        grant_option=True,
        scope=scope,
        legacy_all_privileges=True,
    )


def permission_snapshot_to_json(
    snapshot: CompiledPermissionSnapshot,
) -> str:
    payload: dict[str, object] = {
        "grant_option": snapshot.grant_option,
        "legacy": snapshot.legacy_all_privileges,
        "privileges": (
            ["ALL PRIVILEGES"]
            if snapshot.legacy_all_privileges
            else [privilege.value for privilege in snapshot.privileges]
        ),
        "scope": snapshot.scope.value,
    }
    if snapshot.revision_id is not None:
        payload["revision_id"] = snapshot.revision_id
    if snapshot.template_id is not None:
        payload["template_id"] = snapshot.template_id
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def permission_snapshot_from_json(
    encoded: str,
) -> CompiledPermissionSnapshot:
    try:
        payload = json.loads(encoded)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("permission snapshot must be valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("permission snapshot must be an object")
    try:
        scope = PermissionScope(payload["scope"])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("permission snapshot scope is invalid") from error
    grant_option = payload.get("grant_option")
    legacy = payload.get("legacy", False)
    privileges = payload.get("privileges")
    if not isinstance(grant_option, bool) or not isinstance(legacy, bool):
        raise ValueError("permission snapshot flags are invalid")
    if legacy:
        if privileges != ["ALL PRIVILEGES"]:
            raise ValueError("legacy permission snapshot is invalid")
        return legacy_permission_snapshot(scope)
    compiled = compile_permission_snapshot(
        revision_id=str(payload.get("revision_id") or ""),
        template_id=str(payload.get("template_id") or ""),
        privileges_json=json.dumps(privileges),
        grant_option=grant_option,
        scope=scope,
    )
    return CompiledPermissionSnapshot(
        template_id=compiled.template_id or None,
        revision_id=compiled.revision_id or None,
        privileges=compiled.privileges,
        grant_option=compiled.grant_option,
        scope=compiled.scope,
    )


async def create_permission_template_revision(
    session: AsyncSession,
    *,
    template_id: str,
    privileges: Sequence[str],
    grant_option: bool,
    created_by_user_id: str | None,
) -> PermissionTemplateRevision:
    compiled = compile_permission_snapshot(
        revision_id="pending",
        template_id=template_id,
        privileges_json=json.dumps(list(privileges)),
        grant_option=grant_option,
        scope=PermissionScope.DEDICATED,
    )
    statement = select(
        func.coalesce(func.max(PermissionTemplateRevision.revision), 0)
    ).where(PermissionTemplateRevision.template_id == template_id)
    if session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    current_revision = int(
        (await session.execute(statement)).scalar_one()
    )
    revision = PermissionTemplateRevision(
        template_id=template_id,
        revision=current_revision + 1,
        privileges_json=json.dumps(
            [privilege.value for privilege in compiled.privileges],
            separators=(",", ":"),
        ),
        grant_option=grant_option,
        created_by_user_id=created_by_user_id,
    )
    session.add(revision)
    await session.flush()
    return revision


def permission_snapshot_for_revision(
    revision: PermissionTemplateRevision,
) -> CompiledPermissionSnapshot:
    return compile_permission_snapshot(
        revision_id=revision.id,
        template_id=revision.template_id,
        privileges_json=revision.privileges_json,
        grant_option=revision.grant_option,
        scope=PermissionScope.DEDICATED,
    )


async def _permission_sync_members(
    session: AsyncSession,
    *,
    target_scope: str,
    target_id: str,
) -> list[DedicatedPoolMember]:
    if target_scope == "pool":
        if await session.get(DedicatedPool, target_id) is None:
            raise PermissionSyncTargetNotFound("Dedicated pool not found")
        statement = select(DedicatedPoolMember).where(
            DedicatedPoolMember.pool_id == target_id,
            DedicatedPoolMember.database_name.is_not(None),
            DedicatedPoolMember.sandbox_username_ciphertext.is_not(None),
        )
    elif target_scope == "resource":
        if await session.get(DBInstanceResource, target_id) is None:
            raise PermissionSyncTargetNotFound("Database resource not found")
        statement = select(DedicatedPoolMember).where(
            DedicatedPoolMember.allocated_resource_id == target_id,
            DedicatedPoolMember.database_name.is_not(None),
            DedicatedPoolMember.sandbox_username_ciphertext.is_not(None),
        )
    else:
        raise PermissionSyncError(
            "target_scope must be 'pool' or 'resource'"
        )
    return list((await session.scalars(statement)).all())


async def request_permission_sync(
    session: AsyncSession,
    *,
    revision_id: str,
    target_scope: str,
    target_id: str,
    mode: PermissionSyncMode,
    confirmed_by_user_id: str | None,
) -> PermissionSyncJob:
    if mode == PermissionSyncMode.APPLY and confirmed_by_user_id is None:
        raise PermissionSyncConfirmationRequired(
            "Permission synchronization requires explicit confirmation"
        )
    revision = await session.get(PermissionTemplateRevision, revision_id)
    if revision is None:
        raise PermissionSyncTargetNotFound(
            "Permission template revision not found"
        )
    # Compile before persisting a job so malformed revisions can never enter
    # the worker queue.
    permission_snapshot_for_revision(revision)
    members = await _permission_sync_members(
        session,
        target_scope=target_scope,
        target_id=target_id,
    )
    job = PermissionSyncJob(
        template_revision_id=revision.id,
        target_scope=target_scope,
        target_id=target_id,
        mode=mode,
        status=(
            PermissionSyncStatus.SUCCEEDED
            if mode == PermissionSyncMode.DRY_RUN or not members
            else PermissionSyncStatus.PENDING
        ),
        confirmed_by_user_id=(
            confirmed_by_user_id
            if mode == PermissionSyncMode.APPLY
            else None
        ),
        total_count=len(members),
        completed_count=(
            len(members) if mode == PermissionSyncMode.DRY_RUN else 0
        ),
    )
    session.add(job)
    await session.flush()
    for member in members:
        change_required = (
            member.permission_template_revision_id != revision.id
        )
        session.add(
            PermissionSyncTarget(
                job=job,
                member_id=member.id,
                resource_id=member.allocated_resource_id,
                previous_revision_id=(
                    member.permission_template_revision_id
                ),
                status=(
                    PermissionSyncTargetStatus.SUCCEEDED
                    if mode == PermissionSyncMode.DRY_RUN
                    else PermissionSyncTargetStatus.PENDING
                ),
                change_required=change_required,
            )
        )
    await session.flush()
    await session.refresh(job, attribute_names=["targets"])
    return job
