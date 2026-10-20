from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import String, and_, case, delete, distinct, exists, func, literal, or_, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.api.enterprise_user_identity import enterprise_identities_by_user, resolve_enterprise_user_ids
from server.auth.dependencies import require_admin
from server.core import agent_user_token_service
from server.core.agent_access import has_group_agent_access
from server.core.audit_logger import log_audit
from server.core.knowledge_subjects import knowledge_subject_members
from server.core.user_option_availability import user_option_availability
from server.db.engine import get_session
from server.enterprise_identity.service import identity_provider_key, identity_source_snapshot_is_usable
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentGroupKind,
    AgentKnowledgeScopeBinding,
    AgentUserAssignment,
    AgentPolarRAGInstanceBinding,
    AuthProvider,
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
    IdentitySourceProvider,
    EnterpriseIdentitySourceStatus,
    EnterpriseIdentitySourceSpaceBinding,
    ExternalUserPrincipalMembership,
    User,
    UserDepartment,
    UserExternalIdentity,
    UserStatus,
    PolarRAGSpace,
)
from server.models.base import utc_now

router = APIRouter(prefix="/agents", tags=["agent-polarrag-access"])
logger = logging.getLogger(__name__)

USER_ASSIGNMENT_BULK_REQUEST_TIMEOUT_SECONDS = 2.0
_BULK_ASSIGNMENT_LEASE_SECONDS = 3600


class UserAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str | SkipJsonSchema[None] = None
    identity_source_id: str | None = Field(default=None, min_length=1, max_length=36)
    external_user_id: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_selector(self) -> UserAssignmentRequest:
        if "user_id" in self.model_fields_set and self.user_id is None:
            raise ValueError("user_id must be a string")
        enterprise = self.identity_source_id is not None and self.external_user_id is not None
        if (self.identity_source_id is None) != (self.external_user_id is None) or (self.user_id is not None) == enterprise:
            raise ValueError("provide user_id or identity_source_id and external_user_id")
        return self


class AllSubjectAssignmentsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["BIND", "UNBIND"]


class AllSubjectAssignmentsResponse(BaseModel):
    status: Literal["BOUND", "PARTIAL", "UNBOUND"]
    eligible_source_count: int
    eligible_user_count: int
    assignment_count: int


class UserOptionResponse(BaseModel):
    enterprise_identities: list[dict[str, str]] = Field(default_factory=list)
    id: str
    display_name: str
    external_id: str
    status: str
    availability: Literal["active", "disabled", "unavailable"] = "active"
    identity_source_names: list[str] = Field(default_factory=list)
    assignment_id: str | None = None
    inherited_source_names: list[str] = Field(default_factory=list)


class UserOptionListResponse(BaseModel):
    items: list[UserOptionResponse]
    total: int
    offset: int
    limit: int


class EnterpriseUserOptionResponse(BaseModel):
    id: str
    display_name: str
    external_user_id: str
    email: str | None
    identity_source_id: str
    identity_source_name: str
    availability: Literal["active", "disabled", "unavailable"] = "active"


class EnterpriseUserOptionListResponse(BaseModel):
    items: list[EnterpriseUserOptionResponse]
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
    assignment_id: str | None = None
    inherited_source_names: list[str] = Field(default_factory=list)


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
    enterprise_identities: list[dict[str, str]] = Field(default_factory=list)
    id: str
    user_id: str
    user_name: str
    user_status: str
    token: UserTokenSummaryResponse | None
    created_at: datetime
    identity_source_names: list[str] = Field(default_factory=list)


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


def _assignment_response(
    row: AgentUserAssignment,
    identity_source_names: list[str] | None = None,
    enterprise_identities: list[dict[str, str]] | None = None,
) -> UserAssignmentResponse:
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
        identity_source_names=identity_source_names or [],
        enterprise_identities=enterprise_identities or [],
    )


