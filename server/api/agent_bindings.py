from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import admin_binding_service
from server.core import agent_instance_access_service
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import (
    AgentProvisioningBinding,
    AuditStatus,
    DBInstanceResource,
    Permission,
    ProvisioningBackendStatus,
    User,
)

router = APIRouter(prefix="/agents/{agent_id}", tags=["agent-bindings"])

class AgentInstanceAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str
    credential_id: str | None = None
    permission: Permission | None = None
    direct_enabled: bool | None = None
    capabilities: set[
        agent_instance_access_service.AgentInstanceAccessCapability
    ]


class AgentInstanceAccessUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: str | None = None
    permission: Permission | None = None
    direct_enabled: bool | None = None
    capabilities: set[
        agent_instance_access_service.AgentInstanceAccessCapability
    ]


class AgentInstanceAccessResponse(BaseModel):
    agent_id: str
    instance_id: str
    credential_id: str | None
    permission: Permission | None
    direct_enabled: bool | None
    capabilities: list[
        agent_instance_access_service.AgentInstanceAccessCapability
    ]
    direct_binding_id: str | None
    provisioning_binding_id: str | None
    provisioning_backend_id: str | None
    create_availability: agent_instance_access_service.CreateAvailability

    @classmethod
    def from_view(
        cls,
        view: agent_instance_access_service.AgentInstanceAccessView,
    ) -> "AgentInstanceAccessResponse":
        return cls(
            agent_id=view.agent_id,
            instance_id=view.instance_id,
            credential_id=view.credential_id,
            permission=view.permission,
            direct_enabled=view.direct_enabled,
            capabilities=list(view.capabilities),
            direct_binding_id=view.direct_binding_id,
            provisioning_binding_id=view.provisioning_binding_id,
            provisioning_backend_id=view.provisioning_backend_id,
            create_availability=view.create_availability,
        )


class ProvisioningBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend_id: str
    enabled: bool = True


class ProvisioningBindingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ProvisioningBindingResponse(BaseModel):
    id: str
    agent_id: str
    backend_id: str
    enabled: bool
    backend_status: ProvisioningBackendStatus
    allow_create: bool
    created_by_user_id: str
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def from_model(cls, binding: AgentProvisioningBinding) -> "ProvisioningBindingResponse":
        backend_status = binding.backend.status
        return cls(
            id=binding.id,
            agent_id=binding.agent_id,
            backend_id=binding.backend_id,
            enabled=binding.enabled,
            backend_status=backend_status,
            allow_create=(binding.enabled and backend_status == ProvisioningBackendStatus.ACTIVE),
            created_by_user_id=binding.created_by_user_id,
            created_at=binding.created_at,
            updated_at=binding.updated_at,
        )


class AgentResourceResponse(BaseModel):
    id: str
    backend_id: str
    client_token: str
    name: str | None
    engine: str
    status: str
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def from_model(cls, resource: DBInstanceResource) -> "AgentResourceResponse":
        return cls(
            id=resource.id,
            backend_id=resource.backend_id,
            client_token=resource.client_token,
            name=resource.name,
            engine=resource.engine.value,
            status=resource.status.value,
            created_at=resource.created_at,
            updated_at=resource.updated_at,
        )


async def _commit_mutation(
    session: AsyncSession,
    *,
    admin: User,
    action: str,
    target_type: str,
    target_id: str,
    instance_id: str | None = None,
) -> None:
    await log_audit(
        session,
        user_id=admin.id,
        instance_id=instance_id,
        action=action,
        status=AuditStatus.SUCCESS,
        user_name=admin.display_name,
        target_type=target_type,
        target_id=target_id,
        required=True,
        commit=False,
    )
    await session.commit()


def _binding_error(exc: Exception) -> HTTPException:
    if isinstance(
        exc,
        agent_instance_access_service.AgentInstanceAccessError,
    ):
        return HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        )
    if isinstance(exc, admin_binding_service.BindingNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, admin_binding_service.BindingValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, admin_binding_service.BindingConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, IntegrityError):
        return HTTPException(status_code=409, detail="Binding already exists")
    return HTTPException(status_code=503, detail="Binding administration unavailable")


