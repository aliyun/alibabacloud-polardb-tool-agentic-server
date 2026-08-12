from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    AgentGroupAssignment,
    AgentGroupKind,
    AgentUserAssignment,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    UserDepartment,
)
from server.models.base import utc_now


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
    return direct | departments | enterprise


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
    return False


async def has_agent_access(
    session: AsyncSession,
    agent_id: str,
    user_id: str,
) -> bool:
    return agent_id in await list_accessible_agent_ids(session, user_id)