async def _user_identity_source_names(
    session: AsyncSession,
    user_ids: list[str],
) -> dict[str, list[str]]:
    if not user_ids:
        return {}
    identities = (
        await session.execute(
            select(UserExternalIdentity.user_id, UserExternalIdentity.identity_provider)
            .where(UserExternalIdentity.user_id.in_(user_ids))
            .distinct()
        )
    ).all()
    source_filters = []
    for provider_key in {identity.identity_provider for identity in identities}:
        provider, _, tenant = provider_key.partition(":")
        if provider in {item.value for item in IdentitySourceProvider} and tenant:
            source_filters.append(
                and_(EnterpriseIdentitySource.provider == provider, EnterpriseIdentitySource.tenant_id == tenant)
            )
    if not source_filters:
        return {}
    sources = (
        await session.execute(select(EnterpriseIdentitySource).where(or_(*source_filters)))
    ).scalars().all()
    names_by_provider = {identity_provider_key(source): source.name for source in sources}
    result: dict[str, set[str]] = {}
    for identity in identities:
        name = names_by_provider.get(identity.identity_provider)
        if name:
            result.setdefault(identity.user_id, set()).add(name)
    return {user_id: sorted(names) for user_id, names in result.items()}


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
                ExternalUserPrincipalMembership.identity_source_id == identity_source_id,
                ExternalUserPrincipalMembership.principal_id == external_group_id,
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
            select(EnterpriseDirectoryUser.external_user_id.label("external_user_id")).where(
                EnterpriseDirectoryUser.identity_source_id == row.identity_source_id,
                EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
            ),
            select(ExternalUserPrincipalMembership.external_user_id.label("external_user_id")).where(
                ExternalUserPrincipalMembership.identity_source_id == row.identity_source_id,
                or_(
                    ExternalUserPrincipalMembership.expires_at.is_(None),
                    ExternalUserPrincipalMembership.expires_at > utc_now(),
                ),
            ),
        ).subquery()
        return int(await session.scalar(select(func.count(distinct(members.c.external_user_id)))) or 0)
    if row.group_kind == AgentGroupKind.IDENTITY_SOURCE:
        members = union_all(
            select(EnterpriseDirectoryUser.external_user_id.label("external_user_id"))
            .join(
                EnterpriseDirectoryMembership,
                (EnterpriseDirectoryMembership.identity_source_id == EnterpriseDirectoryUser.identity_source_id)
                & (EnterpriseDirectoryMembership.external_member_id == EnterpriseDirectoryUser.external_user_id),
            )
            .where(
                EnterpriseDirectoryUser.identity_source_id == row.identity_source_id,
                EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                EnterpriseDirectoryMembership.external_group_id == row.principal_id,
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.USER,
            ),
            select(ExternalUserPrincipalMembership.external_user_id.label("external_user_id")).where(
                ExternalUserPrincipalMembership.identity_source_id == row.identity_source_id,
                ExternalUserPrincipalMembership.principal_id == row.principal_id,
                or_(
                    ExternalUserPrincipalMembership.expires_at.is_(None),
                    ExternalUserPrincipalMembership.expires_at > utc_now(),
                ),
            ),
        ).subquery()
        return int(await session.scalar(select(func.count(distinct(members.c.external_user_id)))) or 0)
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
    kind: Any,
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
    assignment_id: Any = None,
) -> tuple[Any, ...]:
    def labeled(value: Any, name: str) -> Any:
        return (literal(None) if value is None else value).label(name)

    return (
        literal(rank).label("kind_rank"),
        (literal(kind) if isinstance(kind, str) else kind).label("group_kind"),
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
        labeled(assignment_id, "assignment_id"),
    )


