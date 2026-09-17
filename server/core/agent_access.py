from __future__ import annotations

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    AgentGroupAssignment,
    AgentGroupKind,
    AgentUserAssignment,
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
)
from server.models.base import utc_now
from server.enterprise_identity.service import (
    identity_provider_key,
    identity_source_snapshot_is_usable,
)
from server.enterprise_identity.principals import resolve_external_user_principals


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
        external_user_id = (
            await session.execute(
                select(UserExternalIdentity.external_subject)
                .where(
                    UserExternalIdentity.user_id == user_id,
                    UserExternalIdentity.identity_provider
                    == identity_provider_key(source),
                )
            )
        ).scalar_one_or_none()
        if external_user_id is None:
            continue
        directory_user_exists = (
            await session.execute(
                select(EnterpriseDirectoryUser.id).where(
                    EnterpriseDirectoryUser.identity_source_id == source.id,
                    EnterpriseDirectoryUser.external_user_id == external_user_id,
                    EnterpriseDirectoryUser.status
                    == EnterpriseDirectoryEntryStatus.ACTIVE,
                )
            )
        ).first() is not None
        local_principals = await resolve_external_user_principals(
            session,
            source.id,
            external_user_id,
        )
        if not directory_user_exists and not local_principals:
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
        local_group_ids = {principal_id for _principal_type, principal_id in local_principals}
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
        frontier = {external_user_id}
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
    return direct | departments | enterprise | identity_source


async def has_group_agent_access(
    session: AsyncSession,
    agent_id: str,
    user_id: str,
) -> bool:
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
