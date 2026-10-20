from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.knowledge_subjects import batch_user_knowledge_subjects
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentGroupKind,
    AgentUserAssignment,
    AuthProvider,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    UserExternalIdentity,
    UserDepartment,
    User,
    UserStatus,
)
from server.models.base import utc_now
from server.enterprise_identity.service import (
    identity_provider_key,
    identity_source_snapshot_is_usable,
)
from server.enterprise_identity.principals import resolve_external_user_principals


def _builtin_user_agent_ids(user_id: str):
    return select(Agent.id).where(
        Agent.all_builtin_users.is_(True),
        select(User.id).where(
            User.id == user_id,
            User.auth_provider == AuthProvider.BUILTIN,
            User.status == UserStatus.ACTIVE,
        ).exists(),
    )


def _department_agent_ids(user_id: str):
    return (
        select(AgentGroupAssignment.agent_id)
        .join(
            UserDepartment,
            UserDepartment.department_id == AgentGroupAssignment.department_id,
        )
        .where(
            UserDepartment.user_id == user_id,
            AgentGroupAssignment.group_kind == AgentGroupKind.DEPARTMENT,
        )
    )


def _enterprise_group_agent_ids(user_id: str):
    return (
        select(AgentGroupAssignment.agent_id)
        .join(
            EnterprisePrincipalAssignment,
            (
                EnterprisePrincipalAssignment.identity_domain
                == AgentGroupAssignment.identity_domain
            )
            & (
                EnterprisePrincipalAssignment.provider
                == AgentGroupAssignment.provider
            )
            & (
                EnterprisePrincipalAssignment.principal_id
                == AgentGroupAssignment.principal_id
            ),
        )
        .where(
            EnterprisePrincipalAssignment.pas_user_id == user_id,
            EnterprisePrincipalAssignment.principal_type
            == EnterprisePrincipalType.GROUP,
            EnterprisePrincipalAssignment.status
            == EnterprisePrincipalStatus.ACTIVE,
            or_(
                EnterprisePrincipalAssignment.valid_until.is_(None),
                EnterprisePrincipalAssignment.valid_until > utc_now(),
            ),
            AgentGroupAssignment.group_kind == AgentGroupKind.ENTERPRISE,
        )
    )


async def _identity_source_group_agent_ids(
    session: AsyncSession,
    user_id: str,
) -> set[str]:
    return {
        agent_id
        for _assignment_id, agent_id in await _identity_source_group_assignments(
            session,
            user_id,
        )
    }