def _group_options_query(
    agent_id: str,
    normalized_search: str,
    inherited_source_ids: list[str],
    binding_status: str,
) -> Any:
    if binding_status == "all":
        return union_all(
            select(_group_options_query(agent_id, normalized_search, inherited_source_ids, "bound")),
            select(_group_options_query(agent_id, normalized_search, inherited_source_ids, "unbound")),
        ).subquery()
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
            AgentGroupAssignment.identity_domain == EnterprisePrincipalAssignment.identity_domain,
            AgentGroupAssignment.provider == EnterprisePrincipalAssignment.provider,
            AgentGroupAssignment.principal_id == EnterprisePrincipalAssignment.principal_id,
        )
    )
    source_group_assigned = exists(
        select(AgentGroupAssignment.id).where(
            AgentGroupAssignment.agent_id == agent_id,
            AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE,
            AgentGroupAssignment.identity_source_id == EnterpriseIdentitySource.id,
            AgentGroupAssignment.principal_id == EnterpriseDirectoryGroup.external_group_id,
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
            (EnterpriseDirectoryUser.identity_source_id == EnterpriseIdentitySource.id)
            & (EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE),
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
                member_count=func.count(distinct(EnterprisePrincipalAssignment.pas_user_id)),
                sort_1=EnterprisePrincipalAssignment.identity_domain,
                sort_2=EnterprisePrincipalAssignment.provider,
                sort_3=EnterprisePrincipalAssignment.principal_id,
            )
        )
        .where(
            EnterprisePrincipalAssignment.principal_type == EnterprisePrincipalType.GROUP,
            EnterprisePrincipalAssignment.status == EnterprisePrincipalStatus.ACTIVE,
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
            EnterpriseDirectoryGroup.identity_source_id == EnterpriseIdentitySource.id,
        )
        .outerjoin(
            EnterpriseDirectoryMembership,
            (EnterpriseDirectoryMembership.identity_source_id == EnterpriseDirectoryGroup.identity_source_id)
            & (EnterpriseDirectoryMembership.external_group_id == EnterpriseDirectoryGroup.external_group_id)
            & (EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.USER),
        )
        .outerjoin(
            EnterpriseDirectoryUser,
            (EnterpriseDirectoryUser.identity_source_id == EnterpriseDirectoryMembership.identity_source_id)
            & (EnterpriseDirectoryUser.external_user_id == EnterpriseDirectoryMembership.external_member_id)
            & (EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE),
        )
        .where(
            usable_source,
            EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
            EnterpriseDirectoryGroup.principal_type.in_(
                (
                    EnterpriseDirectoryPrincipalType.GROUP,
                    EnterpriseDirectoryPrincipalType.DEPARTMENT,
                )
            ),
            ~source_group_assigned,
            EnterpriseIdentitySource.id.in_(inherited_source_ids)
            if binding_status == "bound"
            else ~EnterpriseIdentitySource.id.in_(inherited_source_ids),
        )
        .group_by(
            EnterpriseIdentitySource.id,
            EnterpriseIdentitySource.name,
            EnterpriseDirectoryGroup.external_group_id,
            EnterpriseDirectoryGroup.display_name,
        )
    )
    if binding_status == "bound":
        # Keep direct assignments even when their directory entries disappear.
        direct_query = (
            select(
                *_group_option_columns(
                    0,
                    AgentGroupAssignment.group_kind,
                    department_id=AgentGroupAssignment.department_id,
                    department_name=Department.name,
                    identity_domain=AgentGroupAssignment.identity_domain,
                    provider=AgentGroupAssignment.provider,
                    identity_source_id=AgentGroupAssignment.identity_source_id,
                    identity_source_name=EnterpriseIdentitySource.name,
                    external_group_id=EnterpriseDirectoryGroup.external_group_id,
                    external_group_name=EnterpriseDirectoryGroup.display_name,
                    principal_id=AgentGroupAssignment.principal_id,
                    member_count=literal(0),
                    sort_1=AgentGroupAssignment.group_kind.cast(String),
                    sort_2=AgentGroupAssignment.group_key,
                    assignment_id=AgentGroupAssignment.id,
                )
            )
            .select_from(AgentGroupAssignment)
            .outerjoin(Department, Department.id == AgentGroupAssignment.department_id)
            .outerjoin(
                EnterpriseIdentitySource,
                EnterpriseIdentitySource.id == AgentGroupAssignment.identity_source_id,
            )
            .outerjoin(
                EnterpriseDirectoryGroup,
                (EnterpriseDirectoryGroup.identity_source_id == AgentGroupAssignment.identity_source_id)
                & (EnterpriseDirectoryGroup.external_group_id == AgentGroupAssignment.principal_id),
            )
            .where(AgentGroupAssignment.agent_id == agent_id)
        )
        if normalized_search:
            direct_query = direct_query.where(
                or_(
                    Department.name.ilike(pattern),
                    EnterpriseIdentitySource.name.ilike(pattern),
                    EnterpriseDirectoryGroup.display_name.ilike(pattern),
                    AgentGroupAssignment.identity_domain.ilike(pattern),
                    AgentGroupAssignment.provider.ilike(pattern),
                    AgentGroupAssignment.principal_id.ilike(pattern),
                )
            )
    if normalized_search:
        department_query = department_query.where(Department.name.ilike(pattern))
        source_all_query = source_all_query.where(EnterpriseIdentitySource.name.ilike(pattern))
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
    if binding_status == "bound":
        return union_all(direct_query, source_group_query).subquery()
    return union_all(
        department_query,
        source_all_query,
        enterprise_query,
        source_group_query,
    ).subquery()


