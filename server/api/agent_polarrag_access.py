from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, delete, distinct, exists, func, literal, or_, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.auth.dependencies import require_admin
from server.core import agent_user_token_service
from server.core.agent_access import has_group_agent_access
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.enterprise_identity.service import identity_source_snapshot_is_usable
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentGroupKind,
    AgentKnowledgeScopeBinding,
    AgentUserAssignment,
    AuditStatus,
    Department,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    ExternalUserPrincipalMembership,
    User,
    UserDepartment,
)
from server.models.base import utc_now

router = APIRouter(prefix="/agents", tags=["agent-polarrag-access"])
logger = logging.getLogger(__name__)

USER_ASSIGNMENT_BULK_REQUEST_TIMEOUT_SECONDS = 2.0
_BULK_ASSIGNMENT_LEASE_SECONDS = 3600






class UserAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str


class UserOptionResponse(BaseModel):
    id: str
    display_name: str
    external_id: str
    status: str


class UserOptionListResponse(BaseModel):
    items: list[UserOptionResponse]
    total: int
    offset: int
    limit: int


class GroupAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_kind: AgentGroupKind
    department_id: str | None = None
    identity_domain: str | None = None
    provider: str | None = None
    identity_source_id: str | None = None
    principal_id: str | None = None






class GroupOptionResponse(BaseModel):
    group_kind: AgentGroupKind
    department_id: str | None
    department_name: str | None
    identity_domain: str | None
    provider: str | None
    identity_source_id: str | None
    identity_source_name: str | None
    external_group_id: str | None
    external_group_name: str | None
    principal_id: str | None
    member_count: int


class GroupOptionListResponse(BaseModel):
    items: list[GroupOptionResponse]
    total: int
    offset: int
    limit: int


class GroupAssignmentResponse(GroupOptionResponse):
    id: str
    created_at: datetime


class GroupAssignmentListResponse(BaseModel):
    items: list[GroupAssignmentResponse]
    total: int
    offset: int
    limit: int












class UserTokenSummaryResponse(BaseModel):
    token_prefix: str
    status: str
    last_used_at: datetime | None
    created_at: datetime


class UserAssignmentResponse(BaseModel):
    id: str
    user_id: str
    user_name: str
    user_status: str
    token: UserTokenSummaryResponse | None
    created_at: datetime


class UserAssignmentListResponse(BaseModel):
    items: list[UserAssignmentResponse]
    total: int
    offset: int
    limit: int


class BulkUserAssignmentResponse(BaseModel):
    status: Literal["running", "completed", "failed"]
    created_count: int = 0
    error: str | None = None


def _bulk_user_assignment_response(agent: Agent) -> BulkUserAssignmentResponse:
    return BulkUserAssignmentResponse(
        status=cast(
            Literal["running", "completed", "failed"],
            agent.bulk_assignment_status,
        ),
        created_count=agent.bulk_assignment_created_count,
        error=agent.bulk_assignment_error,
    )




def _assignment_response(row: AgentUserAssignment) -> UserAssignmentResponse:
    token = row.token
    return UserAssignmentResponse(
        id=row.id,
        user_id=row.user_id,
        user_name=row.user.display_name,
        user_status=row.user.status.value,
        token=(
            UserTokenSummaryResponse(
                token_prefix=token.token_prefix,
                status=agent_user_token_service.token_status(token),
                last_used_at=token.last_used_at,
                created_at=token.created_at,
            )
            if token is not None
            else None
        ),
        created_at=row.created_at,
    )


async def _require_agent(session: AsyncSession, agent_id: str) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _load_assignment(
    session: AsyncSession,
    assignment_id: str,
) -> AgentUserAssignment:
    assignment = (
        await session.execute(
            select(AgentUserAssignment)
            .options(
                selectinload(AgentUserAssignment.user),
                selectinload(AgentUserAssignment.token),
            )
            .where(AgentUserAssignment.id == assignment_id)
        )
    ).scalar_one_or_none()
    if assignment is None:
        raise HTTPException(status_code=404, detail="User assignment not found")
    return assignment


async def _audit(
    session: AsyncSession,
    admin: User,
    action: str,
    target_type: str,
    target_id: str,
    client_info: str | None = None,
) -> None:
    await log_audit(
        session,
        user_id=admin.id,
        action=action,
        status=AuditStatus.SUCCESS,
        user_name=admin.display_name,
        target_type=target_type,
        target_id=target_id,
        client_info=client_info,
        required=True,
        commit=False,
    )