async def _identity_source_group_assignments(
    session: AsyncSession,
    user_id: str,
    *,
    agent_id: str | None = None,
) -> set[tuple[str, str]]:
    source_rows = list(
        (
            await session.execute(
                select(EnterpriseIdentitySource).where(
                    or_(
                        EnterpriseIdentitySource.status
                        == EnterpriseIdentitySourceStatus.ACTIVE,
                        and_(
                            EnterpriseIdentitySource.status
                            == EnterpriseIdentitySourceStatus.STALE,
                            EnterpriseIdentitySource.last_synced_at.is_not(None),
                        ),
                    ),
                )
            )
        ).scalars()
    )
    result: set[tuple[str, str]] = set()
    for source in source_rows:
        if not identity_source_snapshot_is_usable(source):
            continue
        external_user_ids = set(
            (
                await session.execute(
                    select(UserExternalIdentity.external_subject).where(
                        UserExternalIdentity.user_id == user_id,
                        UserExternalIdentity.identity_provider == identity_provider_key(source),
                    )
                )
            ).scalars()
        )
        if not external_user_ids:
            continue
        active_directory_ids = set(
            (
                await session.execute(
                    select(EnterpriseDirectoryUser.external_user_id).where(
                        EnterpriseDirectoryUser.identity_source_id == source.id,
                        EnterpriseDirectoryUser.external_user_id.in_(external_user_ids),
                        EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                    )
                )
            ).scalars()
        )
        local_principals_by_user = {
            external_user_id: await resolve_external_user_principals(session, source.id, external_user_id)
            for external_user_id in external_user_ids
        }
        matched_ids = active_directory_ids | {
            external_user_id for external_user_id, principals in local_principals_by_user.items() if principals
        }
        if not matched_ids:
            continue
        all_users_query = select(
            AgentGroupAssignment.id,
            AgentGroupAssignment.agent_id,
        ).where(
            AgentGroupAssignment.group_kind
            == AgentGroupKind.IDENTITY_SOURCE_ALL,
            AgentGroupAssignment.identity_source_id == source.id,
        )
        if agent_id is not None:
            all_users_query = all_users_query.where(
                AgentGroupAssignment.agent_id == agent_id
            )
        result.update((await session.execute(all_users_query)).tuples().all())
        local_group_ids = {
            principal_id
            for external_user_id in matched_ids
            for _principal_type, principal_id in local_principals_by_user[external_user_id]
        }
        if local_group_ids:
            local_group_query = select(
                AgentGroupAssignment.id,
                AgentGroupAssignment.agent_id,
            ).where(
                AgentGroupAssignment.group_kind
                == AgentGroupKind.IDENTITY_SOURCE,
                AgentGroupAssignment.identity_source_id == source.id,
                AgentGroupAssignment.principal_id.in_(local_group_ids),
            )
            if agent_id is not None:
                local_group_query = local_group_query.where(
                    AgentGroupAssignment.agent_id == agent_id
                )
            result.update((await session.execute(local_group_query)).tuples().all())
        frontier = matched_ids
        member_type = EnterpriseDirectoryMembershipType.USER
        visited: set[str] = set()
        while frontier:
            group_ids = set(
                (
                    await session.execute(
                        select(EnterpriseDirectoryGroup.external_group_id)
                        .join(
                            EnterpriseDirectoryMembership,
                            (
                                EnterpriseDirectoryMembership.identity_source_id
                                == EnterpriseDirectoryGroup.identity_source_id
                            )
                            & (
                                EnterpriseDirectoryMembership.external_group_id
                                == EnterpriseDirectoryGroup.external_group_id
                            ),
                        )
                        .where(
                            EnterpriseDirectoryGroup.identity_source_id == source.id,
                            EnterpriseDirectoryGroup.status
                            == EnterpriseDirectoryEntryStatus.ACTIVE,
                            EnterpriseDirectoryMembership.member_type
                            == member_type,
                            EnterpriseDirectoryMembership.external_member_id.in_(
                                frontier
                            ),
                        )
                    )
                ).scalars()
            ) - visited
            if not group_ids:
                break
            group_query = select(
                AgentGroupAssignment.id,
                AgentGroupAssignment.agent_id,
            ).where(
                AgentGroupAssignment.group_kind
                == AgentGroupKind.IDENTITY_SOURCE,
                AgentGroupAssignment.identity_source_id == source.id,
                AgentGroupAssignment.principal_id.in_(group_ids),
            )
            if agent_id is not None:
                group_query = group_query.where(
                    AgentGroupAssignment.agent_id == agent_id
                )
            result.update((await session.execute(group_query)).tuples().all())
            visited.update(group_ids)
            frontier = group_ids
            member_type = EnterpriseDirectoryMembershipType.GROUP
    return result


async def list_matching_agent_group_assignment_ids(
    session: AsyncSession,
    agent_id: str,
    user_id: str,
) -> set[str]:
    department_ids = set(
        (
            await session.execute(
                select(AgentGroupAssignment.id)
                .join(
                    UserDepartment,
                    UserDepartment.department_id
                    == AgentGroupAssignment.department_id,
                )
                .where(
                    AgentGroupAssignment.agent_id == agent_id,
                    AgentGroupAssignment.group_kind
                    == AgentGroupKind.DEPARTMENT,
                    UserDepartment.user_id == user_id,
                )
            )
        ).scalars()
    )
    enterprise_ids = set(
        (
            await session.execute(
                select(AgentGroupAssignment.id)
                .join(
                    EnterprisePrincipalAssignment,
                    (
                        EnterprisePrincipalAssignment.identity_domain
                        == AgentGroupAssignment.identity_domain
                    )
                    & (
                        EnterprisePrincipalAssignment.provider
                        == AgentGroupAssignment.provider
                    )
                    & (
                        EnterprisePrincipalAssignment.principal_id
                        == AgentGroupAssignment.principal_id
                    ),
                )
                .where(
                    AgentGroupAssignment.agent_id == agent_id,
                    AgentGroupAssignment.group_kind
                    == AgentGroupKind.ENTERPRISE,
                    EnterprisePrincipalAssignment.pas_user_id == user_id,
                    EnterprisePrincipalAssignment.principal_type
                    == EnterprisePrincipalType.GROUP,
                    EnterprisePrincipalAssignment.status
                    == EnterprisePrincipalStatus.ACTIVE,
                    or_(
                        EnterprisePrincipalAssignment.valid_until.is_(None),
                        EnterprisePrincipalAssignment.valid_until > utc_now(),
                    ),
                )
            )
        ).scalars()
    )
    identity_source_ids = {
        assignment_id
        for assignment_id, _agent_id in await _identity_source_group_assignments(
            session,
            user_id,
            agent_id=agent_id,
        )
    }
    return department_ids | enterprise_ids | identity_source_ids


