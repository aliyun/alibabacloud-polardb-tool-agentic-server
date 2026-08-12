from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core.audit_logger import log_audit
from server.core.permission_template_service import (
    PermissionSyncConfirmationRequired,
    PermissionSyncError,
    PermissionSyncTargetNotFound,
    create_permission_template_revision,
    request_permission_sync,
)
from server.db.engine import get_session
from server.models import (
    AuditStatus,
    PermissionSyncJob,
    PermissionSyncMode,
    PermissionSyncTarget,
    PermissionTemplate,
    PermissionTemplateRevision,
    User,
)

router = APIRouter(tags=["permission-templates"])


class CreatePermissionTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2048)


class CreatePermissionRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    privileges: list[str] = Field(min_length=1)
    grant_option: bool = False


class PermissionRevisionResponse(BaseModel):
    id: str
    revision: int
    privileges: list[str]
    grant_option: bool
    created_at: datetime

    @classmethod
    def from_model(
        cls, revision: PermissionTemplateRevision
    ) -> "PermissionRevisionResponse":
        return cls(
            id=revision.id,
            revision=revision.revision,
            privileges=list(json.loads(revision.privileges_json)),
            grant_option=revision.grant_option,
            created_at=revision.created_at,
        )


class PermissionTemplateResponse(BaseModel):
    id: str
    name: str
    description: str | None
    revisions: list[PermissionRevisionResponse]
    created_at: datetime

    @classmethod
    def from_model(
        cls, template: PermissionTemplate
    ) -> "PermissionTemplateResponse":
        return cls(
            id=template.id,
            name=template.name,
            description=template.description,
            revisions=[
                PermissionRevisionResponse.from_model(revision)
                for revision in sorted(
                    template.revisions,
                    key=lambda item: item.revision,
                    reverse=True,
                )
            ],
            created_at=template.created_at,
        )


class PermissionSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_scope: Literal["pool", "resource"]
    target_id: str = Field(min_length=1, max_length=36)
    mode: PermissionSyncMode
    confirmed: bool = False


class PermissionSyncTargetResponse(BaseModel):
    id: str
    member_id: str
    resource_id: str | None
    previous_revision_id: str | None
    status: str
    change_required: bool
    retry_count: int
    failure_reason: str | None

    @classmethod
    def from_model(
        cls, target: PermissionSyncTarget
    ) -> "PermissionSyncTargetResponse":
        return cls(
            id=target.id,
            member_id=target.member_id,
            resource_id=target.resource_id,
            previous_revision_id=target.previous_revision_id,
            status=target.status.value,
            change_required=target.change_required,
            retry_count=target.retry_count,
            failure_reason=target.failure_reason,
        )


class PermissionSyncJobResponse(BaseModel):
    id: str
    template_revision_id: str
    target_scope: str
    target_id: str
    mode: str
    status: str
    total_count: int
    completed_count: int
    failed_count: int
    failure_reason: str | None
    targets: list[PermissionSyncTargetResponse]

    @classmethod
    def from_model(
        cls, job: PermissionSyncJob
    ) -> "PermissionSyncJobResponse":
        return cls(
            id=job.id,
            template_revision_id=job.template_revision_id,
            target_scope=job.target_scope,
            target_id=job.target_id,
            mode=job.mode.value,
            status=job.status.value,
            total_count=job.total_count,
            completed_count=job.completed_count,
            failed_count=job.failed_count,
            failure_reason=job.failure_reason,
            targets=[
                PermissionSyncTargetResponse.from_model(target)
                for target in job.targets
            ],
        )


@router.get(
    "/permission-templates",
    response_model=list[PermissionTemplateResponse],
)
async def list_permission_templates(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    templates = list(
        (
            await session.scalars(
                select(PermissionTemplate).order_by(PermissionTemplate.name)
            )
        ).all()
    )
    return [PermissionTemplateResponse.from_model(row) for row in templates]


@router.post(
    "/permission-templates",
    response_model=PermissionTemplateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_permission_template(
    body: CreatePermissionTemplateRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    template = PermissionTemplate(
        name=body.name.strip(), description=body.description
    )
    session.add(template)
    try:
        await session.flush()
        await log_audit(
            session,
            user_id=admin.id,
            action="permission_template.create",
            status=AuditStatus.SUCCESS,
            target_type="permission_template",
            target_id=template.id,
            required=True,
            commit=False,
        )
        await session.commit()
        await session.refresh(template, attribute_names=["revisions"])
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PERMISSION_TEMPLATE_EXISTS",
                "message": "Permission template name already exists.",
            },
        ) from None
    return PermissionTemplateResponse.from_model(template)


@router.post(
    "/permission-templates/{template_id}/revisions",
    response_model=PermissionRevisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_permission_revision(
    template_id: str,
    body: CreatePermissionRevisionRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if await session.get(PermissionTemplate, template_id) is None:
        raise HTTPException(status_code=404, detail="Permission template not found")
    try:
        revision = await create_permission_template_revision(
            session,
            template_id=template_id,
            privileges=body.privileges,
            grant_option=body.grant_option,
            created_by_user_id=admin.id,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_PERMISSION_TEMPLATE", "message": str(error)},
        ) from None
    await log_audit(
        session,
        user_id=admin.id,
        action="permission_template.revision.create",
        status=AuditStatus.SUCCESS,
        target_type="permission_template_revision",
        target_id=revision.id,
        required=True,
        commit=False,
    )
    await session.commit()
    return PermissionRevisionResponse.from_model(revision)


@router.post(
    "/permission-template-revisions/{revision_id}/sync",
    response_model=PermissionSyncJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def synchronize_permission_revision(
    revision_id: str,
    body: PermissionSyncRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        job = await request_permission_sync(
            session,
            revision_id=revision_id,
            target_scope=body.target_scope,
            target_id=body.target_id,
            mode=body.mode,
            confirmed_by_user_id=admin.id if body.confirmed else None,
        )
    except PermissionSyncConfirmationRequired as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "CONFIRMATION_REQUIRED", "message": str(error)},
        ) from None
    except PermissionSyncTargetNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except PermissionSyncError as error:
        raise HTTPException(status_code=422, detail=str(error)) from None
    await log_audit(
        session,
        user_id=admin.id,
        action=f"permission_sync.{body.mode.value}",
        status=AuditStatus.SUCCESS,
        target_type="permission_sync_job",
        target_id=job.id,
        required=True,
        commit=False,
    )
    await session.commit()
    return PermissionSyncJobResponse.from_model(job)


@router.get(
    "/permission-sync-jobs/{job_id}",
    response_model=PermissionSyncJobResponse,
)
async def get_permission_sync_job(
    job_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    job = await session.get(PermissionSyncJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Permission sync job not found")
    return PermissionSyncJobResponse.from_model(job)
