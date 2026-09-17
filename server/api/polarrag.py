from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal, cast
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from sqlalchemy import func, literal, or_, select, union_all, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.dependencies import require_admin
from server.core.audit_logger import log_audit
from server.core.crypto import encrypt
from server.db.engine import get_session
from server.models import (
    AuditStatus,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    KnowledgeResource,
    KnowledgeResourceManagementMode,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
    UserStatus,
)
from server.polarrag.catalog import (
    claim_space_catalog_sync,
    run_claimed_space_catalog_sync,
    sync_space_catalog,
)
from server.polarrag.client import _MAX_CATALOG_PAGES, client_from_instance
from server.polarrag.connection_config import InstanceCreate
from server.polarrag.contracts import (
    PolarRAGCapabilities,
    PolarRAGClient,
    PolarRAGErrorCode,
    PolarRAGKnowledgeBaseRecord,
    PolarRAGOperationNotSupported,
    PolarRAGSpaceRecord,
    PolarRAGUpstreamError,
)
from server.polarrag.identity import principal_assignment_is_valid_for_user
from server.polarrag.oss import (
    OssOperationError,
    normalize_prefix,
    validate_oss_write_access,
)

router = APIRouter(prefix="/polarrag", tags=["polarrag"])
logger = logging.getLogger(__name__)
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_UNCLAIMED_KNOWLEDGE_BASE_PAGE_SIZE = 20


async def _commit_with_required_audit(
    session: AsyncSession,
    **audit_fields,
) -> None:
    await log_audit(
        session,
        required=True,
        commit=False,
        **audit_fields,
    )
    await session.commit()

class KnowledgeResourceManagementModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    management_mode: KnowledgeResourceManagementMode


class InstanceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    username: str | None = Field(default=None, min_length=1, max_length=1024)
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    tls_verify: bool | None = None
    ca_bundle: str | None = Field(default=None, max_length=262144)

    @field_validator("name", "username")
    @classmethod
    def validate_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            raise ValueError("value must not be blank")
        return candidate


class EnableSpaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    space_id: str = Field(min_length=1, max_length=255)

    @field_validator("space_id")
    @classmethod
    def validate_space_id(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("space_id must not be blank")
        return candidate


class PrincipalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_domain: str = Field(min_length=1, max_length=255)
    provider: Literal["feishu", "sharepoint", "polarrag"]
    principal_type: Literal["user", "group"]
    principal_id: str = Field(min_length=1, max_length=255)
    valid_until: datetime | None = None

    @field_validator("identity_domain")
    @classmethod
    def validate_identity_domain(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("value must not be blank")
        return candidate

    @field_validator("principal_id")
    @classmethod
    def validate_principal_id(
        cls,
        value: str,
        info: ValidationInfo,
    ) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value if info.data.get("provider") == "polarrag" else value.strip()


class PrincipalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "disabled"] | None = None
    valid_until: datetime | None = None


class ClaimKnowledgeBaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_assignment_id: str | None = Field(default=None, min_length=1, max_length=36)
    pas_user_id: str | None = Field(default=None, min_length=1, max_length=36)

    @model_validator(mode="after")
    def validate_owner_reference(self) -> "ClaimKnowledgeBaseRequest":
        if (self.principal_assignment_id is None) == (self.pas_user_id is None):
            raise ValueError(
                "exactly one of principal_assignment_id or pas_user_id is required"
            )
        return self


class SpaceSyncStatus(BaseModel):
    status: Literal["idle", "running", "completed", "failed"]
    result: dict[str, int] | None = None
    error: str | None = None


class OssConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_key_id: str = Field(min_length=1, max_length=1024)
    access_key_secret: str = Field(min_length=1, max_length=4096)
    object_prefix: str = Field(default="pas/documents", min_length=1, max_length=512)

    @field_validator("access_key_id")
    @classmethod
    def validate_access_key_id(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("value must not be blank")
        return candidate

    @field_validator("access_key_secret")
    @classmethod
    def validate_access_key_secret(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("object_prefix")
    @classmethod
    def validate_prefix(cls, value: str) -> str:
        return normalize_prefix(value)


def _instance_response(instance: PolarRAGInstance) -> dict:
    capabilities = None
    if instance.capabilities_json:
        try:
            capabilities = json.loads(instance.capabilities_json)
        except ValueError:
            capabilities = None
    return {
        "id": instance.id,
        "name": instance.name,
        "scheme": instance.scheme,
        "host": instance.host,
        "port": instance.port,
        "tls_verify": instance.tls_verify,
        "status": instance.status.value,
        "plugin_version": instance.plugin_version,
        "capabilities": capabilities,
        "last_checked_at": instance.last_checked_at,
        "last_error_code": instance.last_error_code,
        "created_at": instance.created_at,
        "updated_at": instance.updated_at,
    }


def _principal_response(
    assignment: EnterprisePrincipalAssignment,
) -> dict:
    return {
        "id": assignment.id,
        "pas_user_id": assignment.pas_user_id,
        "identity_domain": assignment.identity_domain,
        "provider": assignment.provider,
        "principal_type": assignment.principal_type.value,
        "principal_id": assignment.principal_id,
        "source": assignment.source.value,
        "status": assignment.status.value,
        "valid_until": assignment.valid_until,
        "created_at": assignment.created_at,
        "updated_at": assignment.updated_at,
    }


def _space_response(
    upstream: PolarRAGSpaceRecord,
    local: PolarRAGSpace | None,
    knowledge_resource_count: int,
) -> dict:
    return {
        "space_id": upstream.space_id,
        "name": upstream.name,
        "identity_domain": upstream.identity_domain,
        "oss_bucket": upstream.oss_bucket,
        "oss_endpoint": upstream.oss_endpoint,
        "oss_config_validated": (
            local.oss_config_validated if local is not None else False
        ),
        "oss_validated_at": (
            local.oss_validated_at if local is not None else None
        ),
        "oss_last_error_code": (
            local.oss_last_error_code if local is not None else None
        ),
        "status": upstream.status,
        "enabled": local.enabled if local is not None else False,
        "knowledge_space_id": (
            local.knowledge_space_id if local is not None else None
        ),
        "last_synced_at": (
            local.last_synced_at if local is not None else None
        ),
        "knowledge_resource_count": knowledge_resource_count,
    }


def _knowledge_resource_response(
    resource: KnowledgeResource,
    *,
    space_name: str | None,
) -> dict:
    return {
        "knowledge_resource_id": resource.id,
        "name": resource.name,
        "space_id": resource.space_id,
        "space_name": space_name or resource.space_id,
        "kb_type": resource.kb_type,
        "binding_mode": (
            resource.binding_mode.value if resource.binding_mode is not None else None
        ),
        "sync_status": resource.sync_status.value,
        "enabled": resource.enabled,
        "management_mode": resource.management_mode.value,
    }


def _apply_space_storage(
    local: PolarRAGSpace,
    upstream: PolarRAGSpaceRecord,
) -> bool:
    if (
        local.oss_bucket == upstream.oss_bucket
        and local.oss_endpoint == upstream.oss_endpoint
    ):
        return False
    local.oss_bucket = upstream.oss_bucket
    local.oss_endpoint = upstream.oss_endpoint
    local.oss_config_validated = False
    local.oss_validated_at = None
    local.oss_last_error_code = "OSS_CATALOG_CHANGED"
    return True


def _apply_capability_status(
    instance: PolarRAGInstance,
    capabilities: PolarRAGCapabilities,
) -> None:
    required = (
        capabilities.search
        and capabilities.protected_document_info
        and capabilities.protected_context
        and capabilities.protected_document_search
        and capabilities.space_catalog
        and capabilities.knowledge_base_catalog
    )
    instance.status = (
        PolarRAGInstanceStatus.ACTIVE
        if required
        else PolarRAGInstanceStatus.CAPABILITY_MISSING
    )
    instance.plugin_version = capabilities.version
    instance.capabilities_json = json.dumps(
        capabilities.as_dict(),
        separators=(",", ":"),
        sort_keys=True,
    )
    instance.last_checked_at = datetime.now().astimezone()
    instance.last_error_code = (
        None if required else "POLARRAG_CATALOG_CAPABILITY_MISSING"
    )


def _raise_upstream(error: PolarRAGUpstreamError) -> None:
    status_code = 501 if isinstance(
        error, PolarRAGOperationNotSupported
    ) else 503 if (
        error.retryable
        or error.code == PolarRAGErrorCode.CREDENTIAL_UNAVAILABLE
    ) else 422
    raise HTTPException(
        status_code=status_code,
        detail={"code": error.code.value, "message": error.code.value},
    )


async def _get_instance(
    session: AsyncSession,
    instance_id: str,
) -> PolarRAGInstance:
    instance = await session.get(PolarRAGInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="PolarRAG instance not found")
    return instance


async def _find_space(
    session: AsyncSession,
    instance_id: str,
    space_id: str,
    *,
    enabled: bool | None = None,
) -> PolarRAGSpace | None:
    query = select(PolarRAGSpace).where(
        PolarRAGSpace.polarrag_instance_id == instance_id,
        PolarRAGSpace.space_id == space_id,
    )
    if enabled is not None:
        query = query.where(PolarRAGSpace.enabled.is_(enabled))
    return (await session.execute(query)).scalar_one_or_none()


async def _activate_new_spaces(
    session: AsyncSession,
    instance: PolarRAGInstance,
    client: PolarRAGClient,
) -> tuple[PolarRAGSpace, ...]:
    if instance.status != PolarRAGInstanceStatus.ACTIVE:
        return ()
    activated: list[PolarRAGSpace] = []
    async for upstream_spaces in _iter_upstream_space_pages(client):
        space_ids = [space.space_id for space in upstream_spaces]
        local_spaces = {
            space.space_id: space
            for space in (
                await session.execute(
                    select(PolarRAGSpace).where(
                        PolarRAGSpace.polarrag_instance_id == instance.id,
                        PolarRAGSpace.space_id.in_(space_ids),
                    )
                )
            ).scalars()
        }
        for upstream in upstream_spaces:
            local = local_spaces.get(upstream.space_id)
            if local is not None:
                local.name = upstream.name
                _apply_space_storage(local, upstream)
                continue
            if (
                upstream.status.upper() != "ACTIVE"
                or upstream.identity_domain is None
            ):
                continue
            local = PolarRAGSpace(
                polarrag_instance_id=instance.id,
                space_id=upstream.space_id,
                name=upstream.name,
                identity_domain=upstream.identity_domain,
                oss_bucket=upstream.oss_bucket,
                oss_endpoint=upstream.oss_endpoint,
                enabled=True,
            )
            session.add(local)
            await session.flush()
            await sync_space_catalog(session, local, client, commit=False)
            activated.append(local)
    return tuple(activated)


async def _iter_upstream_space_pages(
    client: PolarRAGClient,
    *,
    page_size: int = 100,
) -> AsyncIterator[list[PolarRAGSpaceRecord]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _page_number in range(_MAX_CATALOG_PAGES):
        spaces, next_cursor = await client.list_spaces_page(
            cursor=cursor,
            page_size=page_size,
        )
        yield spaces
        if next_cursor is None:
            return
        if next_cursor in seen_cursors:
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)


async def _find_upstream_space(
    client: PolarRAGClient,
    space_id: str,
) -> PolarRAGSpaceRecord | None:
    async for spaces in _iter_upstream_space_pages(client):
        for space in spaces:
            if space.space_id == space_id:
                return space
    return None


async def _audit_auto_enabled_spaces(
    session: AsyncSession,
    *,
    user_id: str,
    spaces: tuple[PolarRAGSpace, ...],
) -> None:
    for space in spaces:
        await log_audit(
            session,
            user_id=user_id,
            action="polarrag_space.enable",
            status=AuditStatus.SUCCESS,
            target_type="polarrag_space",
            target_id=space.knowledge_space_id,
            client_info=json.dumps(
                {"mode": "automatic", "space_id": space.space_id},
                sort_keys=True,
                separators=(",", ":"),
            ),
            required=True,
            commit=False,
        )


@router.post("/instances", status_code=201)
async def create_instance(
    body: InstanceCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = PolarRAGInstance(
        name=body.name.strip(),
        scheme=body.scheme,
        host=body.host,
        port=body.port,
        username_ciphertext=encrypt(body.username),
        password_ciphertext=encrypt(body.password),
        tls_verify=body.tls_verify,
        ca_bundle_ciphertext=(
            encrypt(body.ca_bundle) if body.ca_bundle else None
        ),
        status=PolarRAGInstanceStatus.PENDING,
        created_by=admin.id,
    )
    client = client_from_instance(instance)
    try:
        capabilities = await client.check_capabilities()
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    _apply_capability_status(instance, capabilities)
    session.add(instance)
    try:
        await session.flush()
        activated_spaces = await _activate_new_spaces(
            session,
            instance,
            client,
        )
        await _audit_auto_enabled_spaces(
            session,
            user_id=admin.id,
            spaces=activated_spaces,
        )
        await _commit_with_required_audit(
            session,
            user_id=admin.id,
            action="polarrag_instance.create",
            target_type="polarrag_instance",
            target_id=instance.id,
            status=AuditStatus.SUCCESS,
        )
    except PolarRAGUpstreamError as exc:
        await session.rollback()
        _raise_upstream(exc)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="PolarRAG instance name already exists",
        ) from exc
    await session.refresh(instance)
    return _instance_response(instance)


@router.get("/instances")
async def list_instances(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None, max_length=255),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    filters = []
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                PolarRAGInstance.name.ilike(pattern),
                PolarRAGInstance.host.ilike(pattern),
            )
        )
    total = (
        await session.scalar(
            select(func.count(PolarRAGInstance.id)).where(*filters)
        )
        or 0
    )
    instances = (
        await session.execute(
            select(PolarRAGInstance)
            .where(*filters)
            .order_by(PolarRAGInstance.created_at, PolarRAGInstance.id)
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return {
        "items": [_instance_response(item) for item in instances],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.get("/instances/{instance_id}")
async def get_instance(
    instance_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return _instance_response(await _get_instance(session, instance_id))


@router.patch("/instances/{instance_id}")
async def update_instance(
    instance_id: str,
    body: InstanceUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    if body.name is not None:
        instance.name = body.name.strip()
    if body.username is not None:
        instance.username_ciphertext = encrypt(body.username)
    if body.password is not None:
        instance.password_ciphertext = encrypt(body.password)
    if body.tls_verify is not None:
        instance.tls_verify = body.tls_verify
    if "ca_bundle" in body.model_fields_set:
        instance.ca_bundle_ciphertext = (
            encrypt(body.ca_bundle) if body.ca_bundle else None
        )
    client = client_from_instance(instance)
    try:
        capabilities = await client.check_capabilities()
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    _apply_capability_status(instance, capabilities)
    try:
        await session.flush()
        activated_spaces = await _activate_new_spaces(
            session,
            instance,
            client,
        )
        await _audit_auto_enabled_spaces(
            session,
            user_id=admin.id,
            spaces=activated_spaces,
        )
        await _commit_with_required_audit(
            session,
            user_id=admin.id,
            action="polarrag_instance.update",
            target_type="polarrag_instance",
            target_id=instance.id,
            status=AuditStatus.SUCCESS,
        )
    except PolarRAGUpstreamError as exc:
        await session.rollback()
        _raise_upstream(exc)
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Instance conflict") from exc
    await session.refresh(instance)
    return _instance_response(instance)


@router.delete("/instances/{instance_id}", status_code=204)
async def disable_instance(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    instance.status = PolarRAGInstanceStatus.DISABLED
    await session.execute(
        update(PolarRAGSpace)
        .where(PolarRAGSpace.polarrag_instance_id == instance.id)
        .values(enabled=False)
    )
    await session.execute(
        update(KnowledgeResource)
        .where(KnowledgeResource.polarrag_instance_id == instance.id)
        .values(enabled=False)
    )
    await _commit_with_required_audit(
        session,
        user_id=admin.id,
        action="polarrag_instance.disable",
        target_type="polarrag_instance",
        target_id=instance.id,
        status=AuditStatus.SUCCESS,
    )
    return Response(status_code=204)


@router.post("/instances/{instance_id}/check")
async def check_instance(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    client = client_from_instance(instance)
    try:
        capabilities = await client.check_capabilities()
    except PolarRAGUpstreamError as exc:
        instance.status = PolarRAGInstanceStatus.ERROR
        instance.last_error_code = exc.code.value
        await _commit_with_required_audit(
            session,
            user_id=admin.id,
            action="polarrag_instance.check",
            target_type="polarrag_instance",
            target_id=instance.id,
            status=AuditStatus.ERROR,
            error_code=exc.code.value,
        )
        _raise_upstream(exc)
    _apply_capability_status(instance, capabilities)
    try:
        activated_spaces = await _activate_new_spaces(
            session,
            instance,
            client,
        )
        await _audit_auto_enabled_spaces(
            session,
            user_id=admin.id,
            spaces=activated_spaces,
        )
    except PolarRAGUpstreamError as exc:
        await session.rollback()
        _raise_upstream(exc)
    await _commit_with_required_audit(
        session,
        user_id=admin.id,
        action="polarrag_instance.check",
        target_type="polarrag_instance",
        target_id=instance.id,
        status=AuditStatus.SUCCESS,
    )
    return _instance_response(instance)


@router.get("/instances/{instance_id}/spaces")
async def list_spaces(
    instance_id: str,
    cursor: str | None = Query(default=None, max_length=2048),
    limit: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    try:
        spaces, next_cursor = await client_from_instance(
            instance
        ).list_spaces_page(cursor=cursor, page_size=limit)
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    space_ids = [space.space_id for space in spaces]
    local_spaces = {
        space.space_id: space
        for space in (
            await session.execute(
                select(PolarRAGSpace).where(
                    PolarRAGSpace.polarrag_instance_id == instance.id,
                    PolarRAGSpace.space_id.in_(space_ids),
                )
            )
        ).scalars()
    }
    count_rows = (
        await session.execute(
            select(KnowledgeResource.space_id, func.count(KnowledgeResource.id))
            .where(
                KnowledgeResource.polarrag_instance_id == instance.id,
                KnowledgeResource.space_id.in_(space_ids),
            )
            .group_by(KnowledgeResource.space_id)
        )
    ).all()
    resource_counts: dict[str, int] = {
        space_id: count for space_id, count in count_rows
    }
    storage_changed = False
    for upstream in spaces:
        local = local_spaces.get(upstream.space_id)
        if local is not None:
            storage_changed = (
                _apply_space_storage(local, upstream) or storage_changed
            )
    if storage_changed:
        await session.commit()
    return {
        "items": [
            _space_response(
                space,
                local_spaces.get(space.space_id),
                resource_counts.get(space.space_id, 0),
            )
            for space in spaces
        ],
        "next_cursor": next_cursor,
        "limit": limit,
    }


@router.get("/instances/{instance_id}/knowledge-resources")
async def list_knowledge_resources(
    instance_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    filters = [KnowledgeResource.polarrag_instance_id == instance.id]
    total = await session.scalar(select(func.count(KnowledgeResource.id)).where(*filters))
    rows = (
        await session.execute(
            select(KnowledgeResource, PolarRAGSpace.name)
            .outerjoin(
                PolarRAGSpace,
                (PolarRAGSpace.polarrag_instance_id == KnowledgeResource.polarrag_instance_id)
                & (PolarRAGSpace.space_id == KnowledgeResource.space_id),
            )
            .where(*filters)
            .order_by(KnowledgeResource.name, KnowledgeResource.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            _knowledge_resource_response(resource, space_name=space_name)
            for resource, space_name in rows
        ],
        "total": total or 0,
        "offset": offset,
        "limit": limit,
    }


@router.put("/knowledge-resources/{knowledge_resource_id}/management-mode")
async def update_knowledge_resource_management_mode(
    knowledge_resource_id: str,
    body: KnowledgeResourceManagementModeRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    resource = await session.get(KnowledgeResource, knowledge_resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="Knowledge resource not found")
    resource.management_mode = body.management_mode
    await _commit_with_required_audit(
        session,
        user_id=admin.id,
        action="polarrag.knowledge_resource.management_mode_update",
        target_type="knowledge_resource",
        target_id=resource.id,
        status=AuditStatus.SUCCESS,
    )
    return {
        "knowledge_resource_id": resource.id,
        "management_mode": resource.management_mode.value,
    }


@router.post("/instances/{instance_id}/spaces/enable")
async def enable_space(
    instance_id: str,
    body: EnableSpaceRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    client = client_from_instance(instance)
    try:
        upstream = await _find_upstream_space(client, body.space_id)
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    if upstream is None or upstream.status.upper() != "ACTIVE":
        raise HTTPException(status_code=404, detail="PolarRAG Space not available")
    if upstream.identity_domain is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "POLARRAG_IDENTITY_DOMAIN_UNAVAILABLE",
                "message": "PolarRAG Space identity domain is not configured.",
            },
        )
    space = await _find_space(session, instance.id, upstream.space_id)
    if space is None:
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id=upstream.space_id,
            name=upstream.name,
            identity_domain=upstream.identity_domain,
            oss_bucket=upstream.oss_bucket,
            oss_endpoint=upstream.oss_endpoint,
            enabled=True,
        )
        session.add(space)
        await session.flush()
    else:
        if space.identity_domain != upstream.identity_domain:
            raise HTTPException(
                status_code=409,
                detail="PolarRAG Space identity domain changed",
            )
        space.name = upstream.name
        _apply_space_storage(space, upstream)
        space.enabled = True
    try:
        sync_result = await sync_space_catalog(
            session,
            space,
            client,
            commit=False,
        )
    except PolarRAGUpstreamError as exc:
        await session.rollback()
        _raise_upstream(exc)
    await _commit_with_required_audit(
        session,
        user_id=admin.id,
        action="polarrag_space.enable",
        target_type="polarrag_space",
        target_id=space.knowledge_space_id,
        status=AuditStatus.SUCCESS,
    )
    return {
        "knowledge_space_id": space.knowledge_space_id,
        "name": space.name,
        "identity_domain": space.identity_domain,
        "enabled": space.enabled,
        "sync": sync_result,
    }


@router.delete(
    "/instances/{instance_id}/spaces/{space_id}",
    status_code=204,
)
async def disable_space(
    instance_id: str,
    space_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    space = await _find_space(session, instance.id, space_id)
    if space is None:
        raise HTTPException(status_code=404, detail="PolarRAG Space not found")
    space.enabled = False
    await session.execute(
        update(KnowledgeResource)
        .where(
            KnowledgeResource.polarrag_instance_id == instance.id,
            KnowledgeResource.space_id == space.space_id,
        )
        .values(enabled=False)
    )
    await _commit_with_required_audit(
        session,
        user_id=admin.id,
        action="polarrag_space.disable",
        target_type="polarrag_space",
        target_id=space.knowledge_space_id,
        status=AuditStatus.SUCCESS,
    )
    return Response(status_code=204)


@router.put("/spaces/{knowledge_space_id}/oss-config")
async def configure_space_oss(
    knowledge_space_id: str,
    body: OssConfigRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    space = await session.get(PolarRAGSpace, knowledge_space_id)
    if space is None or not space.enabled:
        raise HTTPException(status_code=404, detail="PolarRAG Space not available")
    if not space.oss_bucket or not space.oss_endpoint:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "POLARRAG_OSS_CATALOG_UNAVAILABLE",
                "message": "PolarRAG did not return an OSS bucket and endpoint for this Space.",
            },
        )
    try:
        await validate_oss_write_access(
            endpoint=space.oss_endpoint,
            bucket=space.oss_bucket,
            access_key_id=body.access_key_id,
            access_key_secret=body.access_key_secret,
            object_prefix=body.object_prefix,
        )
    except OssOperationError:
        await log_audit(
            session,
            user_id=admin.id,
            action="polarrag_space.oss_configure",
            target_type="polarrag_space",
            target_id=space.knowledge_space_id,
            status=AuditStatus.ERROR,
            error_code="OSS_VALIDATION_FAILED",
            required=True,
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "OSS_VALIDATION_FAILED",
                "message": "Could not write and delete a validation object in the Space OSS bucket.",
            },
        ) from None
    space.oss_access_key_id_ciphertext = encrypt(body.access_key_id)
    space.oss_access_key_secret_ciphertext = encrypt(body.access_key_secret)
    space.oss_object_prefix = body.object_prefix
    space.oss_config_validated = True
    space.oss_validated_at = datetime.now(UTC)
    space.oss_last_error_code = None
    await _commit_with_required_audit(
        session,
        user_id=admin.id,
        action="polarrag_space.oss_configure",
        target_type="polarrag_space",
        target_id=space.knowledge_space_id,
        status=AuditStatus.SUCCESS,
    )
    return {
        "knowledge_space_id": space.knowledge_space_id,
        "bucket": space.oss_bucket,
        "endpoint": space.oss_endpoint,
        "object_prefix": space.oss_object_prefix,
        "validated": True,
        "validated_at": space.oss_validated_at,
    }


def _space_sync_status(space: PolarRAGSpace) -> SpaceSyncStatus:
    result = None
    if space.catalog_sync_result_json:
        try:
            decoded = json.loads(space.catalog_sync_result_json)
            if isinstance(decoded, dict):
                result = decoded
        except ValueError:
            pass
    return SpaceSyncStatus(
        status=cast(
            Literal["idle", "running", "completed", "failed"],
            space.catalog_sync_status,
        ),
        result=result,
        error=space.catalog_sync_error,
    )


async def _sync_space_in_background(
    session_factory: async_sessionmaker[AsyncSession],
    instance_id: str,
    space_id: str,
    admin_id: str,
    knowledge_space_id: str,
    worker_id: str,
) -> None:
    try:
        result = await run_claimed_space_catalog_sync(
            session_factory,
            knowledge_space_id,
            worker_id,
        )
        if result is not None:
            async with session_factory() as session:
                await _commit_with_required_audit(
                    session,
                    user_id=admin_id,
                    action="polarrag_space.sync",
                    target_type="polarrag_space",
                    target_id=knowledge_space_id,
                    status=AuditStatus.SUCCESS,
                )
    except Exception as exc:
        logger.warning(
            "polarrag.space.background_sync_failed",
            extra={
                "polarrag_instance_id": instance_id,
                "space_id": space_id,
                "error_type": type(exc).__name__,
            },
        )


async def _schedule_space_sync(
    session_factory: async_sessionmaker[AsyncSession],
    session: AsyncSession,
    space: PolarRAGSpace,
    admin_id: str,
) -> tuple[SpaceSyncStatus, asyncio.Task[None] | None]:
    worker_id = str(uuid.uuid4())
    if not await claim_space_catalog_sync(
        session,
        space.knowledge_space_id,
        worker_id,
    ):
        await session.refresh(space)
        return _space_sync_status(space), None
    task = asyncio.create_task(
        _sync_space_in_background(
            session_factory,
            space.polarrag_instance_id,
            space.space_id,
            admin_id,
            space.knowledge_space_id,
            worker_id,
        )
    )
    return SpaceSyncStatus(status="running"), task


@router.post(
    "/instances/{instance_id}/spaces/{space_id}/sync",
    response_model=SpaceSyncStatus,
    status_code=202,
)
async def sync_space(
    instance_id: str,
    space_id: str,
    request: Request,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    space = await _find_space(session, instance.id, space_id, enabled=True)
    if space is None:
        raise HTTPException(status_code=404, detail="Enabled Space not found")
    session_factory = async_sessionmaker(session.bind, expire_on_commit=False)
    status, task = await _schedule_space_sync(
        session_factory,
        session,
        space,
        admin.id,
    )
    if task is not None:
        background_tasks = getattr(request.app.state, "background_tasks", None)
        if background_tasks is not None:
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
    return status


@router.get(
    "/instances/{instance_id}/spaces/{space_id}/sync",
    response_model=SpaceSyncStatus,
)
async def get_space_sync_status(
    instance_id: str,
    space_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    space = await _find_space(session, instance.id, space_id, enabled=True)
    if space is None:
        raise HTTPException(status_code=404, detail="Enabled Space not found")
    return _space_sync_status(space)


def _decode_unclaimed_catalog_cursor(cursor: str | None) -> tuple[int, str | None]:
    if cursor is None:
        return 0, None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        space_offset = payload.get("space_offset")
        upstream_cursor = payload.get("upstream_cursor")
    except (UnicodeDecodeError, ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Invalid unclaimed catalog cursor") from None
    if (
        not isinstance(space_offset, int)
        or space_offset < 0
        or (upstream_cursor is not None and not isinstance(upstream_cursor, str))
    ):
        raise HTTPException(status_code=422, detail="Invalid unclaimed catalog cursor")
    return space_offset, upstream_cursor


def _encode_unclaimed_catalog_cursor(space_offset: int, upstream_cursor: str | None) -> str:
    payload = json.dumps(
        {"space_offset": space_offset, "upstream_cursor": upstream_cursor},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


async def _find_knowledge_base(
    client: PolarRAGClient,
    space_id: str,
    kb_id: str,
    *,
    status: str,
) -> PolarRAGKnowledgeBaseRecord | None:
    cursor: str | None = None
    page_reader = (
        client.list_unclaimed_knowledge_bases_page
        if status == "UNCLAIMED"
        else getattr(client, "list_knowledge_bases_page", None)
    )
    if page_reader is None:
        records = await client.list_knowledge_bases(space_id)
        return next((record for record in records if record.kb_id == kb_id), None)
    seen_cursors: set[str] = set()
    for _page_number in range(_MAX_CATALOG_PAGES):
        records, next_cursor = await page_reader(
            space_id,
            cursor=cursor,
            page_size=100,
        )
        record = next((item for item in records if item.kb_id == kb_id), None)
        if record is not None or next_cursor is None:
            return record
        if next_cursor in seen_cursors:
            raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)


@router.get("/instances/{instance_id}/unclaimed-knowledge-bases")
async def list_unclaimed_knowledge_bases(
    instance_id: str,
    cursor: str | None = None,
    limit: int = Query(default=_UNCLAIMED_KNOWLEDGE_BASE_PAGE_SIZE, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    space_offset, upstream_cursor = _decode_unclaimed_catalog_cursor(cursor)
    client = client_from_instance(instance)
    items: list[dict] = []
    next_cursor: str | None = None
    try:
        while len(items) < limit:
            space = await session.scalar(
                select(PolarRAGSpace)
                .where(
                    PolarRAGSpace.polarrag_instance_id == instance.id,
                    PolarRAGSpace.enabled.is_(True),
                )
                .order_by(PolarRAGSpace.space_id)
                .offset(space_offset)
                .limit(1)
            )
            if space is None:
                break
            records, page_cursor = await client.list_unclaimed_knowledge_bases_page(
                space.space_id,
                cursor=upstream_cursor,
                page_size=limit - len(items),
            )
            for record in records:
                if (
                    record.space_id != space.space_id
                    or record.identity_domain != space.identity_domain
                    or record.kb_type != "PERSONAL"
                ):
                    raise PolarRAGUpstreamError(
                        PolarRAGErrorCode.INVALID_RESPONSE
                    )
                items.append(
                    {
                        "space_id": space.space_id,
                        "space_name": space.name,
                        "identity_domain": space.identity_domain,
                        "kb_id": record.kb_id,
                        "name": record.name,
                        "kb_type": record.kb_type,
                        "status": record.status,
                    }
                )
            if page_cursor is not None:
                next_cursor = _encode_unclaimed_catalog_cursor(space_offset, page_cursor)
                break
            space_offset += 1
            upstream_cursor = None
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    if next_cursor is None:
        next_space = await session.scalar(
            select(PolarRAGSpace.knowledge_space_id)
            .where(
                PolarRAGSpace.polarrag_instance_id == instance.id,
                PolarRAGSpace.enabled.is_(True),
            )
            .order_by(PolarRAGSpace.space_id)
            .offset(space_offset)
            .limit(1)
        )
        if next_space is not None:
            next_cursor = _encode_unclaimed_catalog_cursor(space_offset, None)
    return {
        "items": items,
        "next_cursor": next_cursor,
        "owner_candidates": [],
    }


@router.get("/instances/{instance_id}/owner-candidates")
async def list_owner_candidates(
    instance_id: str,
    identity_domain: str = Query(min_length=1, max_length=255),
    search: str | None = Query(default=None, min_length=1, max_length=255),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    domain_exists = await session.scalar(
        select(PolarRAGSpace.knowledge_space_id)
        .where(
            PolarRAGSpace.polarrag_instance_id == instance.id,
            PolarRAGSpace.enabled.is_(True),
            PolarRAGSpace.identity_domain == identity_domain,
        )
        .limit(1)
    )
    if domain_exists is None:
        raise HTTPException(status_code=404, detail="Enabled identity domain not found")
    current = datetime.now(UTC)
    assigned = (
        select(
            EnterprisePrincipalAssignment.id.label("principal_assignment_id"),
            User.id.label("pas_user_id"),
            User.display_name.label("user_name"),
            User.external_id.label("user_external_id"),
            EnterprisePrincipalAssignment.identity_domain.label("identity_domain"),
            EnterprisePrincipalAssignment.provider.label("provider"),
            EnterprisePrincipalAssignment.principal_id.label("principal_id"),
        )
        .join(User, User.id == EnterprisePrincipalAssignment.pas_user_id)
        .where(
            EnterprisePrincipalAssignment.identity_domain == identity_domain,
            EnterprisePrincipalAssignment.provider.in_(("feishu", "sharepoint")),
            EnterprisePrincipalAssignment.principal_type == EnterprisePrincipalType.USER,
            EnterprisePrincipalAssignment.status == EnterprisePrincipalStatus.ACTIVE,
            or_(
                EnterprisePrincipalAssignment.valid_until.is_(None),
                EnterprisePrincipalAssignment.valid_until > current,
            ),
            User.status == UserStatus.ACTIVE,
        )
    )
    native = select(
        literal(None).label("principal_assignment_id"),
        User.id.label("pas_user_id"),
        User.display_name.label("user_name"),
        User.external_id.label("user_external_id"),
        literal(identity_domain).label("identity_domain"),
        literal("polarrag").label("provider"),
        User.external_id.label("principal_id"),
    ).where(User.status == UserStatus.ACTIVE)
    normalized_search = (search or "").strip()
    if normalized_search:
        pattern = f"%{normalized_search}%"
        assigned = assigned.where(
            or_(
                User.display_name.ilike(pattern),
                User.external_id.ilike(pattern),
                EnterprisePrincipalAssignment.provider.ilike(pattern),
                EnterprisePrincipalAssignment.principal_id.ilike(pattern),
            )
        )
        native = native.where(
            or_(User.display_name.ilike(pattern), User.external_id.ilike(pattern))
        )
    candidates = union_all(assigned, native).subquery()
    total = int(await session.scalar(select(func.count()).select_from(candidates)) or 0)
    rows = (
        await session.execute(
            select(candidates)
            .order_by(
                candidates.c.user_name,
                candidates.c.user_external_id,
                candidates.c.provider,
                candidates.c.principal_id,
            )
            .offset(offset)
            .limit(limit)
        )
    ).mappings().all()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post(
    "/instances/{instance_id}/spaces/{space_id}"
    "/knowledge-bases/{kb_id}/claim"
)
async def claim_knowledge_base(
    instance_id: str,
    space_id: str,
    kb_id: str,
    body: ClaimKnowledgeBaseRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    space = await _find_space(session, instance.id, space_id, enabled=True)
    if space is None:
        raise HTTPException(status_code=404, detail="Enabled Space not found")
    current = datetime.now(UTC)
    assignment: EnterprisePrincipalAssignment | None = None
    if body.principal_assignment_id is not None:
        owner = (
            await session.execute(
                select(EnterprisePrincipalAssignment, User)
                .join(User, User.id == EnterprisePrincipalAssignment.pas_user_id)
                .where(
                    EnterprisePrincipalAssignment.id == body.principal_assignment_id,
                    EnterprisePrincipalAssignment.identity_domain == space.identity_domain,
                    EnterprisePrincipalAssignment.principal_type == EnterprisePrincipalType.USER,
                    EnterprisePrincipalAssignment.status == EnterprisePrincipalStatus.ACTIVE,
                    or_(
                        EnterprisePrincipalAssignment.valid_until.is_(None),
                        EnterprisePrincipalAssignment.valid_until > current,
                    ),
                    User.status == UserStatus.ACTIVE,
                )
            )
        ).one_or_none()
        if owner is None or not principal_assignment_is_valid_for_user(*owner):
            raise HTTPException(
                status_code=422,
                detail="Eligible KB owner principal not found",
            )
        assignment, user = owner
    else:
        assert body.pas_user_id is not None
        user = await session.get(User, body.pas_user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            raise HTTPException(
                status_code=422,
                detail="Eligible KB owner principal not found",
            )
    client = client_from_instance(instance)
    try:
        record = await _find_knowledge_base(
            client,
            space.space_id,
            kb_id,
            status="UNCLAIMED",
        )
        if record is not None:
            if (
                record.space_id != space.space_id
                or record.identity_domain != space.identity_domain
                or record.kb_type != "PERSONAL"
                or record.status.upper() != "UNCLAIMED"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Knowledge base is not awaiting owner assignment",
                )
            await client.claim_knowledge_base(
                space.space_id,
                kb_id,
                owner=user.external_id,
            )
        else:
            record = await _find_knowledge_base(
                client,
                space.space_id,
                kb_id,
                status="ACTIVE",
            )
            expected_owners = {("polarrag", user.external_id)}
            if assignment is not None:
                expected_owners.add((assignment.provider, assignment.principal_id))
            owner_value = record.owner if record is not None else None
            if (
                record is None
                or record.space_id != space.space_id
                or record.identity_domain != space.identity_domain
                or record.kb_type != "PERSONAL"
                or record.status.upper() != "ACTIVE"
                or not isinstance(owner_value, dict)
                or owner_value.get("type")
                != EnterprisePrincipalType.USER.value
                or (owner_value.get("provider"), owner_value.get("id"))
                not in expected_owners
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Knowledge base is not awaiting owner assignment",
                )
        sync_result = await sync_space_catalog(
            session,
            space,
            client,
            commit=False,
        )
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    resource = (
        await session.execute(
            select(KnowledgeResource).where(
                KnowledgeResource.polarrag_instance_id == instance.id,
                KnowledgeResource.space_id == space.space_id,
                KnowledgeResource.kb_id == kb_id,
            )
        )
    ).scalar_one_or_none()
    await _commit_with_required_audit(
        session,
        user_id=admin.id,
        action="polarrag.knowledge_base.claim",
        target_type="polarrag_knowledge_base",
        target_id=f"{space.space_id}/{kb_id}",
        status=AuditStatus.SUCCESS,
        client_info=json.dumps(
            {
                "knowledge_resource_ids": [resource.id] if resource else [],
                "polarrag_instance_ids": [instance.id],
                "space_ids": [space.space_id],
                "kb_ids": [kb_id],
                "owner_pas_user_id": user.id,
                "polarrag_status": "success",
            },
            separators=(",", ":"),
        ),
    )
    return {"kb_id": kb_id, "status": "ACTIVE", "sync": sync_result}


@router.post("/users/{user_id}/principals", status_code=201)
async def create_principal(
    user_id: str,
    body: PrincipalCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if body.provider == "polarrag" and (
        body.principal_type != "user"
        or body.principal_id != user.external_id
    ):
        raise HTTPException(
            status_code=422,
            detail="PolarRAG principal must match the PAS user",
        )
    assignment = EnterprisePrincipalAssignment.create(
        pas_user_id=user_id,
        identity_domain=body.identity_domain,
        provider=body.provider,
        principal_type=EnterprisePrincipalType(body.principal_type),
        principal_id=(
            user.external_id
            if body.provider == "polarrag"
            else body.principal_id
        ),
        source=EnterprisePrincipalSource.ADMIN_MANAGED,
        valid_until=body.valid_until,
        canonical_user_external_id=user.external_id,
    )
    session.add(assignment)
    try:
        await session.flush()
        await _commit_with_required_audit(
            session,
            user_id=admin.id,
            action="enterprise_principal.create",
            target_type="enterprise_principal",
            target_id=assignment.id,
            status=AuditStatus.SUCCESS,
        )
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Enterprise principal assignment conflicts",
        ) from exc
    await session.refresh(assignment)
    return _principal_response(assignment)


@router.get("/users/{user_id}/principals")
async def list_principals(
    user_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None, max_length=255),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    filters = [EnterprisePrincipalAssignment.pas_user_id == user_id]
    if q and q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(
            or_(
                EnterprisePrincipalAssignment.principal_id.ilike(pattern),
                EnterprisePrincipalAssignment.identity_domain.ilike(pattern),
            )
        )
    total = (
        await session.scalar(
            select(func.count(EnterprisePrincipalAssignment.id)).where(*filters)
        )
        or 0
    )
    assignments = (
        await session.execute(
            select(EnterprisePrincipalAssignment)
            .where(*filters)
            .order_by(
                EnterprisePrincipalAssignment.created_at,
                EnterprisePrincipalAssignment.id,
            )
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return {
        "items": [_principal_response(item) for item in assignments],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.patch("/users/{user_id}/principals/{principal_id}")
async def update_principal(
    user_id: str,
    principal_id: str,
    body: PrincipalUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    assignment = await session.get(
        EnterprisePrincipalAssignment,
        principal_id,
    )
    if assignment is None or assignment.pas_user_id != user_id:
        raise HTTPException(status_code=404, detail="Principal not found")
    if body.status is not None:
        assignment.status = EnterprisePrincipalStatus(body.status)
    if "valid_until" in body.model_fields_set:
        assignment.valid_until = body.valid_until
    await session.flush()
    await _commit_with_required_audit(
        session,
        user_id=admin.id,
        action="enterprise_principal.update",
        target_type="enterprise_principal",
        target_id=assignment.id,
        status=AuditStatus.SUCCESS,
    )
    await session.refresh(assignment)
    return _principal_response(assignment)


@router.delete(
    "/users/{user_id}/principals/{principal_id}",
    status_code=204,
)
async def delete_principal(
    user_id: str,
    principal_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    assignment = await session.get(
        EnterprisePrincipalAssignment,
        principal_id,
    )
    if assignment is None or assignment.pas_user_id != user_id:
        raise HTTPException(status_code=404, detail="Principal not found")
    await session.delete(assignment)
    await _commit_with_required_audit(
        session,
        user_id=admin.id,
        action="enterprise_principal.delete",
        target_type="enterprise_principal",
        target_id=principal_id,
        status=AuditStatus.SUCCESS,
    )
    return Response(status_code=204)