async def list_accessible_agent_ids(
    session: AsyncSession,
    user_id: str,
) -> set[str]:
    direct = set(
        (
            await session.execute(
                select(AgentUserAssignment.agent_id).where(
                    AgentUserAssignment.user_id == user_id,
                    AgentUserAssignment.is_direct.is_(True),
                )
            )
        ).scalars()
    )
    departments = set(
        (await session.execute(_department_agent_ids(user_id))).scalars()
    )
    enterprise = set(
        (await session.execute(_enterprise_group_agent_ids(user_id))).scalars()
    )
    identity_source = await _identity_source_group_agent_ids(session, user_id)
    builtin = set((await session.execute(_builtin_user_agent_ids(user_id))).scalars())
    return direct | departments | enterprise | identity_source | builtin


async def has_group_agent_access(
    session: AsyncSession,
    agent_id: str,
    user_id: str,
) -> bool:
    if await session.scalar(_builtin_user_agent_ids(user_id).where(Agent.id == agent_id)):
        return True
    for query in (
        _department_agent_ids(user_id),
        _enterprise_group_agent_ids(user_id),
    ):
        match = (
            await session.execute(
                query.where(AgentGroupAssignment.agent_id == agent_id).limit(1)
            )
        ).first()
        if match is not None:
            return True
    return agent_id in await _identity_source_group_agent_ids(session, user_id)


async def has_agent_access(
    session: AsyncSession,
    agent_id: str,
    user_id: str,
) -> bool:
    return agent_id in await list_accessible_agent_ids(session, user_id)


async def list_agent_accessible_user_ids(
    session: AsyncSession,
    agent: Agent,
    user_ids: set[str],
) -> set[str]:
    """Resolve Agent access in batches instead of issuing queries per user."""
    if not user_ids:
        return set()

    accessible = set(
        (
            await session.execute(
                select(AgentUserAssignment.user_id).where(
                    AgentUserAssignment.agent_id == agent.id,
                    AgentUserAssignment.user_id.in_(user_ids),
                    AgentUserAssignment.is_direct.is_(True),
                )
            )
        ).scalars()
    )
    if agent.all_builtin_users:
        accessible.update(
            (
                await session.execute(
                    select(User.id).where(
                        User.id.in_(user_ids),
                        User.auth_provider == AuthProvider.BUILTIN,
                        User.status == UserStatus.ACTIVE,
                    )
                )
            ).scalars()
        )

    subject_keys: set[tuple[str, ...]] = set()
    for assignment in (
        await session.execute(
            select(AgentGroupAssignment).where(
                AgentGroupAssignment.agent_id == agent.id,
                AgentGroupAssignment.group_kind.in_(
                    {
                        AgentGroupKind.DEPARTMENT,
                        AgentGroupKind.IDENTITY_SOURCE,
                        AgentGroupKind.IDENTITY_SOURCE_ALL,
                    }
                ),
            )
        )
    ).scalars():
        if assignment.group_kind == AgentGroupKind.DEPARTMENT:
            if assignment.department_id:
                subject_keys.add(("DEPARTMENT", assignment.department_id))
        elif assignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL:
            if assignment.identity_source_id:
                subject_keys.add(("IDENTITY_SOURCE", assignment.identity_source_id))
        elif assignment.identity_source_id and assignment.principal_id:
            subject_keys.add(
                ("GROUP", assignment.identity_source_id, assignment.principal_id)
            )

    if subject_keys:
        subjects_by_user = await batch_user_knowledge_subjects(
            session, sorted(user_ids)
        )
        accessible.update(
            user_id
            for user_id, subjects in subjects_by_user.items()
            if subjects & subject_keys
        )

    accessible.update(
        (
            await session.execute(
                select(EnterprisePrincipalAssignment.pas_user_id)
                .join(
                    AgentGroupAssignment,
                    (
                        EnterprisePrincipalAssignment.identity_domain
                        == AgentGroupAssignment.identity_domain
                    )
                    & (
                        EnterprisePrincipalAssignment.provider
                        == AgentGroupAssignment.provider
                    )
                    & (
                        EnterprisePrincipalAssignment.principal_id
                        == AgentGroupAssignment.principal_id
                    ),
                )
                .where(
                    AgentGroupAssignment.agent_id == agent.id,
                    AgentGroupAssignment.group_kind == AgentGroupKind.ENTERPRISE,
                    EnterprisePrincipalAssignment.pas_user_id.in_(user_ids),
                    EnterprisePrincipalAssignment.principal_type
                    == EnterprisePrincipalType.GROUP,
                    EnterprisePrincipalAssignment.status
                    == EnterprisePrincipalStatus.ACTIVE,
                    or_(
                        EnterprisePrincipalAssignment.valid_until.is_(None),
                        EnterprisePrincipalAssignment.valid_until > utc_now(),
                    ),
                )
            )
        ).scalars()
    )
    return accessible
