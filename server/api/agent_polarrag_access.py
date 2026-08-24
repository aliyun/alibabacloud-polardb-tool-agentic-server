from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.auth.dependencies import require_admin
from server.core import agent_user_token_service
from server.core.agent_access import has_group_agent_access
from server.core.agent_enterprise_access_service import (
    EnterpriseAccessAuditError,
    EnterpriseAccessPreviewStaleError,
    EnterpriseAccessSelection,
    EnterpriseAccessValidationError,
    apply_enterprise_access,
    preview_enterprise_access,
)
from server.core.resource_write_guard import serialized_resource_write
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentGroupKind,
    AgentPolarRAGInstanceBinding,
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
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGSpace,
    User,
    UserDepartment,
)
from server.models.base import utc_now

router = APIRouter(prefix="/agents", tags=["agent-polarrag-access"])


class PolarRAGBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    polarrag_instance_id: str


class PolarRAGPublicScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_knowledge_resource_ids: list[str] | None


class UserAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str


class GroupAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_kind: AgentGroupKind
    department_id: str | None = None
    identity_domain: str | None = None
    provider: str | None = None
    identity_source_id: str | None = None
    principal_id: str | None = None


class EnterpriseAccessSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_source_id: str = Field(min_length=1, max_length=36)
    all_synced_users: bool = False
    directory_group_ids: list[str] = Field(default_factory=list, max_length=500)
    pas_user_ids: list[str] = Field(default_factory=list, max_length=500)
    knowledge_space_ids: list[str] = Field(min_length=1, max_length=200)


class EnterpriseAccessApplyRequest(EnterpriseAccessSelectionRequest):
    preview_hash: str


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


class GroupAssignmentResponse(GroupOptionResponse):
    id: str
    created_at: datetime


class PolarRAGBindingResponse(BaseModel):
    id: str
    polarrag_instance_id: str
    instance_name: str
    public_knowledge_resource_ids: list[str] | None
    created_at: datetime


class PolarRAGPublicResourceResponse(BaseModel):
    knowledge_resource_id: str
    name: str
    knowledge_space_name: str


def _public_resource_ids(
    row: AgentPolarRAGInstanceBinding,
) -> list[str] | None:
    raw = row.public_knowledge_resource_ids_json
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        return []
    return value


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


def _binding_response(
    row: AgentPolarRAGInstanceBinding,
) -> PolarRAGBindingResponse:
    return PolarRAGBindingResponse(
        id=row.id,
        polarrag_instance_id=row.polarrag_instance_id,
        instance_name=row.instance.name,
        public_knowledge_resource_ids=_public_resource_ids(row),
        created_at=row.created_at,
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


def _enterprise_access_selection(
    body: EnterpriseAccessSelectionRequest,
) -> EnterpriseAccessSelection:
    return EnterpriseAccessSelection(
        identity_source_id=body.identity_source_id,
        all_synced_users=body.all_synced_users,
        directory_group_ids=tuple(body.directory_group_ids),
        pas_user_ids=tuple(body.pas_user_ids),
        knowledge_space_ids=tuple(body.knowledge_space_ids),
    )


@router.post("/{agent_id}/enterprise-access/preview")
async def preview_agent_enterprise_access(
    agent_id: str,
    body: EnterpriseAccessSelectionRequest,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        preview = await preview_enterprise_access(
            session,
            agent_id=agent_id,
            selection=_enterprise_access_selection(body),
        )
    except EnterpriseAccessValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    return asdict(preview)


@router.post("/{agent_id}/enterprise-access/apply")
async def apply_agent_enterprise_access(
    agent_id: str,
    body: EnterpriseAccessApplyRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        if session.in_transaction():
            await session.rollback()
        async with serialized_resource_write(session):
            result = await apply_enterprise_access(
                session,
                agent_id=agent_id,
                admin=admin,
                selection=_enterprise_access_selection(body),
                preview_hash=body.preview_hash,
            )
            await session.commit()
    except EnterpriseAccessPreviewStaleError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ENTERPRISE_ACCESS_PREVIEW_STALE",
                "message": "Configuration changed; review the refreshed preview",
                "preview": asdict(exc.preview),
            },
        ) from exc
    except EnterpriseAccessValidationError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except EnterpriseAccessAuditError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ENTERPRISE_ACCESS_CONFLICT",
                "message": "Enterprise access configuration conflicted",
            },
        ) from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Enterprise access configuration unavailable",
        ) from exc
    return asdict(result)