async def _enterprise_group_exists(
    session: AsyncSession,
    identity_domain: str,
    provider: str,
    principal_id: str,
) -> bool:
    return (
        await session.execute(
            select(EnterprisePrincipalAssignment.id).where(
                EnterprisePrincipalAssignment.identity_domain == identity_domain,
                EnterprisePrincipalAssignment.provider == provider,
                EnterprisePrincipalAssignment.principal_type == EnterprisePrincipalType.GROUP,
                EnterprisePrincipalAssignment.principal_id == principal_id,
                EnterprisePrincipalAssignment.status == EnterprisePrincipalStatus.ACTIVE,
                or_(
                    EnterprisePrincipalAssignment.valid_until.is_(None),
                    EnterprisePrincipalAssignment.valid_until > utc_now(),
                ),
            )
        )
    ).first() is not None


async def _identity_source_group_exists(
    session: AsyncSession,
    identity_source_id: str,
    external_group_id: str,
) -> bool:
    source = await session.get(EnterpriseIdentitySource, identity_source_id)
    if source is None or not identity_source_snapshot_is_usable(source):
        return False
    directory_group_exists = (
        await session.execute(
            select(EnterpriseDirectoryGroup.id).where(
                EnterpriseDirectoryGroup.identity_source_id == identity_source_id,
                EnterpriseDirectoryGroup.external_group_id == external_group_id,
                EnterpriseDirectoryGroup.principal_type.in_(
                    (
                        EnterpriseDirectoryPrincipalType.GROUP,
                        EnterpriseDirectoryPrincipalType.DEPARTMENT,
                        EnterpriseDirectoryPrincipalType.ACL_GROUP,
                    )
                ),
                EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
            )
        )
    ).first() is not None
    if directory_group_exists:
        return True
    return (
        await session.execute(
            select(ExternalUserPrincipalMembership.id)
            .where(
                ExternalUserPrincipalMembership.identity_source_id
                == identity_source_id,
                ExternalUserPrincipalMembership.principal_id
                == external_group_id,
                or_(
                    ExternalUserPrincipalMembership.expires_at.is_(None),
                    ExternalUserPrincipalMembership.expires_at > utc_now(),
                ),
            )
            .limit(1)
        )
    ).first() is not None


async def _identity_source_exists(
    session: AsyncSession,
    identity_source_id: str,
) -> bool:
    source = await session.get(EnterpriseIdentitySource, identity_source_id)
    return source is not None and identity_source_snapshot_is_usable(source)


async def _group_member_count(
    session: AsyncSession,
    row: AgentGroupAssignment,
) -> int:
    if row.group_kind == AgentGroupKind.DEPARTMENT:
        return int(
            await session.scalar(
                select(func.count(UserDepartment.user_id)).where(UserDepartment.department_id == row.department_id)
            )
            or 0
        )
    if row.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL:
        members = union_all(
            select(
                EnterpriseDirectoryUser.external_user_id.label(
                    "external_user_id"
                )
            ).where(
                EnterpriseDirectoryUser.identity_source_id
                == row.identity_source_id,
                EnterpriseDirectoryUser.status
                == EnterpriseDirectoryEntryStatus.ACTIVE,
            ),
            select(
                ExternalUserPrincipalMembership.external_user_id.label(
                    "external_user_id"
                )
            ).where(
                ExternalUserPrincipalMembership.identity_source_id
                == row.identity_source_id,
                or_(
                    ExternalUserPrincipalMembership.expires_at.is_(None),
                    ExternalUserPrincipalMembership.expires_at > utc_now(),
                ),
            ),
        ).subquery()
        return int(
            await session.scalar(
                select(func.count(distinct(members.c.external_user_id)))
            )
            or 0
        )
    if row.group_kind == AgentGroupKind.IDENTITY_SOURCE:
        members = union_all(
            select(
                EnterpriseDirectoryUser.external_user_id.label(
                    "external_user_id"
                )
            )
            .join(
                EnterpriseDirectoryMembership,
                (
                    EnterpriseDirectoryMembership.identity_source_id
                    == EnterpriseDirectoryUser.identity_source_id
                )
                & (
                    EnterpriseDirectoryMembership.external_member_id
                    == EnterpriseDirectoryUser.external_user_id
                ),
            )
            .where(
                EnterpriseDirectoryUser.identity_source_id
                == row.identity_source_id,
                EnterpriseDirectoryUser.status
                == EnterpriseDirectoryEntryStatus.ACTIVE,
                EnterpriseDirectoryMembership.external_group_id
                == row.principal_id,
                EnterpriseDirectoryMembership.member_type
                == EnterpriseDirectoryMembershipType.USER,
            ),
            select(
                ExternalUserPrincipalMembership.external_user_id.label(
                    "external_user_id"
                )
            ).where(
                ExternalUserPrincipalMembership.identity_source_id
                == row.identity_source_id,
                ExternalUserPrincipalMembership.principal_id
                == row.principal_id,
                or_(
                    ExternalUserPrincipalMembership.expires_at.is_(None),
                    ExternalUserPrincipalMembership.expires_at > utc_now(),
                ),
            ),
        ).subquery()
        return int(
            await session.scalar(
                select(func.count(distinct(members.c.external_user_id)))
            )
            or 0
        )
    return int(
        await session.scalar(
            select(func.count(distinct(EnterprisePrincipalAssignment.pas_user_id))).where(
                EnterprisePrincipalAssignment.identity_domain == row.identity_domain,
                EnterprisePrincipalAssignment.provider == row.provider,
                EnterprisePrincipalAssignment.principal_type == EnterprisePrincipalType.GROUP,
                EnterprisePrincipalAssignment.principal_id == row.principal_id,
                EnterprisePrincipalAssignment.status == EnterprisePrincipalStatus.ACTIVE,
                or_(
                    EnterprisePrincipalAssignment.valid_until.is_(None),
                    EnterprisePrincipalAssignment.valid_until > utc_now(),
                ),
            )
        )
        or 0
    )


