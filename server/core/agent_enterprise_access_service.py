from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.audit_logger import log_audit
from server.enterprise_identity.service import identity_provider_key
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentGroupKind,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    AuditStatus,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus,
    PolarRAGSpace,
    User,
    UserExternalIdentity,
    UserStatus,
)


@dataclass(frozen=True)
class EnterpriseAccessSelection:
    identity_source_id: str
    all_synced_users: bool
    directory_group_ids: tuple[str, ...]
    pas_user_ids: tuple[str, ...]
    knowledge_space_ids: tuple[str, ...]


@dataclass(frozen=True)
class EnterpriseAccessImpact:
    relation_type: str
    relation_id: str | None
    display_name: str
    scope: str


@dataclass(frozen=True)
class EnterpriseAccessPreview:
    selection: EnterpriseAccessSelection
    creates: tuple[EnterpriseAccessImpact, ...]
    reuses: tuple[EnterpriseAccessImpact, ...]
    global_changes: tuple[EnterpriseAccessImpact, ...]
    preview_hash: str


class EnterpriseAccessValidationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class EnterpriseAccessPreviewStaleError(Exception):
    def __init__(self, preview: EnterpriseAccessPreview) -> None:
        super().__init__("Enterprise access preview is stale")
        self.preview = preview


class EnterpriseAccessAuditError(Exception):
    pass


@dataclass(frozen=True)
class _PreparedEnterpriseAccess:
    preview: EnterpriseAccessPreview
    source: EnterpriseIdentitySource
    groups: tuple[EnterpriseDirectoryGroup, ...]
    users: tuple[User, ...]
    spaces: tuple[PolarRAGSpace, ...]