async def _enterprise_group_exists(
    session: AsyncSession,
    identity_domain: str,
    provider: str,
    principal_id: str,
) -> bool:
    return (
        await session.execute(
            select(EnterprisePrincipalAssignment.id).where(
                EnterprisePrincipalAssignment.identity_domain
                == identity_domain,
                EnterprisePrincipalAssignment.provider == provider,
                EnterprisePrincipalAssignment.principal_type
                == EnterprisePrincipalType.GROUP,
                EnterprisePrincipalAssignment.principal_id == principal_id,
                EnterprisePrincipalAssignment.status
                == EnterprisePrincipalStatus.ACTIVE,
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
    return (
        await session.execute(
            select(EnterpriseDirectoryGroup.id)
            .join(
                EnterpriseIdentitySource,
                EnterpriseIdentitySource.id
                == EnterpriseDirectoryGroup.identity_source_id,
            )
            .where(
                EnterpriseDirectoryGroup.identity_source_id
                == identity_source_id,
                EnterpriseDirectoryGroup.external_group_id == external_group_id,
                EnterpriseDirectoryGroup.principal_type
                == EnterpriseDirectoryPrincipalType.GROUP,
                EnterpriseDirectoryGroup.status
                == EnterpriseDirectoryEntryStatus.ACTIVE,
                EnterpriseIdentitySource.status
                == EnterpriseIdentitySourceStatus.ACTIVE,
            )
        )
    ).first() is not None


async def _identity_source_exists(
    session: AsyncSession,
    identity_source_id: str,
) -> bool:
    return (
        await session.execute(
            select(EnterpriseIdentitySource.id).where(
                EnterpriseIdentitySource.id == identity_source_id,
                EnterpriseIdentitySource.status
                == EnterpriseIdentitySourceStatus.ACTIVE,
            )
        )
    ).first() is not None


async def _group_member_count(
    session: AsyncSession,
    row: AgentGroupAssignment,
) -> int:
    if row.group_kind == AgentGroupKind.DEPARTMENT:
        return int(
            await session.scalar(
                select(func.count(UserDepartment.user_id)).where(
                    UserDepartment.department_id == row.department_id
                )
            )
            or 0
        )
    if row.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL:
        return int(
            await session.scalar(
                select(func.count(EnterpriseDirectoryUser.id)).where(
                    EnterpriseDirectoryUser.identity_source_id
                    == row.identity_source_id,
                    EnterpriseDirectoryUser.status
                    == EnterpriseDirectoryEntryStatus.ACTIVE,
                )
            )
            or 0
        )
    if row.group_kind == AgentGroupKind.IDENTITY_SOURCE:
        return int(
            await session.scalar(
                select(func.count(distinct(EnterpriseDirectoryUser.id)))
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
                )
            )
            or 0
        )
    return int(
        await session.scalar(
            select(func.count(distinct(EnterprisePrincipalAssignment.pas_user_id))).where(
                EnterprisePrincipalAssignment.identity_domain == row.identity_domain,
                EnterprisePrincipalAssignment.provider == row.provider,
                EnterprisePrincipalAssignment.principal_type
                == EnterprisePrincipalType.GROUP,
                EnterprisePrincipalAssignment.principal_id == row.principal_id,
                EnterprisePrincipalAssignment.status
                == EnterprisePrincipalStatus.ACTIVE,
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
                    EnterpriseDirectoryGroup.identity_source_id
                    == row.identity_source_id,
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
        external_group_id=(
            row.principal_id
            if row.group_kind == AgentGroupKind.IDENTITY_SOURCE
            else None
        ),
        external_group_name=external_group_name,
        principal_id=row.principal_id,
        member_count=await _group_member_count(session, row),
        created_at=row.created_at,
    )


@router.get(
    "/{agent_id}/group-options",
    response_model=list[GroupOptionResponse],
)
async def list_group_options(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    department_rows = (
        await session.execute(
            select(
                Department.id,
                Department.name,
                func.count(UserDepartment.user_id),
            )
            .outerjoin(
                UserDepartment,
                UserDepartment.department_id == Department.id,
            )
            .group_by(Department.id, Department.name)
            .order_by(Department.name, Department.id)
        )
    ).all()
    enterprise_rows = (
        await session.execute(
            select(
                EnterprisePrincipalAssignment.identity_domain,
                EnterprisePrincipalAssignment.provider,
                EnterprisePrincipalAssignment.principal_id,
                func.count(
                    distinct(EnterprisePrincipalAssignment.pas_user_id)
                ),
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
            )
            .group_by(
                EnterprisePrincipalAssignment.identity_domain,
                EnterprisePrincipalAssignment.provider,
                EnterprisePrincipalAssignment.principal_id,
            )
            .order_by(
                EnterprisePrincipalAssignment.identity_domain,
                EnterprisePrincipalAssignment.provider,
                EnterprisePrincipalAssignment.principal_id,
            )
        )
    ).all()
    identity_source_all_rows = list(
        (
            await session.execute(
                select(
                    EnterpriseIdentitySource.id,
                    EnterpriseIdentitySource.name,
                    func.count(EnterpriseDirectoryUser.id),
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
                .where(
                    EnterpriseIdentitySource.status
                    == EnterpriseIdentitySourceStatus.ACTIVE,
                )
                .group_by(
                    EnterpriseIdentitySource.id,
                    EnterpriseIdentitySource.name,
                )
                .order_by(
                    EnterpriseIdentitySource.name,
                    EnterpriseIdentitySource.id,
                )
            )
        ).all()
    )
    identity_source_rows = list(
        (
            await session.execute(
                select(
                    EnterpriseIdentitySource.id,
                    EnterpriseIdentitySource.name,
                    EnterpriseDirectoryGroup.external_group_id,
                    EnterpriseDirectoryGroup.display_name,
                    func.count(distinct(EnterpriseDirectoryUser.id)),
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
                    EnterpriseIdentitySource.status
                    == EnterpriseIdentitySourceStatus.ACTIVE,
                    EnterpriseDirectoryGroup.status
                    == EnterpriseDirectoryEntryStatus.ACTIVE,
                    EnterpriseDirectoryGroup.principal_type
                    == EnterpriseDirectoryPrincipalType.GROUP,
                )
                .group_by(
                    EnterpriseIdentitySource.id,
                    EnterpriseIdentitySource.name,
                    EnterpriseDirectoryGroup.external_group_id,
                    EnterpriseDirectoryGroup.display_name,
                )
                .order_by(
                    EnterpriseIdentitySource.name,
                    EnterpriseDirectoryGroup.display_name,
                    EnterpriseDirectoryGroup.external_group_id,
                )
            )
        ).all()
    )
    return [
        GroupOptionResponse(
            group_kind=AgentGroupKind.DEPARTMENT,
            department_id=department_id,
            department_name=name,
            identity_domain=None,
            provider=None,
            identity_source_id=None,
            identity_source_name=None,
            external_group_id=None,
            external_group_name=None,
            principal_id=None,
            member_count=count,
        )
        for department_id, name, count in department_rows
    ] + [
        GroupOptionResponse(
            group_kind=AgentGroupKind.IDENTITY_SOURCE_ALL,
            department_id=None,
            department_name=None,
            identity_domain=None,
            provider=None,
            identity_source_id=source_id,
            identity_source_name=source_name,
            external_group_id=None,
            external_group_name=None,
            principal_id=None,
            member_count=count,
        )
        for source_id, source_name, count in identity_source_all_rows
    ] + [
        GroupOptionResponse(
            group_kind=AgentGroupKind.ENTERPRISE,
            department_id=None,
            department_name=None,
            identity_domain=identity_domain,
            provider=provider,
            identity_source_id=None,
            identity_source_name=None,
            external_group_id=None,
            external_group_name=None,
            principal_id=principal_id,
            member_count=count,
        )
        for identity_domain, provider, principal_id, count in enterprise_rows
    ] + [
        GroupOptionResponse(
            group_kind=AgentGroupKind.IDENTITY_SOURCE,
            department_id=None,
            department_name=None,
            identity_domain=None,
            provider=None,
            identity_source_id=source_id,
            identity_source_name=source_name,
            external_group_id=external_group_id,
            external_group_name=external_group_name,
            principal_id=external_group_id,
            member_count=count,
        )
        for (
            source_id,
            source_name,
            external_group_id,
            external_group_name,
            count,
        ) in identity_source_rows
    ]


@router.get(
    "/{agent_id}/group-assignments",
    response_model=list[GroupAssignmentResponse],
)
async def list_group_assignments(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    rows = list(
        (
            await session.execute(
                select(AgentGroupAssignment)
                .options(selectinload(AgentGroupAssignment.department))
                .where(AgentGroupAssignment.agent_id == agent_id)
                .order_by(
                    AgentGroupAssignment.group_kind,
                    AgentGroupAssignment.group_key,
                )
            )
        ).scalars()
    )
    return [await _group_assignment_response(session, row) for row in rows]


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
        await session.delete(row)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc


@router.get(
    "/{agent_id}/polarrag-bindings",
    response_model=list[PolarRAGBindingResponse],
)
async def list_polarrag_bindings(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    rows = (
        await session.execute(
            select(AgentPolarRAGInstanceBinding)
            .options(selectinload(AgentPolarRAGInstanceBinding.instance))
            .where(AgentPolarRAGInstanceBinding.agent_id == agent_id)
            .order_by(AgentPolarRAGInstanceBinding.id)
        )
    ).scalars()
    return [_binding_response(row) for row in rows]


@router.post(
    "/{agent_id}/polarrag-bindings",
    response_model=PolarRAGBindingResponse,
    status_code=201,
)
async def create_polarrag_binding(
    agent_id: str,
    body: PolarRAGBindingRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    instance = await session.get(PolarRAGInstance, body.polarrag_instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="PolarRAG instance not found")
    row = AgentPolarRAGInstanceBinding(
        agent_id=agent_id,
        polarrag_instance_id=instance.id,
        created_by_user_id=admin.id,
    )
    row.instance = instance
    session.add(row)
    try:
        await session.flush()
        await _audit(
            session, admin, "agent_polarrag_binding.create", "agent_polarrag_binding", row.id
        )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="PolarRAG instance already bound") from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc
    return _binding_response(row)


async def _require_polarrag_binding(
    session: AsyncSession,
    agent_id: str,
    binding_id: str,
) -> AgentPolarRAGInstanceBinding:
    row = (
        await session.execute(
            select(AgentPolarRAGInstanceBinding)
            .options(selectinload(AgentPolarRAGInstanceBinding.instance))
            .where(
                AgentPolarRAGInstanceBinding.id == binding_id,
                AgentPolarRAGInstanceBinding.agent_id == agent_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="PolarRAG binding not found")
    return row


@router.get(
    "/{agent_id}/polarrag-bindings/{binding_id}/public-resources",
    response_model=list[PolarRAGPublicResourceResponse],
)
async def list_polarrag_binding_public_resources(
    agent_id: str,
    binding_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await _require_polarrag_binding(session, agent_id, binding_id)
    resources = (
        await session.execute(
            select(KnowledgeResource)
            .join(
                PolarRAGSpace,
                PolarRAGSpace.knowledge_space_id
                == KnowledgeResource.knowledge_space_id,
            )
            .options(selectinload(KnowledgeResource.space))
            .where(
                KnowledgeResource.polarrag_instance_id
                == row.polarrag_instance_id,
                KnowledgeResource.kb_type == "PUBLIC",
                KnowledgeResource.binding_mode == KnowledgeBindingMode.DOMAIN,
                KnowledgeResource.sync_status
                == KnowledgeResourceSyncStatus.ACTIVE,
                KnowledgeResource.enabled.is_(True),
                PolarRAGSpace.enabled.is_(True),
            )
            .order_by(KnowledgeResource.name, KnowledgeResource.id)
        )
    ).scalars()
    return [
        PolarRAGPublicResourceResponse(
            knowledge_resource_id=resource.id,
            name=resource.name,
            knowledge_space_name=resource.space.name,
        )
        for resource in resources
    ]


@router.put(
    "/{agent_id}/polarrag-bindings/{binding_id}/public-resources",
    response_model=PolarRAGBindingResponse,
)
async def update_polarrag_binding_public_resources(
    agent_id: str,
    binding_id: str,
    body: PolarRAGPublicScopeRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await _require_polarrag_binding(session, agent_id, binding_id)
    requested = body.public_knowledge_resource_ids
    if requested is not None:
        try:
            valid_shape = len(requested) == len(set(requested)) and all(
                str(uuid.UUID(resource_id)) == resource_id
                for resource_id in requested
            )
        except (TypeError, ValueError, AttributeError):
            valid_shape = False
        selected = set(
            (
                await session.execute(
                    select(KnowledgeResource.id)
                    .join(
                        PolarRAGSpace,
                        PolarRAGSpace.knowledge_space_id
                        == KnowledgeResource.knowledge_space_id,
                    )
                    .where(
                        KnowledgeResource.id.in_(requested),
                        KnowledgeResource.polarrag_instance_id
                        == row.polarrag_instance_id,
                        KnowledgeResource.kb_type == "PUBLIC",
                        KnowledgeResource.binding_mode
                        == KnowledgeBindingMode.DOMAIN,
                        KnowledgeResource.sync_status
                        == KnowledgeResourceSyncStatus.ACTIVE,
                        KnowledgeResource.enabled.is_(True),
                        PolarRAGSpace.enabled.is_(True),
                    )
                )
            ).scalars()
        )
        if not valid_shape or selected != set(requested):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Selected resources must be synchronized ACTIVE PUBLIC "
                    "knowledge resources on the bound instance"
                ),
            )
        requested = sorted(requested)
    row.public_knowledge_resource_ids_json = (
        None
        if requested is None
        else json.dumps(requested, separators=(",", ":"))
    )
    scope = "all" if requested is None else "selected"
    try:
        await _audit(
            session,
            admin,
            "agent_polarrag_binding.public_scope.update",
            "agent_polarrag_binding",
            row.id,
            client_info=json.dumps(
                {
                    "public_scope": scope,
                    "resource_count": len(requested or []),
                },
                separators=(",", ":"),
            ),
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc
    return _binding_response(row)


@router.delete("/{agent_id}/polarrag-bindings/{binding_id}", status_code=204)
async def delete_polarrag_binding(
    agent_id: str,
    binding_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.get(AgentPolarRAGInstanceBinding, binding_id)
    if row is None or row.agent_id != agent_id:
        raise HTTPException(status_code=404, detail="PolarRAG binding not found")
    try:
        await _audit(
            session, admin, "agent_polarrag_binding.delete", "agent_polarrag_binding", row.id
        )
        await session.delete(row)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc


@router.get(
    "/{agent_id}/user-assignments",
    response_model=list[UserAssignmentResponse],
)
async def list_user_assignments(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    rows = (
        await session.execute(
            select(AgentUserAssignment)
            .options(
                selectinload(AgentUserAssignment.user),
                selectinload(AgentUserAssignment.token),
            )
            .where(AgentUserAssignment.agent_id == agent_id)
            .where(AgentUserAssignment.is_direct.is_(True))
            .order_by(AgentUserAssignment.id)
        )
    ).scalars()
    return [_assignment_response(row) for row in rows]


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
        await _audit(
            session, admin, "agent_user_assignment.create", "agent_user_assignment", row.id
        )
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
        await _audit(
            session, admin, "agent_user_assignment.delete", "agent_user_assignment", row.id
        )
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
        await _audit(
            session, admin, "agent_user_token.force_revoke", "agent_user_token", row.id
        )
        await session.commit()
        assignment = await _load_assignment(session, assignment_id)
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail="Agent user token not found") from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Token revocation unavailable") from exc
    return _assignment_response(assignment)