async def _group_assignment_response(
    session: AsyncSession,
    row: AgentGroupAssignment,
) -> GroupAssignmentResponse:
    identity_source_name = None
    external_group_name = None
    if row.group_kind in {
        AgentGroupKind.IDENTITY_SOURCE,
        AgentGroupKind.IDENTITY_SOURCE_ALL,
    }:
        source = await session.get(EnterpriseIdentitySource, row.identity_source_id)
        group = (
            await session.execute(
                select(EnterpriseDirectoryGroup).where(
                    EnterpriseDirectoryGroup.identity_source_id == row.identity_source_id,
                    EnterpriseDirectoryGroup.external_group_id == row.principal_id,
                )
            )
        ).scalar_one_or_none()
        identity_source_name = source.name if source is not None else None
        external_group_name = group.display_name if group is not None else None
    return GroupAssignmentResponse(
        id=row.id,
        group_kind=row.group_kind,
        department_id=row.department_id,
        department_name=(row.department.name if row.department else None),
        identity_domain=row.identity_domain,
        provider=row.provider,
        identity_source_id=row.identity_source_id,
        identity_source_name=identity_source_name,
        external_group_id=(row.principal_id if row.group_kind == AgentGroupKind.IDENTITY_SOURCE else None),
        external_group_name=external_group_name,
        principal_id=row.principal_id,
        member_count=await _group_member_count(session, row),
        created_at=row.created_at,
    )


def _group_option_columns(
    rank: int,
    kind: str,
    *,
    department_id: Any = None,
    department_name: Any = None,
    identity_domain: Any = None,
    provider: Any = None,
    identity_source_id: Any = None,
    identity_source_name: Any = None,
    external_group_id: Any = None,
    external_group_name: Any = None,
    principal_id: Any = None,
    member_count: Any = None,
    sort_1: Any = None,
    sort_2: Any = None,
    sort_3: Any = None,
) -> tuple[Any, ...]:
    def labeled(value: Any, name: str) -> Any:
        return (literal(None) if value is None else value).label(name)

    return (
        literal(rank).label("kind_rank"),
        literal(kind).label("group_kind"),
        labeled(department_id, "department_id"),
        labeled(department_name, "department_name"),
        labeled(identity_domain, "identity_domain"),
        labeled(provider, "provider"),
        labeled(identity_source_id, "identity_source_id"),
        labeled(identity_source_name, "identity_source_name"),
        labeled(external_group_id, "external_group_id"),
        labeled(external_group_name, "external_group_name"),
        labeled(principal_id, "principal_id"),
        labeled(member_count, "member_count"),
        labeled(sort_1, "sort_1"),
        labeled(sort_2, "sort_2"),
        labeled(sort_3, "sort_3"),
    )


