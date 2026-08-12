from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import distinct, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.auth.dependencies import require_admin
from server.core import agent_user_token_service
from server.core.agent_access import has_group_agent_access
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
    PolarRAGInstance,
    User,
    UserDepartment,
)
from server.models.base import utc_now

router = APIRouter(prefix="/agents", tags=["agent-polarrag-access"])


class PolarRAGBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    polarrag_instance_id: str


class UserAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str


class GroupAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_kind: AgentGroupKind
    department_id: str | None = None
    identity_domain: str | None = None
    provider: str | None = None
    principal_id: str | None = None


class GroupOptionResponse(BaseModel):
    group_kind: AgentGroupKind
    department_id: str | None
    department_name: str | None
    identity_domain: str | None
    provider: str | None
    principal_id: str | None
    member_count: int


class GroupAssignmentResponse(GroupOptionResponse):
    id: str
    created_at: datetime


class PolarRAGBindingResponse(BaseModel):
    id: str
    polarrag_instance_id: str
    instance_name: str
    created_at: datetime


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
) -> None:
    await log_audit(
        session,
        user_id=admin.id,
        action=action,
        status=AuditStatus.SUCCESS,
        user_name=admin.display_name,
        target_type=target_type,
        target_id=target_id,
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
    return GroupAssignmentResponse(
        id=row.id,
        group_kind=row.group_kind,
        department_id=row.department_id,
        department_name=(row.department.name if row.department else None),
        identity_domain=row.identity_domain,
        provider=row.provider,
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
    return [
        GroupOptionResponse(
            group_kind=AgentGroupKind.DEPARTMENT,
            department_id=department_id,
            department_name=name,
            identity_domain=None,
            provider=None,
            principal_id=None,
            member_count=count,
        )
        for department_id, name, count in department_rows
    ] + [
        GroupOptionResponse(
            group_kind=AgentGroupKind.ENTERPRISE,
            department_id=None,
            department_name=None,
            identity_domain=identity_domain,
            provider=provider,
            principal_id=principal_id,
            member_count=count,
        )
        for identity_domain, provider, principal_id, count in enterprise_rows
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
    else:
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