@router.get(
    "/instance-bindings",
    response_model=list[AgentInstanceAccessResponse],
)
async def list_direct_bindings(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        rows = (
            await agent_instance_access_service
            .list_agent_instance_access(session, agent_id)
        )
    except admin_binding_service.BindingNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [
        AgentInstanceAccessResponse.from_view(row) for row in rows
    ]


@router.post(
    "/instance-bindings",
    response_model=AgentInstanceAccessResponse,
    status_code=201,
)
async def create_direct_binding(
    agent_id: str,
    body: AgentInstanceAccessRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row = (
            await agent_instance_access_service
            .upsert_agent_instance_access(
                session,
                agent_id=agent_id,
                instance_id=body.instance_id,
                credential_id=body.credential_id,
                permission=body.permission,
                direct_enabled=body.direct_enabled,
                capabilities=body.capabilities,
                admin_id=admin.id,
                require_existing=False,
            )
        )
        await _commit_mutation(
            session,
            admin=admin,
            action="binding.create",
            target_type="agent_instance_access",
            target_id=row.instance_id,
            instance_id=row.instance_id,
        )
        return AgentInstanceAccessResponse.from_view(row)
    except Exception as exc:
        await session.rollback()
        raise _binding_error(exc) from exc


@router.put(
    "/instance-bindings/{instance_id}",
    response_model=AgentInstanceAccessResponse,
)
async def update_direct_binding(
    agent_id: str,
    instance_id: str,
    body: AgentInstanceAccessUpdateRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row = (
            await agent_instance_access_service
            .upsert_agent_instance_access(
                session,
                agent_id=agent_id,
                instance_id=instance_id,
                credential_id=body.credential_id,
                permission=body.permission,
                direct_enabled=body.direct_enabled,
                capabilities=body.capabilities,
                admin_id=admin.id,
                require_existing=True,
            )
        )
        await _commit_mutation(
            session,
            admin=admin,
            action="binding.update",
            target_type="agent_instance_access",
            target_id=row.instance_id,
            instance_id=row.instance_id,
        )
        return AgentInstanceAccessResponse.from_view(row)
    except Exception as exc:
        await session.rollback()
        raise _binding_error(exc) from exc


@router.delete("/instance-bindings/{instance_id}", status_code=204)
async def delete_direct_binding(
    agent_id: str,
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row = (
            await agent_instance_access_service
            .delete_agent_instance_access(
                session,
                agent_id=agent_id,
                instance_id=instance_id,
            )
        )
        await _commit_mutation(
            session,
            admin=admin,
            action="binding.delete",
            target_type="agent_instance_access",
            target_id=row.instance_id,
            instance_id=row.instance_id,
        )
        return Response(status_code=204)
    except Exception as exc:
        await session.rollback()
        raise _binding_error(exc) from exc


@router.get(
    "/provisioning-bindings",
    response_model=list[ProvisioningBindingResponse],
)
async def list_provisioning_bindings(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        rows = await admin_binding_service.list_agent_provisioning_bindings(session, agent_id)
    except admin_binding_service.BindingNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [ProvisioningBindingResponse.from_model(row) for row in rows]


@router.post(
    "/provisioning-bindings",
    response_model=ProvisioningBindingResponse,
    status_code=201,
)
async def create_provisioning_binding(
    agent_id: str,
    body: ProvisioningBindingRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row = await admin_binding_service.create_agent_provisioning_binding(
            session,
            agent_id=agent_id,
            backend_id=body.backend_id,
            enabled=body.enabled,
            admin_id=admin.id,
        )
        await _commit_mutation(
            session,
            admin=admin,
            action="binding.create",
            target_type="agent_provisioning_binding",
            target_id=row.id,
        )
        return ProvisioningBindingResponse.from_model(row)
    except Exception as exc:
        await session.rollback()
        raise _binding_error(exc) from exc


@router.put(
    "/provisioning-bindings/{binding_id}",
    response_model=ProvisioningBindingResponse,
)
async def update_provisioning_binding(
    agent_id: str,
    binding_id: str,
    body: ProvisioningBindingUpdateRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row = await admin_binding_service.update_agent_provisioning_binding(
            session,
            agent_id=agent_id,
            binding_id=binding_id,
            enabled=body.enabled,
        )
        await _commit_mutation(
            session,
            admin=admin,
            action="binding.update",
            target_type="agent_provisioning_binding",
            target_id=row.id,
        )
        return ProvisioningBindingResponse.from_model(row)
    except Exception as exc:
        await session.rollback()
        raise _binding_error(exc) from exc


@router.delete("/provisioning-bindings/{binding_id}", status_code=204)
async def delete_provisioning_binding(
    agent_id: str,
    binding_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row = await admin_binding_service.delete_agent_provisioning_binding(
            session, agent_id=agent_id, binding_id=binding_id
        )
        await _commit_mutation(
            session,
            admin=admin,
            action="binding.delete",
            target_type="agent_provisioning_binding",
            target_id=row.id,
        )
        return Response(status_code=204)
    except Exception as exc:
        await session.rollback()
        raise _binding_error(exc) from exc


@router.get("/resources", response_model=list[AgentResourceResponse])
async def list_resources(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        rows = await admin_binding_service.list_agent_resources(session, agent_id)
    except admin_binding_service.BindingNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [AgentResourceResponse.from_model(row) for row in rows]