def _group_options_query(agent_id: str, normalized_search: str) -> Any:
    pattern = f"%{normalized_search}%"
    usable_source = or_(
        EnterpriseIdentitySource.status == EnterpriseIdentitySourceStatus.ACTIVE,
        and_(
            EnterpriseIdentitySource.status == EnterpriseIdentitySourceStatus.STALE,
            EnterpriseIdentitySource.last_synced_at.is_not(None),
        ),
    )
    department_assigned = exists(
        select(AgentGroupAssignment.id).where(
            AgentGroupAssignment.agent_id == agent_id,
            AgentGroupAssignment.group_kind == AgentGroupKind.DEPARTMENT,
            AgentGroupAssignment.department_id == Department.id,
        )
    )
    source_all_assigned = exists(
        select(AgentGroupAssignment.id).where(
            AgentGroupAssignment.agent_id == agent_id,
            AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL,
            AgentGroupAssignment.identity_source_id == EnterpriseIdentitySource.id,
        )
    )
    enterprise_assigned = exists(
        select(AgentGroupAssignment.id).where(
            AgentGroupAssignment.agent_id == agent_id,
            AgentGroupAssignment.group_kind == AgentGroupKind.ENTERPRISE,
            AgentGroupAssignment.identity_domain
            == EnterprisePrincipalAssignment.identity_domain,
            AgentGroupAssignment.provider == EnterprisePrincipalAssignment.provider,
            AgentGroupAssignment.principal_id
            == EnterprisePrincipalAssignment.principal_id,
        )
    )
    source_group_assigned = exists(
        select(AgentGroupAssignment.id).where(
            AgentGroupAssignment.agent_id == agent_id,
            AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE,
            AgentGroupAssignment.identity_source_id == EnterpriseIdentitySource.id,
            AgentGroupAssignment.principal_id
            == EnterpriseDirectoryGroup.external_group_id,
        )
    )
    department_query = (
        select(
            *_group_option_columns(
                0,
                AgentGroupKind.DEPARTMENT.value,
                department_id=Department.id,
                department_name=Department.name,
                member_count=func.count(UserDepartment.user_id),
                sort_1=Department.name,
                sort_2=Department.id,
            )
        )
        .outerjoin(UserDepartment, UserDepartment.department_id == Department.id)
        .where(~department_assigned)
        .group_by(Department.id, Department.name)
    )
    source_all_query = (
        select(
            *_group_option_columns(
                1,
                AgentGroupKind.IDENTITY_SOURCE_ALL.value,
                identity_source_id=EnterpriseIdentitySource.id,
                identity_source_name=EnterpriseIdentitySource.name,
                member_count=func.count(EnterpriseDirectoryUser.id),
                sort_1=EnterpriseIdentitySource.name,
                sort_2=EnterpriseIdentitySource.id,
            )
        )
        .outerjoin(
            EnterpriseDirectoryUser,
            (
                EnterpriseDirectoryUser.identity_source_id
                == EnterpriseIdentitySource.id
            )
            & (
                EnterpriseDirectoryUser.status
                == EnterpriseDirectoryEntryStatus.ACTIVE
            ),
        )
        .where(usable_source, ~source_all_assigned)
        .group_by(EnterpriseIdentitySource.id, EnterpriseIdentitySource.name)
    )
    enterprise_query = (
        select(
            *_group_option_columns(
                2,
                AgentGroupKind.ENTERPRISE.value,
                identity_domain=EnterprisePrincipalAssignment.identity_domain,
                provider=EnterprisePrincipalAssignment.provider,
                principal_id=EnterprisePrincipalAssignment.principal_id,
                member_count=func.count(
                    distinct(EnterprisePrincipalAssignment.pas_user_id)
                ),
                sort_1=EnterprisePrincipalAssignment.identity_domain,
                sort_2=EnterprisePrincipalAssignment.provider,
                sort_3=EnterprisePrincipalAssignment.principal_id,
            )
        )
        .where(
            EnterprisePrincipalAssignment.principal_type
            == EnterprisePrincipalType.GROUP,
            EnterprisePrincipalAssignment.status
            == EnterprisePrincipalStatus.ACTIVE,
            or_(
                EnterprisePrincipalAssignment.valid_until.is_(None),
                EnterprisePrincipalAssignment.valid_until > utc_now(),
            ),
            ~enterprise_assigned,
        )
        .group_by(
            EnterprisePrincipalAssignment.identity_domain,
            EnterprisePrincipalAssignment.provider,
            EnterprisePrincipalAssignment.principal_id,
        )
    )
    source_group_query = (
        select(
            *_group_option_columns(
                3,
                AgentGroupKind.IDENTITY_SOURCE.value,
                identity_source_id=EnterpriseIdentitySource.id,
                identity_source_name=EnterpriseIdentitySource.name,
                external_group_id=EnterpriseDirectoryGroup.external_group_id,
                external_group_name=EnterpriseDirectoryGroup.display_name,
                principal_id=EnterpriseDirectoryGroup.external_group_id,
                member_count=func.count(distinct(EnterpriseDirectoryUser.id)),
                sort_1=EnterpriseIdentitySource.name,
                sort_2=EnterpriseDirectoryGroup.display_name,
                sort_3=EnterpriseDirectoryGroup.external_group_id,
            )
        )
        .join(
            EnterpriseDirectoryGroup,
            EnterpriseDirectoryGroup.identity_source_id
            == EnterpriseIdentitySource.id,
        )
        .outerjoin(
            EnterpriseDirectoryMembership,
            (
                EnterpriseDirectoryMembership.identity_source_id
                == EnterpriseDirectoryGroup.identity_source_id
            )
            & (
                EnterpriseDirectoryMembership.external_group_id
                == EnterpriseDirectoryGroup.external_group_id
            )
            & (
                EnterpriseDirectoryMembership.member_type
                == EnterpriseDirectoryMembershipType.USER
            ),
        )
        .outerjoin(
            EnterpriseDirectoryUser,
            (
                EnterpriseDirectoryUser.identity_source_id
                == EnterpriseDirectoryMembership.identity_source_id
            )
            & (
                EnterpriseDirectoryUser.external_user_id
                == EnterpriseDirectoryMembership.external_member_id
            )
            & (
                EnterpriseDirectoryUser.status
                == EnterpriseDirectoryEntryStatus.ACTIVE
            ),
        )
        .where(
            usable_source,
            EnterpriseDirectoryGroup.status
            == EnterpriseDirectoryEntryStatus.ACTIVE,
            EnterpriseDirectoryGroup.principal_type.in_(
                (
                    EnterpriseDirectoryPrincipalType.GROUP,
                    EnterpriseDirectoryPrincipalType.DEPARTMENT,
                )
            ),
            ~source_group_assigned,
        )
        .group_by(
            EnterpriseIdentitySource.id,
            EnterpriseIdentitySource.name,
            EnterpriseDirectoryGroup.external_group_id,
            EnterpriseDirectoryGroup.display_name,
        )
    )
    if normalized_search:
        department_query = department_query.where(Department.name.ilike(pattern))
        source_all_query = source_all_query.where(
            EnterpriseIdentitySource.name.ilike(pattern)
        )
        enterprise_query = enterprise_query.where(
            or_(
                EnterprisePrincipalAssignment.identity_domain.ilike(pattern),
                EnterprisePrincipalAssignment.provider.ilike(pattern),
                EnterprisePrincipalAssignment.principal_id.ilike(pattern),
            )
        )
        source_group_query = source_group_query.where(
            or_(
                EnterpriseIdentitySource.name.ilike(pattern),
                EnterpriseDirectoryGroup.display_name.ilike(pattern),
                EnterpriseDirectoryGroup.external_group_id.ilike(pattern),
            )
        )
    return union_all(
        department_query,
        source_all_query,
        enterprise_query,
        source_group_query,
    ).subquery()


