from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import provisioning_backend_service
from server.core.provisioning_backend_repository import (
    backend_is_fresh_and_healthy,
)
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import (
    AuditStatus,
    ProvisioningBackend,
    ProvisioningBackendStatus,
    User,
)

router = APIRouter(
    prefix="/provisioning-backends",
    tags=["provisioning-backends"],
)


class CreateBackendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str
    admin_credential_id: str
    priority: int = 0
    max_active_resources: int = Field(gt=0)
    resource_min_cpu: Decimal = Field(ge=0)
    resource_max_cpu: Decimal = Field(gt=0)
    ddl_concurrency: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_cpu_range(self) -> "CreateBackendRequest":
        if self.resource_min_cpu > self.resource_max_cpu:
            raise ValueError(
                "resource_min_cpu must not exceed resource_max_cpu"
            )
        if any(
            value != value.to_integral_value()
            for value in (
                self.resource_min_cpu,
                self.resource_max_cpu,
            )
        ):
            raise ValueError("CPU values must be whole units")
        return self


class UpdateBackendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    admin_credential_id: str | None = None
    priority: int | None = None
    max_active_resources: int | None = Field(default=None, gt=0)
    resource_min_cpu: Decimal | None = Field(default=None, ge=0)
    resource_max_cpu: Decimal | None = Field(default=None, gt=0)
    ddl_concurrency: int | None = Field(default=None, gt=0)
    status: Literal["active"] | None = None

    @model_validator(mode="after")
    def validate_cpu_values(self) -> "UpdateBackendRequest":
        if not self.model_fields_set:
            raise ValueError("At least one backend field must be updated")
        if any(
            getattr(self, field) is None for field in self.model_fields_set
        ):
            raise ValueError("Backend update fields cannot be null")
        for value in (self.resource_min_cpu, self.resource_max_cpu):
            if value is not None and value != value.to_integral_value():
                raise ValueError("CPU values must be whole units")
        if (
            self.resource_min_cpu is not None
            and self.resource_max_cpu is not None
            and self.resource_min_cpu > self.resource_max_cpu
        ):
            raise ValueError(
                "resource_min_cpu must not exceed resource_max_cpu"
            )
        return self


class BackendResponse(BaseModel):
    id: str
    instance_id: str
    admin_credential_id: str
    status: ProvisioningBackendStatus
    priority: int
    max_active_resources: int
    resource_min_cpu: int
    resource_max_cpu: int
    ddl_concurrency: int
    config_revision: int
    healthy: bool | None
    health_checked_at: datetime | None
    available_for_create: bool
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def from_model(
        cls, backend: ProvisioningBackend
    ) -> "BackendResponse":
        health = backend.health
        return cls(
            id=backend.id,
            instance_id=backend.instance_id,
            admin_credential_id=backend.admin_credential_id,
            status=backend.status,
            priority=backend.priority,
            max_active_resources=backend.max_active_resources,
            resource_min_cpu=backend.resource_min_cpu,
            resource_max_cpu=backend.resource_max_cpu,
            ddl_concurrency=backend.ddl_concurrency,
            config_revision=backend.config_revision,
            healthy=health.healthy if health is not None else None,
            health_checked_at=(
                health.checked_at if health is not None else None
            ),
            available_for_create=backend_is_fresh_and_healthy(
                backend
            ),
            created_at=backend.created_at,
            updated_at=backend.updated_at,
        )


async def _required_audit(
    session: AsyncSession,
    *,
    admin: User,
    action: str,
    backend: ProvisioningBackend,
) -> None:
    await log_audit(
        session,
        user_id=admin.id,
        instance_id=backend.instance_id,
        action=action,
        status=AuditStatus.SUCCESS,
        user_name=admin.display_name,
        target_type="provisioning_backend",
        target_id=backend.id,
        required=True,
        commit=False,
    )


async def _commit_backend_change(
    session: AsyncSession,
    *,
    admin: User,
    action: str,
    backend: ProvisioningBackend,
) -> BackendResponse:
    await _required_audit(
        session,
        admin=admin,
        action=action,
        backend=backend,
    )
    await session.commit()
    await session.refresh(backend, attribute_names=["health"])
    return BackendResponse.from_model(backend)


def _update_changes(body: UpdateBackendRequest) -> dict[str, object]:
    changes: dict[str, object] = {}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None:
            continue
        if field in {"resource_min_cpu", "resource_max_cpu"}:
            value = int(value)
        elif field == "status":
            value = ProvisioningBackendStatus(value)
        changes[field] = value
    return changes


@router.get("", response_model=list[BackendResponse])
async def list_provisioning_backends(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return [
        BackendResponse.from_model(backend)
        for backend in await provisioning_backend_service.list_backends(
            session
        )
    ]


@router.post("", response_model=BackendResponse, status_code=201)
async def create_provisioning_backend(
    body: CreateBackendRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        backend = await provisioning_backend_service.create_backend(
            session,
            instance_id=body.instance_id,
            admin_credential_id=body.admin_credential_id,
            priority=body.priority,
            max_active_resources=body.max_active_resources,
            resource_min_cpu=int(body.resource_min_cpu),
            resource_max_cpu=int(body.resource_max_cpu),
            ddl_concurrency=body.ddl_concurrency,
        )
        return await _commit_backend_change(
            session,
            admin=admin,
            action="backend.activate",
            backend=backend,
        )
    except provisioning_backend_service.BackendNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except provisioning_backend_service.BackendValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Provisioning backend already exists",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Provisioning backend administration unavailable",
        ) from exc


@router.put("/{backend_id}", response_model=BackendResponse)
async def update_provisioning_backend(
    backend_id: str,
    body: UpdateBackendRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        changes = _update_changes(body)
        backend = await provisioning_backend_service.update_backend(
            session,
            backend_id,
            changes,
        )
        action = (
            "backend.activate"
            if changes.get("status")
            == ProvisioningBackendStatus.ACTIVE
            else "backend.update"
        )
        return await _commit_backend_change(
            session,
            admin=admin,
            action=action,
            backend=backend,
        )
    except provisioning_backend_service.BackendNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except provisioning_backend_service.BackendValidationError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Provisioning backend update conflicts",
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Provisioning backend administration unavailable",
        ) from exc


async def _set_status(
    backend_id: str,
    status: ProvisioningBackendStatus,
    *,
    admin: User,
    session: AsyncSession,
) -> BackendResponse:
    try:
        backend = await provisioning_backend_service.set_backend_status(
            session, backend_id, status
        )
        return await _commit_backend_change(
            session,
            admin=admin,
            action=(
                "backend.drain"
                if status == ProvisioningBackendStatus.DRAINING
                else "backend.disable"
            ),
            backend=backend,
        )
    except provisioning_backend_service.BackendNotFound as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except provisioning_backend_service.InvalidBackendTransition as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Provisioning backend administration unavailable",
        ) from exc


@router.post("/{backend_id}/drain", response_model=BackendResponse)
async def drain_provisioning_backend(
    backend_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return await _set_status(
        backend_id,
        ProvisioningBackendStatus.DRAINING,
        admin=admin,
        session=session,
    )


@router.post("/{backend_id}/disable", response_model=BackendResponse)
async def disable_provisioning_backend(
    backend_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return await _set_status(
        backend_id,
        ProvisioningBackendStatus.DISABLED,
        admin=admin,
        session=session,
    )
