from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
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
from server.enterprise_identity.service import identity_provider_key


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


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
    current = datetime.now(UTC)
    source_rows = list(
        (
            await session.execute(
                select(EnterpriseIdentitySource).where(
                    EnterpriseIdentitySource.status
                    == EnterpriseIdentitySourceStatus.ACTIVE
                )
            )
        ).scalars()
    )
    result: set[str] = set()
    for source in source_rows:
        if (
            source.last_synced_at is not None
            and _as_utc(source.last_synced_at)
            < current - timedelta(seconds=source.stale_after_seconds)
        ):
            continue
        external_user_id = (
            await session.execute(
                select(UserExternalIdentity.external_subject)
                .join(
                    EnterpriseDirectoryUser,
                    EnterpriseDirectoryUser.external_user_id
                    == UserExternalIdentity.external_subject,
                )
                .where(
                    UserExternalIdentity.user_id == user_id,
                    UserExternalIdentity.identity_provider
                    == identity_provider_key(source),
                    EnterpriseDirectoryUser.identity_source_id == source.id,
                    EnterpriseDirectoryUser.status
                    == EnterpriseDirectoryEntryStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()
        if external_user_id is None:
            continue
        result.update(
            (
                await session.execute(
                    select(AgentGroupAssignment.agent_id).where(
                        AgentGroupAssignment.group_kind
                        == AgentGroupKind.IDENTITY_SOURCE_ALL,
                        AgentGroupAssignment.identity_source_id == source.id,
                    )
                )
            ).scalars()
        )
        frontier = {external_user_id}
        member_type = EnterpriseDirectoryMembershipType.USER
        visited: set[str] = set()
        for _ in range(8):
            if not frontier:
                break
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
            result.update(
                (
                    await session.execute(
                        select(AgentGroupAssignment.agent_id).where(
                            AgentGroupAssignment.group_kind
                            == AgentGroupKind.IDENTITY_SOURCE,
                            AgentGroupAssignment.identity_source_id == source.id,
                            AgentGroupAssignment.principal_id.in_(group_ids),
                        )
                    )
                ).scalars()
            )
            visited.update(group_ids)
            frontier = group_ids
            member_type = EnterpriseDirectoryMembershipType.GROUP
    return result


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