def _normalize(selection: EnterpriseAccessSelection) -> EnterpriseAccessSelection:
    return EnterpriseAccessSelection(
        identity_source_id=selection.identity_source_id.strip(),
        all_synced_users=selection.all_synced_users,
        directory_group_ids=tuple(
            sorted({item.strip() for item in selection.directory_group_ids if item.strip()})
        ),
        pas_user_ids=tuple(
            sorted({item.strip() for item in selection.pas_user_ids if item.strip()})
        ),
        knowledge_space_ids=tuple(
            sorted({item.strip() for item in selection.knowledge_space_ids if item.strip()})
        ),
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _timestamp(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _validation_error(code: str, message: str) -> EnterpriseAccessValidationError:
    return EnterpriseAccessValidationError(code, message)


async def _prepare_enterprise_access(
    session: AsyncSession,
    *,
    agent_id: str,
    selection: EnterpriseAccessSelection,
    for_update: bool = False,
) -> _PreparedEnterpriseAccess:
    normalized = _normalize(selection)
    if not normalized.identity_source_id:
        raise _validation_error(
            "ENTERPRISE_ACCESS_SOURCE_NOT_ELIGIBLE",
            "Select an active enterprise identity source",
        )
    if not (
        normalized.all_synced_users
        or normalized.directory_group_ids
        or normalized.pas_user_ids
    ):
        raise _validation_error(
            "ENTERPRISE_ACCESS_SUBJECT_REQUIRED",
            "Select at least one Agent access subject",
        )
    if not normalized.knowledge_space_ids:
        raise _validation_error(
            "ENTERPRISE_ACCESS_SPACE_REQUIRED",
            "Select at least one PolarRAG Space",
        )

    agent = await session.get(Agent, agent_id, with_for_update=for_update)
    if agent is None:
        raise _validation_error(
            "ENTERPRISE_ACCESS_AGENT_NOT_FOUND",
            "Agent not found",
        )
    source = await session.get(
        EnterpriseIdentitySource,
        normalized.identity_source_id,
        with_for_update=for_update,
    )
    current = datetime.now(UTC)
    if (
        source is None
        or source.status != EnterpriseIdentitySourceStatus.ACTIVE
        or source.last_synced_at is None
        or _as_utc(source.last_synced_at)
        < current - timedelta(seconds=source.stale_after_seconds)
    ):
        raise _validation_error(
            "ENTERPRISE_ACCESS_SOURCE_NOT_ELIGIBLE",
            "Identity source must be active and freshly synchronized",
        )

    group_statement = (
        select(EnterpriseDirectoryGroup)
        .where(
            EnterpriseDirectoryGroup.id.in_(normalized.directory_group_ids),
            EnterpriseDirectoryGroup.identity_source_id == source.id,
            EnterpriseDirectoryGroup.principal_type
            == EnterpriseDirectoryPrincipalType.GROUP,
            EnterpriseDirectoryGroup.status
            == EnterpriseDirectoryEntryStatus.ACTIVE,
        )
        .order_by(EnterpriseDirectoryGroup.id)
    )
    if for_update:
        group_statement = group_statement.with_for_update()
    groups = tuple((await session.execute(group_statement)).scalars())
    if len(groups) != len(normalized.directory_group_ids):
        raise _validation_error(
            "ENTERPRISE_ACCESS_GROUP_NOT_ELIGIBLE",
            "Every selected group must be active and belong to the identity source",
        )

    user_statement = (
        select(User, UserExternalIdentity, EnterpriseDirectoryUser)
        .join(
            UserExternalIdentity,
            UserExternalIdentity.user_id == User.id,
        )
        .join(
            EnterpriseDirectoryUser,
            (
                EnterpriseDirectoryUser.external_user_id
                == UserExternalIdentity.external_subject
            )
            & (EnterpriseDirectoryUser.identity_source_id == source.id),
        )
        .where(
            User.id.in_(normalized.pas_user_ids),
            User.status == UserStatus.ACTIVE,
            UserExternalIdentity.identity_provider
            == identity_provider_key(source),
            EnterpriseDirectoryUser.status
            == EnterpriseDirectoryEntryStatus.ACTIVE,
        )
        .order_by(
            User.id,
            UserExternalIdentity.id,
            EnterpriseDirectoryUser.id,
        )
    )
    if for_update:
        user_statement = user_statement.with_for_update()
    user_rows = list((await session.execute(user_statement)).all())
    user_row_by_user_id: dict[
        str,
        tuple[User, UserExternalIdentity, EnterpriseDirectoryUser],
    ] = {}
    for user, identity, directory_user in user_rows:
        user_row_by_user_id.setdefault(
            user.id,
            (user, identity, directory_user),
        )
    if set(user_row_by_user_id) != set(normalized.pas_user_ids):
        raise _validation_error(
            "ENTERPRISE_ACCESS_USER_NOT_ELIGIBLE",
            "Every selected PAS user must have an active synchronized identity",
        )
    selected_user_rows = [
        user_row_by_user_id[user_id] for user_id in normalized.pas_user_ids
    ]
    users = tuple(row[0] for row in selected_user_rows)

    space_statement = (
        select(PolarRAGSpace)
        .where(
            PolarRAGSpace.knowledge_space_id.in_(
                normalized.knowledge_space_ids
            ),
            PolarRAGSpace.enabled.is_(True),
        )
        .order_by(PolarRAGSpace.knowledge_space_id)
    )
    if for_update:
        space_statement = space_statement.with_for_update()
    spaces = tuple((await session.execute(space_statement)).scalars())
    agent_binding_ids_statement = select(
        AgentPolarRAGInstanceBinding.polarrag_instance_id
    ).where(AgentPolarRAGInstanceBinding.agent_id == agent.id)
    if for_update:
        agent_binding_ids_statement = (
            agent_binding_ids_statement.with_for_update()
        )
    bound_instances = set(
        (await session.execute(agent_binding_ids_statement)).scalars()
    )
    if len(spaces) != len(normalized.knowledge_space_ids) or any(
        space.polarrag_instance_id not in bound_instances for space in spaces
    ):
        raise _validation_error(
            "ENTERPRISE_ACCESS_SPACE_NOT_ELIGIBLE",
            "Every selected Space must be enabled on an Agent-bound instance",
        )

    group_assignment_statement = select(AgentGroupAssignment).where(
        AgentGroupAssignment.agent_id == agent.id,
        AgentGroupAssignment.identity_source_id == source.id,
        AgentGroupAssignment.group_kind.in_(
            [
                AgentGroupKind.IDENTITY_SOURCE,
                AgentGroupKind.IDENTITY_SOURCE_ALL,
            ]
        ),
    )
    if for_update:
        group_assignment_statement = group_assignment_statement.with_for_update()
    group_assignments = list(
        (await session.execute(group_assignment_statement)).scalars()
    )
    group_by_key = {row.group_key: row for row in group_assignments}
    user_assignment_statement = select(AgentUserAssignment).where(
        AgentUserAssignment.agent_id == agent.id,
        AgentUserAssignment.user_id.in_(normalized.pas_user_ids),
    )
    if for_update:
        user_assignment_statement = user_assignment_statement.with_for_update()
    user_assignments = list(
        (await session.execute(user_assignment_statement)).scalars()
    )
    user_assignment_by_user = {row.user_id: row for row in user_assignments}
    space_binding_statement = select(
        EnterpriseIdentitySourceSpaceBinding
    ).where(
        EnterpriseIdentitySourceSpaceBinding.identity_source_id == source.id,
        EnterpriseIdentitySourceSpaceBinding.knowledge_space_id.in_(
            normalized.knowledge_space_ids
        ),
    )
    if for_update:
        space_binding_statement = space_binding_statement.with_for_update()
    space_bindings = list(
        (await session.execute(space_binding_statement)).scalars()
    )
    space_binding_by_space = {
        row.knowledge_space_id: row for row in space_bindings
    }

    creates: list[EnterpriseAccessImpact] = []
    reuses: list[EnterpriseAccessImpact] = []
    global_changes: list[EnterpriseAccessImpact] = []
    if normalized.all_synced_users:
        candidate = AgentGroupAssignment.for_identity_source_all_users(
            agent_id=agent.id,
            identity_source_id=source.id,
            created_by_user_id=None,
        )
        existing_group = group_by_key.get(candidate.group_key)
        impact = EnterpriseAccessImpact(
            relation_type="agent_identity_source_all_users",
            relation_id=(
                existing_group.id if existing_group is not None else None
            ),
            display_name="All synchronized users",
            scope="agent",
        )
        (reuses if existing_group is not None else creates).append(impact)
    for group in groups:
        candidate = AgentGroupAssignment.for_identity_source_group(
            agent_id=agent.id,
            identity_source_id=source.id,
            external_group_id=group.external_group_id,
            created_by_user_id=None,
        )
        existing_group = group_by_key.get(candidate.group_key)
        impact = EnterpriseAccessImpact(
            relation_type="agent_identity_source_group",
            relation_id=(
                existing_group.id if existing_group is not None else None
            ),
            display_name=group.display_name,
            scope="agent",
        )
        (reuses if existing_group is not None else creates).append(impact)
    for user in users:
        existing_user = user_assignment_by_user.get(user.id)
        impact = EnterpriseAccessImpact(
            relation_type="agent_user",
            relation_id=(
                existing_user.id
                if existing_user is not None and existing_user.is_direct
                else None
            ),
            display_name=user.display_name,
            scope="agent",
        )
        (
            reuses
            if existing_user is not None and existing_user.is_direct
            else creates
        ).append(impact)
    for space in spaces:
        existing_space_binding = space_binding_by_space.get(
            space.knowledge_space_id
        )
        impact = EnterpriseAccessImpact(
            relation_type="identity_source_space_binding",
            relation_id=(
                existing_space_binding.id
                if existing_space_binding is not None
                else None
            ),
            display_name=space.name,
            scope="global",
        )
        (
            reuses if existing_space_binding is not None else global_changes
        ).append(impact)

    agent_binding_statement = (
        select(AgentPolarRAGInstanceBinding)
        .where(AgentPolarRAGInstanceBinding.agent_id == agent.id)
        .order_by(AgentPolarRAGInstanceBinding.id)
    )
    if for_update:
        agent_binding_statement = agent_binding_statement.with_for_update()
    agent_bindings = list(
        (await session.execute(agent_binding_statement)).scalars()
    )
    hash_payload = {
        "selection": asdict(normalized),
        "agent_id": agent.id,
        "source": {
            "id": source.id,
            "provider": source.provider.value,
            "tenant_id": source.tenant_id,
            "status": source.status.value,
            "last_synced_at": _timestamp(source.last_synced_at),
            "stale_after_seconds": source.stale_after_seconds,
        },
        "groups": [
            {
                "id": group.id,
                "external_group_id": group.external_group_id,
                "principal_type": group.principal_type.value,
                "status": group.status.value,
            }
            for group in groups
        ],
        "users": [
            {
                "user_id": user.id,
                "identity_id": identity.id,
                "external_subject": identity.external_subject,
                "directory_user_id": directory_user.id,
                "directory_status": directory_user.status.value,
            }
            for user, identity, directory_user in selected_user_rows
        ],
        "spaces": [
            {
                "id": space.knowledge_space_id,
                "instance_id": space.polarrag_instance_id,
                "space_id": space.space_id,
                "identity_domain": space.identity_domain,
                "acl_mode": space.acl_mode.value,
                "enabled": space.enabled,
                "last_synced_at": _timestamp(space.last_synced_at),
            }
            for space in spaces
        ],
        "agent_bindings": [
            {
                "id": binding.id,
                "instance_id": binding.polarrag_instance_id,
                "public_scope": binding.public_knowledge_resource_ids_json,
            }
            for binding in agent_bindings
        ],
        "existing_relations": sorted(
            [
                ["agent_group", row.id, row.group_key]
                for row in group_assignments
            ]
            + [
                ["agent_user", row.id, row.user_id, row.is_direct]
                for row in user_assignments
            ]
            + [
                ["source_space", row.id, row.knowledge_space_id]
                for row in space_bindings
            ]
        ),
    }
    preview_hash = hashlib.sha256(
        json.dumps(
            hash_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    preview = EnterpriseAccessPreview(
        selection=normalized,
        creates=tuple(creates),
        reuses=tuple(reuses),
        global_changes=tuple(global_changes),
        preview_hash=preview_hash,
    )
    return _PreparedEnterpriseAccess(
        preview=preview,
        source=source,
        groups=groups,
        users=users,
        spaces=spaces,
    )


async def preview_enterprise_access(
    session: AsyncSession,
    *,
    agent_id: str,
    selection: EnterpriseAccessSelection,
) -> EnterpriseAccessPreview:
    return (
        await _prepare_enterprise_access(
            session,
            agent_id=agent_id,
            selection=selection,
        )
    ).preview


_RelationT = TypeVar("_RelationT")


def _is_exact_duplicate(
    exc: IntegrityError,
    *,
    constraint_name: str,
    sqlite_columns: str,
) -> bool:
    diagnostic = getattr(exc.orig, "diag", None)
    actual_constraint = getattr(diagnostic, "constraint_name", None)
    if actual_constraint is not None:
        return bool(actual_constraint == constraint_name)
    original_args = getattr(exc.orig, "args", ())
    if original_args and original_args[0] == 1062:
        match = re.search(r"for key ['`]([^'`]+)['`]", str(exc.orig))
        if match is not None:
            actual_key = match.group(1)
            return actual_key == constraint_name or actual_key.rsplit(".", 1)[-1] == constraint_name
    return f"UNIQUE constraint failed: {sqlite_columns}" in str(exc.orig)


async def _insert_or_reuse(
    session: AsyncSession,
    row: _RelationT,
    *,
    lookup: Select[tuple[_RelationT]],
    constraint_name: str,
    sqlite_columns: str,
) -> tuple[_RelationT, bool]:
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
        return row, True
    except IntegrityError as exc:
        if not _is_exact_duplicate(
            exc,
            constraint_name=constraint_name,
            sqlite_columns=sqlite_columns,
        ):
            raise
        existing = (await session.execute(lookup)).scalar_one_or_none()
        if existing is None:
            raise
        return existing, False


async def _required_audit(
    session: AsyncSession,
    *,
    admin: User,
    action: str,
    target_type: str,
    target_id: str,
    metadata: dict[str, object],
) -> None:
    try:
        await log_audit(
            session,
            user_id=admin.id,
            action=action,
            status=AuditStatus.SUCCESS,
            target_type=target_type,
            target_id=target_id,
            client_info=json.dumps(
                metadata,
                sort_keys=True,
                separators=(",", ":"),
            ),
            required=True,
            commit=False,
        )
    except Exception as exc:
        raise EnterpriseAccessAuditError from exc


async def apply_enterprise_access(
    session: AsyncSession,
    *,
    agent_id: str,
    admin: User,
    selection: EnterpriseAccessSelection,
    preview_hash: str,
) -> EnterpriseAccessPreview:
    prepared = await _prepare_enterprise_access(
        session,
        agent_id=agent_id,
        selection=selection,
        for_update=True,
    )
    if prepared.preview.preview_hash != preview_hash:
        raise EnterpriseAccessPreviewStaleError(prepared.preview)

    creates: list[EnterpriseAccessImpact] = []
    reuses: list[EnterpriseAccessImpact] = []
    global_changes: list[EnterpriseAccessImpact] = []
    created_space_binding_ids: list[str] = []

    for space in prepared.spaces:
        space_binding, created = await _insert_or_reuse(
            session,
            EnterpriseIdentitySourceSpaceBinding(
                identity_source_id=prepared.source.id,
                knowledge_space_id=space.knowledge_space_id,
            ),
            lookup=select(EnterpriseIdentitySourceSpaceBinding).where(
                EnterpriseIdentitySourceSpaceBinding.identity_source_id
                == prepared.source.id,
                EnterpriseIdentitySourceSpaceBinding.knowledge_space_id
                == space.knowledge_space_id,
            ),
            constraint_name="uq_identity_source_space_binding",
            sqlite_columns=(
                "enterprise_identity_source_space_bindings.identity_source_id, "
                "enterprise_identity_source_space_bindings.knowledge_space_id"
            ),
        )
        impact = EnterpriseAccessImpact(
            relation_type="identity_source_space_binding",
            relation_id=space_binding.id,
            display_name=space.name,
            scope="global",
        )
        if created:
            global_changes.append(impact)
            created_space_binding_ids.append(space_binding.id)
            await _required_audit(
                session,
                admin=admin,
                action="identity_source.space_bind",
                target_type="enterprise_identity_source_space_binding",
                target_id=space_binding.id,
                metadata={
                    "agent_id": agent_id,
                    "identity_source_id": prepared.source.id,
                    "knowledge_space_id": space.knowledge_space_id,
                },
            )
        else:
            reuses.append(impact)

    group_candidates: list[
        tuple[AgentGroupAssignment, str, str]
    ] = []
    if prepared.preview.selection.all_synced_users:
        group_candidates.append(
            (
                AgentGroupAssignment.for_identity_source_all_users(
                    agent_id=agent_id,
                    identity_source_id=prepared.source.id,
                    created_by_user_id=admin.id,
                ),
                "agent_identity_source_all_users",
                "All synchronized users",
            )
        )
    group_candidates.extend(
        (
            AgentGroupAssignment.for_identity_source_group(
                agent_id=agent_id,
                identity_source_id=prepared.source.id,
                external_group_id=group.external_group_id,
                created_by_user_id=admin.id,
            ),
            "agent_identity_source_group",
            group.display_name,
        )
        for group in prepared.groups
    )
    for candidate, relation_type, display_name in group_candidates:
        group_assignment, created = await _insert_or_reuse(
            session,
            candidate,
            lookup=select(AgentGroupAssignment).where(
                AgentGroupAssignment.agent_id == agent_id,
                AgentGroupAssignment.group_key == candidate.group_key,
            ),
            constraint_name="uq_agent_group_assignment",
            sqlite_columns=(
                "agent_group_assignments.agent_id, "
                "agent_group_assignments.group_key"
            ),
        )
        impact = EnterpriseAccessImpact(
            relation_type=relation_type,
            relation_id=group_assignment.id,
            display_name=display_name,
            scope="agent",
        )
        (creates if created else reuses).append(impact)

    for user in prepared.users:
        user_assignment = (
            await session.execute(
                select(AgentUserAssignment).where(
                    AgentUserAssignment.agent_id == agent_id,
                    AgentUserAssignment.user_id == user.id,
                )
            )
        ).scalar_one_or_none()
        created = False
        if user_assignment is None:
            inserted_assignment, created = await _insert_or_reuse(
                session,
                AgentUserAssignment(
                    agent_id=agent_id,
                    user_id=user.id,
                    created_by_user_id=admin.id,
                    is_direct=True,
                ),
                lookup=select(AgentUserAssignment).where(
                    AgentUserAssignment.agent_id == agent_id,
                    AgentUserAssignment.user_id == user.id,
                ),
                constraint_name="uq_agent_user_assignment",
                sqlite_columns=(
                    "agent_user_assignments.agent_id, "
                    "agent_user_assignments.user_id"
                ),
            )
            user_assignment = inserted_assignment
        if not user_assignment.is_direct:
            user_assignment.is_direct = True
            user_assignment.created_by_user_id = admin.id
            await session.flush()
            created = True
        impact = EnterpriseAccessImpact(
            relation_type="agent_user",
            relation_id=user_assignment.id,
            display_name=user.display_name,
            scope="agent",
        )
        (creates if created else reuses).append(impact)

    await _required_audit(
        session,
        admin=admin,
        action="agent_enterprise_access.configure",
        target_type="agent",
        target_id=agent_id,
        metadata={
            "identity_source_id": prepared.source.id,
            "all_synced_users": prepared.preview.selection.all_synced_users,
            "agent_creates": len(creates),
            "global_creates": len(global_changes),
            "reuses": len(reuses),
            "created_space_binding_ids": created_space_binding_ids,
        },
    )
    await session.flush()
    return EnterpriseAccessPreview(
        selection=prepared.preview.selection,
        creates=tuple(creates),
        reuses=tuple(reuses),
        global_changes=tuple(global_changes),
        preview_hash=prepared.preview.preview_hash,
    )
