from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core.audit_logger import log_audit
from server.core.db_instance_service import (
    DBInstanceNotFound,
    DBInstanceServiceError,
    restore_db_instance_resource,
)
from server.db.engine import get_session
from server.models import (
    AuditStatus,
    DBInstanceResource,
    DBInstanceStatus,
    DeleteLifecycleStep,
    User,
)

router = APIRouter(
    prefix="/db-instance-resources", tags=["db-instance-resources"]
)


class ResourceActions(BaseModel):
    restore: bool


class DBInstanceResourceAdminResponse(BaseModel):
    id: str
    owner_agent_id: str
    backend_id: str
    allocated_instance_id: str | None
    name: str | None
    provisioning_mode: str
    status: str
    provisioning_step: str
    cleanup_step: str
    delete_step: str
    delete_requested_at: datetime | None
    disconnected_at: datetime | None
    cooldown_until: datetime | None
    delete_cooldown_duration_hours: int | None
    reclaim_policy: str | None
    failure_reason: str | None
    restore_failure_reason: str | None
    actions: ResourceActions

    @classmethod
    def from_model(
        cls, resource: DBInstanceResource
    ) -> "DBInstanceResourceAdminResponse":
        irreversible = resource.delete_step in {
            DeleteLifecycleStep.LOGICAL_CLEANUP,
            DeleteLifecycleStep.PHYSICAL_DESTROY,
            DeleteLifecycleStep.SANITIZE_DISPATCH,
            DeleteLifecycleStep.COMPLETE,
        }
        return cls(
            id=resource.id,
            owner_agent_id=resource.owner_agent_id,
            backend_id=resource.backend_id,
            allocated_instance_id=resource.allocated_instance_id,
            name=resource.name,
            provisioning_mode=resource.provisioning_mode.value,
            status=resource.status.value,
            provisioning_step=resource.provisioning_step.value,
            cleanup_step=resource.cleanup_step.value,
            delete_step=resource.delete_step.value,
            delete_requested_at=resource.delete_requested_at,
            disconnected_at=resource.disconnected_at,
            cooldown_until=resource.cooldown_until,
            delete_cooldown_duration_hours=(
                resource.effective_delete_cooldown_duration_hours
            ),
            reclaim_policy=(
                resource.reclaim_policy.value
                if resource.reclaim_policy is not None
                else None
            ),
            failure_reason=resource.failure_reason,
            restore_failure_reason=resource.restore_failure_reason,
            actions=ResourceActions(
                restore=(
                    resource.status
                    in {
                        DBInstanceStatus.DELETING,
                        DBInstanceStatus.DELETE_FAILED,
                        DBInstanceStatus.COOLING_DOWN,
                    }
                    and not irreversible
                )
            ),
        )


@router.get("", response_model=list[DBInstanceResourceAdminResponse])
async def list_db_instance_resources(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    resources = list(
        (
            await session.scalars(
                select(DBInstanceResource).order_by(
                    DBInstanceResource.created_at.desc()
                )
            )
        ).all()
    )
    return [
        DBInstanceResourceAdminResponse.from_model(resource)
        for resource in resources
    ]


@router.get(
    "/{resource_id}", response_model=DBInstanceResourceAdminResponse
)
async def get_db_instance_resource(
    resource_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    resource = await session.get(DBInstanceResource, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="Database resource not found")
    return DBInstanceResourceAdminResponse.from_model(resource)

@router.post(
    "/{resource_id}/restore",
    response_model=DBInstanceResourceAdminResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def restore_resource(
    resource_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    admin_id = admin.id
    await session.rollback()
    try:
        resource = await restore_db_instance_resource(session, resource_id)
    except DBInstanceNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except DBInstanceServiceError as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "RESOURCE_STATE_CONFLICT", "message": str(error)},
        ) from None
    await log_audit(
        session,
        user_id=admin_id,
        action="db_instance_resource.restore",
        status=AuditStatus.SUCCESS,
        target_type="db_instance_resource",
        target_id=resource.id,
        required=True,
        commit=False,
    )
    await session.commit()
    return DBInstanceResourceAdminResponse.from_model(resource)
