from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import admin_binding_service
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import (
    AuditStatus,
    BindingCapability,
    BindingOrigin,
    Permission,
    User,
    UserInstanceBinding,
)

router = APIRouter(prefix="/users/{user_id}", tags=["user-instance-access"])

_CAPABILITY_ORDER = {
    capability: index
    for index, capability in enumerate(
        (
            BindingCapability.DB_INSTANCE_LIST,
            BindingCapability.DB_INSTANCE_DESCRIBE,
            BindingCapability.DB_INSTANCE_CREDENTIALS_READ,
            BindingCapability.SQL_READ,
            BindingCapability.SQL_WRITE,
        )
    )
}


class UserInstanceAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credential_id: str
    permission: Permission
    capabilities: set[BindingCapability]
    enabled: bool = True


class UserInstanceAccessResponse(BaseModel):
    id: str
    user_id: str
    instance_id: str
    credential_id: str | None
    permission: Permission
    capabilities: list[BindingCapability]
    enabled: bool
    origin: BindingOrigin
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def from_model(cls, binding: UserInstanceBinding) -> "UserInstanceAccessResponse":
        return cls(
            id=binding.id,
            user_id=binding.user_id,
            instance_id=binding.instance_id,
            credential_id=binding.credential_id,
            permission=binding.permission,
            capabilities=sorted(
                (row.capability for row in binding.capabilities),
                key=_CAPABILITY_ORDER.__getitem__,
            ),
            enabled=binding.enabled,
            origin=binding.origin,
            created_at=binding.created_at,
            updated_at=binding.updated_at,
        )


@router.get(
    "/instance-access/{instance_id}",
    response_model=UserInstanceAccessResponse,
)
async def get_instance_access(
    user_id: str,
    instance_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row = await admin_binding_service.get_user_instance_access(session, user_id=user_id, instance_id=instance_id)
    except admin_binding_service.BindingNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return UserInstanceAccessResponse.from_model(row)


@router.put(
    "/instance-access/{instance_id}",
    response_model=UserInstanceAccessResponse,
)
async def update_instance_access(
    user_id: str,
    instance_id: str,
    body: UserInstanceAccessRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row, created = await admin_binding_service.update_user_instance_access(
            session,
            user_id=user_id,
            instance_id=instance_id,
            credential_id=body.credential_id,
            permission=body.permission,
            capabilities=body.capabilities,
            enabled=body.enabled,
        )
        await log_audit(
            session,
            user_id=admin.id,
            instance_id=instance_id,
            action="binding.create" if created else "binding.update",
            status=AuditStatus.SUCCESS,
            user_name=admin.display_name,
            target_type="user_instance_binding",
            target_id=row.id,
            required=True,
            commit=False,
        )
        await session.commit()
        return UserInstanceAccessResponse.from_model(row)
    except admin_binding_service.BindingNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except admin_binding_service.BindingValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="User instance access conflicts") from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="User instance access administration unavailable",
        ) from exc