@router.get(
    "/{agent_id}/group-options",
    response_model=GroupOptionListResponse,
)
async def list_group_options(
    agent_id: str,
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    normalized_search = (search or "").strip()
    options_query = _group_options_query(agent_id, normalized_search)
    total = int(await session.scalar(select(func.count()).select_from(options_query)) or 0)
    rows = (
        await session.execute(
            select(options_query)
            .order_by(
                options_query.c.kind_rank,
                options_query.c.sort_1,
                options_query.c.sort_2,
                options_query.c.sort_3,
            )
            .offset(offset)
            .limit(limit)
        )
    ).mappings().all()
    return GroupOptionListResponse(
        items=[
            GroupOptionResponse(
                **{
                    key: row[key]
                    for key in GroupOptionResponse.model_fields
                }
            )
            for row in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{agent_id}/group-assignments",
    response_model=GroupAssignmentListResponse,
)
async def list_group_assignments(
    agent_id: str,
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    filters = [AgentGroupAssignment.agent_id == agent_id]
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                Department.name.ilike(pattern),
                EnterpriseIdentitySource.name.ilike(pattern),
                EnterpriseDirectoryGroup.display_name.ilike(pattern),
                AgentGroupAssignment.identity_domain.ilike(pattern),
                AgentGroupAssignment.provider.ilike(pattern),
                AgentGroupAssignment.principal_id.ilike(pattern),
            )
        )
    group_join = (EnterpriseDirectoryGroup.identity_source_id == AgentGroupAssignment.identity_source_id) & (
        EnterpriseDirectoryGroup.external_group_id == AgentGroupAssignment.principal_id
    )
    query = (
        select(AgentGroupAssignment)
        .outerjoin(
            Department,
            Department.id == AgentGroupAssignment.department_id,
        )
        .outerjoin(
            EnterpriseIdentitySource,
            EnterpriseIdentitySource.id == AgentGroupAssignment.identity_source_id,
        )
        .outerjoin(EnterpriseDirectoryGroup, group_join)
        .where(*filters)
    )
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = list(
        (
            await session.execute(
                query.options(selectinload(AgentGroupAssignment.department))
                .order_by(
                    AgentGroupAssignment.group_kind,
                    AgentGroupAssignment.group_key,
                )
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )
    return GroupAssignmentListResponse(
        items=[await _group_assignment_response(session, row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "/{agent_id}/group-assignments",
    response_model=GroupAssignmentResponse,
    status_code=201,
)
async def create_group_assignment(
    agent_id: str,
    body: GroupAssignmentRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    if body.group_kind == AgentGroupKind.DEPARTMENT:
        if (
            body.department_id is None
            or body.identity_domain is not None
            or body.provider is not None
            or body.principal_id is not None
        ):
            raise HTTPException(status_code=422, detail="Invalid department group")
        department = await session.get(Department, body.department_id)
        if department is None:
            raise HTTPException(status_code=404, detail="Department not found")
        row = AgentGroupAssignment.for_department(
            agent_id=agent_id,
            department_id=department.id,
            created_by_user_id=admin.id,
        )
        row.department = department
    elif body.group_kind == AgentGroupKind.ENTERPRISE:
        if (
            body.department_id is not None
            or body.identity_domain is None
            or body.provider is None
            or body.principal_id is None
        ):
            raise HTTPException(status_code=422, detail="Invalid enterprise group")
        try:
            row = AgentGroupAssignment.for_enterprise_group(
                agent_id=agent_id,
                identity_domain=body.identity_domain,
                provider=body.provider,
                principal_id=body.principal_id,
                created_by_user_id=admin.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not await _enterprise_group_exists(
            session,
            row.identity_domain or "",
            row.provider or "",
            row.principal_id or "",
        ):
            raise HTTPException(
                status_code=404,
                detail="Enterprise group is not registered",
            )
    elif body.group_kind == AgentGroupKind.IDENTITY_SOURCE:
        if (
            body.department_id is not None
            or body.identity_domain is not None
            or body.provider is not None
            or body.identity_source_id is None
            or body.principal_id is None
        ):
            raise HTTPException(
                status_code=422,
                detail="Invalid identity source group",
            )
        try:
            row = AgentGroupAssignment.for_identity_source_group(
                agent_id=agent_id,
                identity_source_id=body.identity_source_id,
                external_group_id=body.principal_id,
                created_by_user_id=admin.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not await _identity_source_group_exists(
            session,
            row.identity_source_id or "",
            row.principal_id or "",
        ):
            raise HTTPException(
                status_code=404,
                detail="Identity source group is not available",
            )
    else:
        if (
            body.department_id is not None
            or body.identity_domain is not None
            or body.provider is not None
            or body.identity_source_id is None
            or body.principal_id is not None
        ):
            raise HTTPException(
                status_code=422,
                detail="Invalid identity source all-users assignment",
            )
        try:
            row = AgentGroupAssignment.for_identity_source_all_users(
                agent_id=agent_id,
                identity_source_id=body.identity_source_id,
                created_by_user_id=admin.id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not await _identity_source_exists(
            session,
            row.identity_source_id or "",
        ):
            raise HTTPException(
                status_code=404,
                detail="Identity source is not available",
            )
    session.add(row)
    try:
        await session.flush()
        await _audit(
            session,
            admin,
            "agent_group_assignment.create",
            "agent_group_assignment",
            row.id,
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Group already assigned") from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc
    return await _group_assignment_response(session, row)


@router.delete(
    "/{agent_id}/group-assignments/{assignment_id}",
    status_code=204,
)
async def delete_group_assignment(
    agent_id: str,
    assignment_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(AgentGroupAssignment, assignment_id)
    if row is None or row.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="Group assignment not found")
    try:
        await _audit(
            session,
            admin,
            "agent_group_assignment.delete",
            "agent_group_assignment",
            row.id,
        )
        await session.execute(
            delete(AgentKnowledgeScopeBinding).where(
                AgentKnowledgeScopeBinding.group_assignment_id == row.id
            )
        )
        await session.delete(row)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc














@router.get(
    "/{agent_id}/user-assignments",
    response_model=UserAssignmentListResponse,
)
async def list_user_assignments(
    agent_id: str,
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    filters = [
        AgentUserAssignment.agent_id == agent_id,
        AgentUserAssignment.is_direct.is_(True),
    ]
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                User.display_name.ilike(pattern),
                User.email.ilike(pattern),
                User.external_id.ilike(pattern),
            )
        )
    base_query = (
        select(AgentUserAssignment)
        .join(
            User,
            User.id == AgentUserAssignment.user_id,
        )
        .where(*filters)
    )
    total = int(await session.scalar(select(func.count()).select_from(base_query.subquery())) or 0)
    rows = (
        await session.execute(
            base_query.options(
                selectinload(AgentUserAssignment.user),
                selectinload(AgentUserAssignment.token),
            )
            .order_by(AgentUserAssignment.id)
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return UserAssignmentListResponse(
        items=[_assignment_response(row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{agent_id}/user-options",
    response_model=UserOptionListResponse,
)
async def list_user_options(
    agent_id: str,
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    direct_assignment_exists = (
        select(AgentUserAssignment.id)
        .where(
            AgentUserAssignment.agent_id == agent_id,
            AgentUserAssignment.user_id == User.id,
            AgentUserAssignment.is_direct.is_(True),
        )
        .exists()
    )
    filters = [~direct_assignment_exists]
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        filters.append(
            or_(
                User.display_name.ilike(pattern),
                User.email.ilike(pattern),
                User.external_id.ilike(pattern),
            )
        )
    total = int(await session.scalar(select(func.count()).select_from(User).where(*filters)) or 0)
    rows = (
        await session.execute(
            select(User.id, User.display_name, User.external_id, User.status)
            .where(*filters)
            .order_by(User.created_at.desc(), User.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return UserOptionListResponse(
        items=[
            UserOptionResponse(
                id=row.id,
                display_name=row.display_name,
                external_id=row.external_id,
                status=row.status.value,
            )
            for row in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


async def _assign_all_users_in_background(
    session_factory: Any,
    agent_id: str,
    admin_id: str,
    worker_id: str,
) -> None:
    try:
        async with session_factory() as session:
            agent = await session.get(Agent, agent_id)
            admin = await session.get(User, admin_id)
            if agent is None or admin is None:
                failed = await session.execute(
                    update(Agent)
                    .where(
                        Agent.id == agent_id,
                        Agent.bulk_assignment_worker_id == worker_id,
                    )
                    .values(
                        bulk_assignment_status="failed",
                        bulk_assignment_error="Agent or administrator not found",
                        bulk_assignment_worker_id=None,
                        bulk_assignment_lease_until=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if failed.rowcount == 1:  # type: ignore[attr-defined]
                    await session.commit()
                else:
                    await session.rollback()
                return
            existing = {
                row.user_id: row
                for row in (
                    await session.execute(select(AgentUserAssignment).where(AgentUserAssignment.agent_id == agent_id))
                ).scalars()
            }
            user_ids = list((await session.execute(select(User.id))).scalars())
            created_count = 0
            for user_id in user_ids:
                assignment = existing.get(user_id)
                if assignment is None:
                    session.add(
                        AgentUserAssignment(
                            agent_id=agent_id,
                            user_id=user_id,
                            created_by_user_id=admin_id,
                            is_direct=True,
                        )
                    )
                    created_count += 1
                elif not assignment.is_direct:
                    assignment.is_direct = True
                    assignment.created_by_user_id = admin_id
                    created_count += 1
            if created_count:
                await session.flush()
                await _audit(
                    session,
                    admin,
                    "agent_user_assignment.bulk_create",
                    "agent",
                    agent_id,
                    client_info=json.dumps({"created_count": created_count}),
                )
            completed = await session.execute(
                update(Agent)
                .where(
                    Agent.id == agent_id,
                    Agent.bulk_assignment_worker_id == worker_id,
                )
                .values(
                    bulk_assignment_created_count=created_count,
                    bulk_assignment_status="completed",
                    bulk_assignment_error=None,
                    bulk_assignment_worker_id=None,
                    bulk_assignment_lease_until=None,
                )
                .execution_options(synchronize_session=False)
            )
            if completed.rowcount != 1:  # type: ignore[attr-defined]
                await session.rollback()
                return
            await session.commit()
    except Exception:
        logger.exception(
            "agent_user_assignment.bulk_create_failed",
            extra={"agent_id": agent_id},
        )
        async with session_factory() as session:
            await session.execute(
                update(Agent)
                .where(
                    Agent.id == agent_id,
                    Agent.bulk_assignment_worker_id == worker_id,
                )
                .values(
                    bulk_assignment_status="failed",
                    bulk_assignment_error="Bulk user assignment failed",
                    bulk_assignment_worker_id=None,
                    bulk_assignment_lease_until=None,
                )
            )
            await session.commit()


async def _schedule_bulk_user_assignment(
    session_factory: Any,
    session: AsyncSession,
    agent_id: str,
    admin_id: str,
    *,
    background_tasks: set[asyncio.Task[None]] | None = None,
) -> asyncio.Task[None] | None:
    worker_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    claimed = await session.execute(
        update(Agent)
        .where(
            Agent.id == agent_id,
            or_(
                Agent.bulk_assignment_worker_id.is_(None),
                Agent.bulk_assignment_lease_until.is_(None),
                Agent.bulk_assignment_lease_until <= now,
            ),
        )
        .values(
            bulk_assignment_status="running",
            bulk_assignment_created_count=0,
            bulk_assignment_error=None,
            bulk_assignment_worker_id=worker_id,
            bulk_assignment_lease_until=now
            + timedelta(seconds=_BULK_ASSIGNMENT_LEASE_SECONDS),
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return None
    await session.commit()
    task = asyncio.create_task(
        _assign_all_users_in_background(
            session_factory, agent_id, admin_id, worker_id
        )
    )
    if background_tasks is not None:
        background_tasks.add(task)

    def finished(completed: asyncio.Task[None]) -> None:
        if background_tasks is not None:
            background_tasks.discard(completed)

    task.add_done_callback(finished)
    return task


@router.post(
    "/{agent_id}/user-assignments/bulk",
    response_model=BulkUserAssignmentResponse,
)
async def create_all_user_assignments(
    agent_id: str,
    request: Request,
    response: Response,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        from server.db.engine import get_session_factory

        session_factory = get_session_factory()
    task = await _schedule_bulk_user_assignment(
        session_factory,
        session,
        agent_id,
        admin.id,
        background_tasks=getattr(request.app.state, "background_tasks", None),
    )
    if task is None:
        agent = await _require_agent(session, agent_id)
        response.status_code = 202
        return _bulk_user_assignment_response(agent)
    try:
        await asyncio.wait_for(
            asyncio.shield(task),
            timeout=USER_ASSIGNMENT_BULK_REQUEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        response.status_code = 202
    agent = await _require_agent(session, agent_id)
    await session.refresh(agent)
    return _bulk_user_assignment_response(agent)


@router.get(
    "/{agent_id}/user-assignments/bulk/status",
    response_model=BulkUserAssignmentResponse,
)
async def get_all_user_assignment_status(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _require_agent(session, agent_id)
    return _bulk_user_assignment_response(agent)


@router.post(
    "/{agent_id}/user-assignments",
    response_model=UserAssignmentResponse,
    status_code=201,
)
async def create_user_assignment(
    agent_id: str,
    body: UserAssignmentRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _require_agent(session, agent_id)
    user = await session.get(User, body.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    row = (
        await session.execute(
            select(AgentUserAssignment).where(
                AgentUserAssignment.agent_id == agent.id,
                AgentUserAssignment.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if row is not None and row.is_direct:
        raise HTTPException(status_code=409, detail="User already assigned")
    if row is None:
        row = AgentUserAssignment(
            agent_id=agent.id,
            user_id=user.id,
            created_by_user_id=admin.id,
            is_direct=True,
        )
        session.add(row)
    else:
        row.is_direct = True
        row.created_by_user_id = admin.id
    row.agent = agent
    row.user = user
    try:
        await session.flush()
        await _audit(session, admin, "agent_user_assignment.create", "agent_user_assignment", row.id)
        await session.commit()
        row = await _load_assignment(session, row.id)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="User already assigned") from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc
    return _assignment_response(row)


@router.delete("/{agent_id}/user-assignments/{assignment_id}", status_code=204)
async def delete_user_assignment(
    agent_id: str,
    assignment_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(AgentUserAssignment, assignment_id)
    if row is None or row.agent_id != agent_id or not row.is_direct:
        raise HTTPException(status_code=404, detail="User assignment not found")
    try:
        await _audit(session, admin, "agent_user_assignment.delete", "agent_user_assignment", row.id)
        row.is_direct = False
        await session.flush()
        if not await has_group_agent_access(
            session,
            row.agent_id,
            row.user_id,
        ):
            await session.delete(row)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc


@router.post(
    "/{agent_id}/user-assignments/{assignment_id}/token/revoke",
    response_model=UserAssignmentResponse,
)
async def force_revoke_user_token(
    agent_id: str,
    assignment_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    assignment = await session.get(AgentUserAssignment, assignment_id)
    if assignment is None or assignment.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="User assignment not found")
    try:
        row = await agent_user_token_service.revoke_token(session, assignment_id)
        await _audit(session, admin, "agent_user_token.force_revoke", "agent_user_token", row.id)
        await session.commit()
        assignment = await _load_assignment(session, assignment_id)
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Agent user token not found") from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Token revocation unavailable") from exc
    return _assignment_response(assignment)