@router.get(
    "/{agent_id}/group-options",
    response_model=GroupOptionListResponse,
    response_model_exclude_defaults=True,
)
async def list_group_options(
    agent_id: str,
    search: str | None = None,
    category: Literal["identity_source", "group"] | None = None,
    identity_source_ids: str | None = None,
    binding_status: Literal["bound", "unbound", "all"] = "unbound",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    normalized_search = (search or "").strip()
    inherited_sources = await _bound_usable_identity_sources(session, agent_id)
    inherited_source_names = {source.id: source.name for source in inherited_sources}
    options_query = _group_options_query(
        agent_id, normalized_search, list(inherited_source_names), binding_status
    )
    query = select(options_query)
    allowed_source_ids = {value.strip() for value in (identity_source_ids or "").split(",") if value.strip()}
    if allowed_source_ids:
        query = query.where(
            or_(
                options_query.c.identity_source_id.is_(None),
                options_query.c.identity_source_id.in_(allowed_source_ids),
            )
        )
    if category == "identity_source":
        query = query.where(options_query.c.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL)
    elif category == "group":
        query = query.where(options_query.c.group_kind != AgentGroupKind.IDENTITY_SOURCE_ALL)
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        (
            await session.execute(
                query.order_by(
                    options_query.c.kind_rank,
                    options_query.c.sort_1,
                    options_query.c.sort_2,
                    options_query.c.sort_3,
                )
                .offset(offset)
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )
    items = []
    for row in rows:
        if row["assignment_id"]:
            assignment = await session.get(
                AgentGroupAssignment, row["assignment_id"],
                options=[selectinload(AgentGroupAssignment.department)],
            )
            item = GroupOptionResponse(**(await _group_assignment_response(session, assignment)).model_dump())
            item.assignment_id = assignment.id
        else:
            item = GroupOptionResponse(**row)
        if item.group_kind == AgentGroupKind.IDENTITY_SOURCE and item.identity_source_id in inherited_source_names:
            item.inherited_source_names = [inherited_source_names[item.identity_source_id]]
        items.append(item)
    return GroupOptionListResponse(
        items=items,
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
    category: Literal["identity_source", "group"] | None = None,
    identity_source_ids: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    filters = [AgentGroupAssignment.agent_id == agent_id]
    allowed_source_ids = {value.strip() for value in (identity_source_ids or "").split(",") if value.strip()}
    if allowed_source_ids:
        filters.append(
            or_(
                AgentGroupAssignment.identity_source_id.is_(None),
                AgentGroupAssignment.identity_source_id.in_(allowed_source_ids),
            )
        )
    if category == "identity_source":
        filters.append(AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL)
    elif category == "group":
        filters.append(AgentGroupAssignment.group_kind != AgentGroupKind.IDENTITY_SOURCE_ALL)
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
            delete(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.group_assignment_id == row.id)
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
    ).scalars().all()
    source_names = await _user_identity_source_names(session, [row.user_id for row in rows])
    identities = await enterprise_identities_by_user(session, [row.user_id for row in rows])
    return UserAssignmentListResponse(
        items=[_assignment_response(row, source_names.get(row.user_id), identities[row.user_id]) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get(
    "/{agent_id}/enterprise-user-options",
    response_model=EnterpriseUserOptionListResponse,
)
async def list_enterprise_user_options(
    agent_id: str,
    identity_source_ids: str,
    binding_status: Literal["bound", "unbound"] = "unbound",
    search: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    requested_source_ids = {value.strip() for value in identity_source_ids.split(",") if value.strip()}
    sources = (
        await session.execute(
            select(EnterpriseIdentitySource).where(EnterpriseIdentitySource.id.in_(requested_source_ids))
        )
    ).scalars().all()
    usable_sources = [source for source in sources if identity_source_snapshot_is_usable(source)]
    if not sources:
        return EnterpriseUserOptionListResponse(items=[], total=0, offset=offset, limit=limit)

    mapped_identity = or_(
        *[
            and_(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                UserExternalIdentity.identity_provider == identity_provider_key(source),
            )
            for source in sources
        ]
    )
    assignment_exists = (
        select(AgentUserAssignment.id)
        .where(
            AgentUserAssignment.agent_id == agent_id,
            AgentUserAssignment.user_id == User.id,
            AgentUserAssignment.is_direct.is_(True),
        )
        .exists()
    )
    availability = case(
        (or_(User.status == UserStatus.DISABLED,
             EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.DISABLED), literal("disabled")),
        (EnterpriseIdentitySource.id.in_([source.id for source in usable_sources]), literal("active")),
        else_=literal("unavailable"),
    )
    filters = [assignment_exists if binding_status == "bound" else ~assignment_exists]
    if binding_status == "unbound":
        filters.append(availability == "active")
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                EnterpriseDirectoryUser.display_name.ilike(pattern),
                EnterpriseDirectoryUser.external_user_id.ilike(pattern),
                EnterpriseDirectoryUser.email.ilike(pattern),
            )
        )
    query = (
        select(
            User.id,
            EnterpriseDirectoryUser.display_name,
            EnterpriseDirectoryUser.external_user_id,
            EnterpriseDirectoryUser.email,
            EnterpriseIdentitySource.id.label("identity_source_id"),
            EnterpriseIdentitySource.name.label("identity_source_name"),
            availability.label("availability"),
        )
        .join(
            UserExternalIdentity,
            UserExternalIdentity.external_subject == EnterpriseDirectoryUser.external_user_id,
        )
        .join(User, User.id == UserExternalIdentity.user_id)
        .join(EnterpriseIdentitySource, EnterpriseIdentitySource.id == EnterpriseDirectoryUser.identity_source_id)
        .where(mapped_identity, *filters)
    )
    total = int(await session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        await session.execute(
            query.order_by(EnterpriseIdentitySource.name, EnterpriseDirectoryUser.display_name, User.id)
            .offset(offset)
            .limit(limit)
        )
    ).mappings().all()
    return EnterpriseUserOptionListResponse(
        items=[EnterpriseUserOptionResponse(**row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


async def _bound_usable_identity_sources(session: AsyncSession, agent_id: str) -> list[EnterpriseIdentitySource]:
    sources = (
        await session.scalars(
            select(EnterpriseIdentitySource)
            .join(AgentGroupAssignment, AgentGroupAssignment.identity_source_id == EnterpriseIdentitySource.id)
            .where(
                AgentGroupAssignment.agent_id == agent_id,
                AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL,
            )
        )
    ).all()
    return [source for source in sources if identity_source_snapshot_is_usable(source)]


def _source_user_inheritance_query(sources: list[EnterpriseIdentitySource]) -> Any:
    directory_user_exists = exists(
        select(EnterpriseDirectoryUser.id).where(
            EnterpriseDirectoryUser.identity_source_id == EnterpriseIdentitySource.id,
            EnterpriseDirectoryUser.external_user_id == UserExternalIdentity.external_subject,
            EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
        )
    )
    local_principal_exists = exists(
        select(ExternalUserPrincipalMembership.id).where(
            ExternalUserPrincipalMembership.identity_source_id == EnterpriseIdentitySource.id,
            ExternalUserPrincipalMembership.external_user_id == UserExternalIdentity.external_subject,
            or_(
                ExternalUserPrincipalMembership.expires_at.is_(None),
                ExternalUserPrincipalMembership.expires_at > utc_now(),
            ),
        )
    )
    return (
        select(UserExternalIdentity.user_id, EnterpriseIdentitySource.name.label("source_name"))
        .join(
            EnterpriseIdentitySource,
            or_(
                *[
                    and_(
                        EnterpriseIdentitySource.id == source.id,
                        UserExternalIdentity.identity_provider == identity_provider_key(source),
                    )
                    for source in sources
                ],
                literal(False),
            ),
        )
        .where(or_(directory_user_exists, local_principal_exists))
        .distinct()
    ).subquery()


async def _agent_user_inheritance_query(session: AsyncSession, agent: Agent):
    source_users = _source_user_inheritance_query(await _bound_usable_identity_sources(session, agent.id))
    queries = [select(source_users.c.user_id, source_users.c.source_name)]
    if agent.all_builtin_users:
        queries.append(select(User.id.label("user_id"), literal("PAS").label("source_name")).where(
            User.auth_provider == AuthProvider.BUILTIN, User.status == UserStatus.ACTIVE,
        ))
    queries.append(
        select(UserDepartment.user_id, Department.name.label("source_name"))
        .join(Department, Department.id == UserDepartment.department_id)
        .join(AgentGroupAssignment, AgentGroupAssignment.department_id == Department.id)
        .where(AgentGroupAssignment.agent_id == agent.id, AgentGroupAssignment.group_kind == AgentGroupKind.DEPARTMENT)
    )
    queries.append(
        select(EnterprisePrincipalAssignment.pas_user_id.label("user_id"),
               AgentGroupAssignment.principal_id.label("source_name"))
        .join(AgentGroupAssignment, and_(
            AgentGroupAssignment.identity_domain == EnterprisePrincipalAssignment.identity_domain,
            AgentGroupAssignment.provider == EnterprisePrincipalAssignment.provider,
            AgentGroupAssignment.principal_id == EnterprisePrincipalAssignment.principal_id,
        ))
        .where(
            AgentGroupAssignment.agent_id == agent.id,
            AgentGroupAssignment.group_kind == AgentGroupKind.ENTERPRISE,
            EnterprisePrincipalAssignment.principal_type == EnterprisePrincipalType.GROUP,
            EnterprisePrincipalAssignment.status == EnterprisePrincipalStatus.ACTIVE,
            or_(EnterprisePrincipalAssignment.valid_until.is_(None), EnterprisePrincipalAssignment.valid_until > utc_now()),
        )
    )
    group_rows = (await session.execute(
        select(AgentGroupAssignment.identity_source_id, AgentGroupAssignment.principal_id,
               EnterpriseDirectoryGroup.display_name)
        .outerjoin(EnterpriseDirectoryGroup, and_(
            EnterpriseDirectoryGroup.identity_source_id == AgentGroupAssignment.identity_source_id,
            EnterpriseDirectoryGroup.external_group_id == AgentGroupAssignment.principal_id,
        ))
        .where(AgentGroupAssignment.agent_id == agent.id, AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE)
    )).all()
    for source_id, principal_id, name in group_rows:
        members = (await knowledge_subject_members(session, ("GROUP", source_id, principal_id))).subquery()
        queries.append(select(members.c[0].label("user_id"), literal(name or principal_id).label("source_name")))
    return union_all(*queries).subquery()


@router.get(
    "/{agent_id}/user-options",
    response_model=UserOptionListResponse,
)
async def list_user_options(
    agent_id: str,
    search: str | None = None,
    binding_status: Literal["bound", "unbound", "all"] = "unbound",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _require_agent(session, agent_id)
    direct_assignment_id = (
        select(AgentUserAssignment.id)
        .where(
            AgentUserAssignment.agent_id == agent_id,
            AgentUserAssignment.user_id == User.id,
            AgentUserAssignment.is_direct.is_(True),
        )
        .scalar_subquery()
    )
    inherited_users = await _agent_user_inheritance_query(session, agent)
    inherited = exists(select(inherited_users.c.user_id).where(inherited_users.c.user_id == User.id))
    bound = or_(direct_assignment_id.is_not(None), inherited)
    filters = [] if binding_status == "all" else [bound if binding_status == "bound" else ~bound]
    availability = await user_option_availability(session, agent_id)
    if binding_status != "bound":
        filters.append(or_(bound, availability == "active") if binding_status == "all" else availability == "active")
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
            select(
                User.id, User.display_name, User.external_id, User.status,
                availability.label("availability"),
                direct_assignment_id.label("assignment_id"),
            )
            .where(*filters)
            .order_by(User.created_at.desc(), User.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    source_names = await _user_identity_source_names(session, [row.id for row in rows])
    inherited_names: dict[str, list[str]] = {}
    for user_id, source_name in await session.execute(
        select(inherited_users).where(inherited_users.c.user_id.in_([row.id for row in rows]))
    ):
        inherited_names.setdefault(user_id, []).append(source_name)
    identities = await enterprise_identities_by_user(session, [row.id for row in rows])
    return UserOptionListResponse(
        items=[
            UserOptionResponse(
                id=row.id,
                display_name=row.display_name,
                external_id=row.external_id,
                status=row.status.value,
                availability=row.availability,
                identity_source_names=source_names.get(row.id, []),
                enterprise_identities=identities[row.id],
                assignment_id=row.assignment_id,
                inherited_source_names=sorted(set(inherited_names.get(row.id, []))),
            )
            for row in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


async def _all_subject_sources(session: AsyncSession, agent_id: str) -> list[EnterpriseIdentitySource]:
    sources = list(
        await session.scalars(
            select(EnterpriseIdentitySource).where(
                exists(
                    select(EnterpriseIdentitySourceSpaceBinding.id)
                    .join(
                        PolarRAGSpace,
                        PolarRAGSpace.knowledge_space_id == EnterpriseIdentitySourceSpaceBinding.knowledge_space_id,
                    )
                    .join(
                        AgentPolarRAGInstanceBinding,
                        AgentPolarRAGInstanceBinding.polarrag_instance_id == PolarRAGSpace.polarrag_instance_id,
                    )
                    .where(
                        EnterpriseIdentitySourceSpaceBinding.identity_source_id == EnterpriseIdentitySource.id,
                        AgentPolarRAGInstanceBinding.agent_id == agent_id,
                        PolarRAGSpace.enabled.is_(True),
                    )
                )
            )
        )
    )
    return [source for source in sources if identity_source_snapshot_is_usable(source)]


@router.get("/{agent_id}/all-subject-assignments", response_model=AllSubjectAssignmentsResponse)
async def get_all_subject_assignments(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await _require_agent(session, agent_id)
    sources = await _all_subject_sources(session, agent_id)
    assigned_sources = set(
        await session.scalars(
            select(AgentGroupAssignment.identity_source_id).where(
                AgentGroupAssignment.agent_id == agent_id,
                AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL,
            )
        )
    )
    native_users = select(User.id).where(User.auth_provider == AuthProvider.BUILTIN, User.status == UserStatus.ACTIVE)
    user_count = int(await session.scalar(select(func.count()).select_from(native_users.subquery())) or 0)
    assignment_count = int(agent.all_builtin_users)
    for model in (AgentUserAssignment, AgentGroupAssignment):
        assignment_count += int(
            await session.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    model.agent_id == agent_id,
                )
            )
            or 0
        )
    complete = (
        agent.all_builtin_users and all(source.id in assigned_sources for source in sources)
    )
    return AllSubjectAssignmentsResponse(
        status="BOUND" if complete else "PARTIAL" if assignment_count else "UNBOUND",
        eligible_source_count=len(sources),
        eligible_user_count=user_count,
        assignment_count=assignment_count,
    )


@router.post("/{agent_id}/all-subject-assignments", response_model=AllSubjectAssignmentsResponse)
async def update_all_subject_assignments(
    agent_id: str,
    body: AllSubjectAssignmentsRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await session.execute(
        update(Agent).where(Agent.id == agent_id).values(knowledge_scope_mode=Agent.knowledge_scope_mode)
    )
    agent = await _require_agent(session, agent_id)
    if await session.scalar(
        select(Agent.id).where(
            Agent.id == agent_id,
            Agent.bulk_assignment_status == "running",
            Agent.bulk_assignment_lease_until > utc_now(),
        )
    ):
        raise HTTPException(status_code=409, detail="A user assignment operation is still running")
    if body.operation == "BIND":
        agent.all_builtin_users = True
        assigned_sources = set(
            await session.scalars(
                select(AgentGroupAssignment.identity_source_id).where(
                    AgentGroupAssignment.agent_id == agent_id,
                    AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL,
                )
            )
        )
        for source in await _all_subject_sources(session, agent_id):
            if source.id not in assigned_sources:
                session.add(
                    AgentGroupAssignment.for_identity_source_all_users(
                        agent_id=agent_id,
                        identity_source_id=source.id,
                        created_by_user_id=admin.id,
                    )
                )
    else:
        agent.all_builtin_users = False
        group_ids = select(AgentGroupAssignment.id).where(AgentGroupAssignment.agent_id == agent_id)
        await session.execute(
            delete(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.group_assignment_id.in_(group_ids))
        )
        await session.execute(delete(AgentGroupAssignment).where(AgentGroupAssignment.agent_id == agent_id))
        # Assignment deletion also revokes its per-user Agent Token through the FK.
        await session.execute(delete(AgentUserAssignment).where(AgentUserAssignment.agent_id == agent_id))
    await _audit(
        session,
        admin,
        "agent_subject_assignment.all",
        "agent",
        agent_id,
        client_info=json.dumps({"operation": body.operation}),
    )
    await session.commit()
    return await get_all_subject_assignments(agent_id, admin, session)


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
            bulk_assignment_lease_until=now + timedelta(seconds=_BULK_ASSIGNMENT_LEASE_SECONDS),
        )
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await session.rollback()
        return None
    await session.commit()
    task = asyncio.create_task(_assign_all_users_in_background(session_factory, agent_id, admin_id, worker_id))
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
    user_id = body.user_id
    if body.external_user_id is not None:
        user_id = (await resolve_enterprise_user_ids(session, cast(str, body.identity_source_id), [body.external_user_id]))[0]
    user = await session.get(User, user_id)
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
    identities = await enterprise_identities_by_user(session, [row.user_id])
    return _assignment_response(row, enterprise_identities=identities[row.user_id])


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
