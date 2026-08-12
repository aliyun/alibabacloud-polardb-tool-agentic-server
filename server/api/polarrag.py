from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    User,
    UserStatus,
)
from server.polarrag.catalog import sync_space_catalog
from server.polarrag.client import client_from_instance
from server.polarrag.contracts import (
    PolarRAGCapabilities,
    PolarRAGOperationNotSupported,
    PolarRAGSpaceRecord,
    PolarRAGUpstreamError,
)
from server.polarrag.identity import resolve_acl_context
from server.polarrag.oss import (
    OssOperationError,
    normalize_prefix,
    validate_oss_write_access,
)

router = APIRouter(prefix="/polarrag", tags=["polarrag"])
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class InstanceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    scheme: Literal["http", "https"]
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535, strict=True)
    username: str = Field(min_length=1, max_length=1024)
    password: str = Field(min_length=1, max_length=4096)
    tls_verify: bool = True
    ca_bundle: str | None = Field(default=None, max_length=262144)

    @field_validator("name", "username")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("value must not be blank")
        return candidate

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        candidate = value.strip()
        if (
            not _HOST_RE.fullmatch(candidate)
            or "/" in candidate
            or "@" in candidate
        ):
            raise ValueError("host must not contain a URL or credentials")
        return candidate


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
    provider: Literal["feishu", "sharepoint"]
    principal_type: Literal["user", "group"]
    principal_id: str = Field(min_length=1, max_length=255)
    valid_until: datetime | None = None

    @field_validator("identity_domain", "principal_id")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("value must not be blank")
        return candidate


class PrincipalUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "disabled"] | None = None
    valid_until: datetime | None = None


class ClaimKnowledgeBaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_assignment_id: str = Field(min_length=1, max_length=36)


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
    resources: list[KnowledgeResource],
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
        "knowledge_resources": [
            {
                "knowledge_resource_id": resource.id,
                "name": resource.name,
                "kb_type": resource.kb_type,
                "binding_mode": (
                    resource.binding_mode.value
                    if resource.binding_mode is not None
                    else None
                ),
                "sync_status": resource.sync_status.value,
                "enabled": resource.enabled,
            }
            for resource in resources
        ],
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
    ) else 503 if error.retryable else 422
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
    try:
        capabilities = await client_from_instance(
            instance
        ).check_capabilities()
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    _apply_capability_status(instance, capabilities)
    session.add(instance)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="PolarRAG instance name already exists",
        ) from exc
    await session.refresh(instance)
    await log_audit(
        session,
        user_id=admin.id,
        action="polarrag_instance.create",
        target_type="polarrag_instance",
        target_id=instance.id,
        status=AuditStatus.SUCCESS,
        required=True,
    )
    return _instance_response(instance)


@router.get("/instances")
async def list_instances(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instances = (
        await session.execute(
            select(PolarRAGInstance).order_by(PolarRAGInstance.created_at)
        )
    ).scalars()
    return {"items": [_instance_response(item) for item in instances]}


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
    try:
        capabilities = await client_from_instance(
            instance
        ).check_capabilities()
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    _apply_capability_status(instance, capabilities)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Instance conflict") from exc
    await session.refresh(instance)
    await log_audit(
        session,
        user_id=admin.id,
        action="polarrag_instance.update",
        target_type="polarrag_instance",
        target_id=instance.id,
        status=AuditStatus.SUCCESS,
        required=True,
    )
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
    await session.commit()
    await log_audit(
        session,
        user_id=admin.id,
        action="polarrag_instance.disable",
        target_type="polarrag_instance",
        target_id=instance.id,
        status=AuditStatus.SUCCESS,
        required=True,
    )
    return Response(status_code=204)


@router.post("/instances/{instance_id}/check")
async def check_instance(
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    try:
        capabilities = await client_from_instance(
            instance
        ).check_capabilities()
    except PolarRAGUpstreamError as exc:
        instance.status = PolarRAGInstanceStatus.ERROR
        instance.last_error_code = exc.code.value
        await session.commit()
        await log_audit(
            session,
            user_id=admin.id,
            action="polarrag_instance.check",
            target_type="polarrag_instance",
            target_id=instance.id,
            status=AuditStatus.ERROR,
            error_code=exc.code.value,
            required=True,
        )
        _raise_upstream(exc)
    _apply_capability_status(instance, capabilities)
    await session.commit()
    await log_audit(
        session,
        user_id=admin.id,
        action="polarrag_instance.check",
        target_type="polarrag_instance",
        target_id=instance.id,
        status=AuditStatus.SUCCESS,
        required=True,
    )
    return _instance_response(instance)


@router.get("/instances/{instance_id}/spaces")
async def list_spaces(
    instance_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    try:
        spaces = await client_from_instance(instance).list_spaces()
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    local_spaces = {
        space.space_id: space
        for space in (
            await session.execute(
                select(PolarRAGSpace).where(
                    PolarRAGSpace.polarrag_instance_id == instance.id
                )
            )
        ).scalars()
    }
    resources_by_space: dict[str, list[KnowledgeResource]] = {}
    resources = (
        await session.execute(
            select(KnowledgeResource)
            .where(KnowledgeResource.polarrag_instance_id == instance.id)
            .order_by(KnowledgeResource.name, KnowledgeResource.id)
        )
    ).scalars()
    for resource in resources:
        resources_by_space.setdefault(resource.space_id, []).append(resource)
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
                resources_by_space.get(space.space_id, []),
            )
            for space in spaces
        ]
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
        upstream_spaces = await client.list_spaces()
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    upstream = next(
        (item for item in upstream_spaces if item.space_id == body.space_id),
        None,
    )
    if upstream is None or upstream.status.upper() != "ACTIVE":
        raise HTTPException(status_code=404, detail="PolarRAG Space not available")
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
        sync_result = await sync_space_catalog(session, space, client)
    except PolarRAGUpstreamError as exc:
        await session.rollback()
        _raise_upstream(exc)
    await log_audit(
        session,
        user_id=admin.id,
        action="polarrag_space.enable",
        target_type="polarrag_space",
        target_id=space.knowledge_space_id,
        status=AuditStatus.SUCCESS,
        required=True,
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
    await session.commit()
    await log_audit(
        session,
        user_id=admin.id,
        action="polarrag_space.disable",
        target_type="polarrag_space",
        target_id=space.knowledge_space_id,
        status=AuditStatus.SUCCESS,
        required=True,
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
    await session.commit()
    await log_audit(
        session,
        user_id=admin.id,
        action="polarrag_space.oss_configure",
        target_type="polarrag_space",
        target_id=space.knowledge_space_id,
        status=AuditStatus.SUCCESS,
        required=True,
    )
    return {
        "knowledge_space_id": space.knowledge_space_id,
        "bucket": space.oss_bucket,
        "endpoint": space.oss_endpoint,
        "object_prefix": space.oss_object_prefix,
        "validated": True,
        "validated_at": space.oss_validated_at,
    }


@router.post("/instances/{instance_id}/spaces/{space_id}/sync")
async def sync_space(
    instance_id: str,
    space_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    space = await _find_space(session, instance.id, space_id, enabled=True)
    if space is None:
        raise HTTPException(status_code=404, detail="Enabled Space not found")
    try:
        result = await sync_space_catalog(
            session,
            space,
            client_from_instance(instance),
        )
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    await log_audit(
        session,
        user_id=admin.id,
        action="polarrag_space.sync",
        target_type="polarrag_space",
        target_id=space.knowledge_space_id,
        status=AuditStatus.SUCCESS,
        required=True,
    )
    return result


@router.get("/instances/{instance_id}/unclaimed-knowledge-bases")
async def list_unclaimed_knowledge_bases(
    instance_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    instance = await _get_instance(session, instance_id)
    spaces = list(
        (
            await session.execute(
                select(PolarRAGSpace)
                .where(
                    PolarRAGSpace.polarrag_instance_id == instance.id,
                    PolarRAGSpace.enabled.is_(True),
                )
                .order_by(PolarRAGSpace.space_id)
            )
        ).scalars()
    )
    client = client_from_instance(instance)
    items: list[dict] = []
    try:
        for space in spaces:
            records = await client.list_unclaimed_knowledge_bases(
                space.space_id
            )
            for record in records:
                if (
                    record.space_id != space.space_id
                    or record.identity_domain != space.identity_domain
                    or record.kb_type != "PERSONAL"
                ):
                    raise ValueError(
                        "PolarRAG catalog identity boundary mismatch"
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
    except PolarRAGUpstreamError as exc:
        _raise_upstream(exc)
    domains = {space.identity_domain for space in spaces}
    current = datetime.now(UTC)
    candidates: list[tuple[EnterprisePrincipalAssignment, User]] = []
    if domains:
        candidates = [
            (assignment, user)
            for assignment, user in (
                await session.execute(
                    select(EnterprisePrincipalAssignment, User)
                    .join(
                        User,
                        User.id
                        == EnterprisePrincipalAssignment.pas_user_id,
                    )
                    .where(
                        EnterprisePrincipalAssignment.identity_domain.in_(
                            domains
                        ),
                        EnterprisePrincipalAssignment.principal_type
                        == EnterprisePrincipalType.USER,
                        EnterprisePrincipalAssignment.status
                        == EnterprisePrincipalStatus.ACTIVE,
                        or_(
                            EnterprisePrincipalAssignment.valid_until.is_(
                                None
                            ),
                            EnterprisePrincipalAssignment.valid_until
                            > current,
                        ),
                        User.status == UserStatus.ACTIVE,
                    )
                    .order_by(
                        User.display_name,
                        EnterprisePrincipalAssignment.identity_domain,
                        EnterprisePrincipalAssignment.provider,
                        EnterprisePrincipalAssignment.principal_id,
                    )
                )
            ).all()
        ]
    return {
        "items": items,
        "owner_candidates": [
            {
                "principal_assignment_id": assignment.id,
                "pas_user_id": user.id,
                "user_name": user.display_name,
                "identity_domain": assignment.identity_domain,
                "provider": assignment.provider,
                "principal_id": assignment.principal_id,
            }
            for assignment, user in candidates
        ],
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
    owner = (
        await session.execute(
            select(EnterprisePrincipalAssignment, User)
            .join(User, User.id == EnterprisePrincipalAssignment.pas_user_id)
            .where(
                EnterprisePrincipalAssignment.id
                == body.principal_assignment_id,
                EnterprisePrincipalAssignment.identity_domain
                == space.identity_domain,
                EnterprisePrincipalAssignment.principal_type
                == EnterprisePrincipalType.USER,
                EnterprisePrincipalAssignment.status
                == EnterprisePrincipalStatus.ACTIVE,
                or_(
                    EnterprisePrincipalAssignment.valid_until.is_(None),
                    EnterprisePrincipalAssignment.valid_until > current,
                ),
                User.status == UserStatus.ACTIVE,
            )
        )
    ).one_or_none()
    if owner is None:
        raise HTTPException(
            status_code=422,
            detail="Eligible KB owner principal not found",
        )
    assignment, user = owner
    client = client_from_instance(instance)
    try:
        pending = await client.list_unclaimed_knowledge_bases(space.space_id)
        record = next((item for item in pending if item.kb_id == kb_id), None)
        if (
            record is None
            or record.space_id != space.space_id
            or record.identity_domain != space.identity_domain
            or record.kb_type != "PERSONAL"
        ):
            raise HTTPException(
                status_code=409,
                detail="Knowledge base is not awaiting owner assignment",
            )
        acl_context = await resolve_acl_context(
            session,
            user.id,
            space.identity_domain,
            now=current,
        )
        actor = {
            "provider": assignment.provider,
            "type": EnterprisePrincipalType.USER.value,
            "id": assignment.principal_id,
        }
        acl_context["actor"] = actor
        await client.claim_knowledge_base(
            space.space_id,
            kb_id,
            acl_context=acl_context,
        )
        sync_result = await sync_space_catalog(session, space, client)
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
    await log_audit(
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
        required=True,
    )
    return {"kb_id": kb_id, "status": "ACTIVE", "sync": sync_result}


@router.post("/users/{user_id}/principals", status_code=201)
async def create_principal(
    user_id: str,
    body: PrincipalCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if await session.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    assignment = EnterprisePrincipalAssignment.create(
        pas_user_id=user_id,
        identity_domain=body.identity_domain,
        provider=body.provider,
        principal_type=EnterprisePrincipalType(body.principal_type),
        principal_id=body.principal_id,
        source=EnterprisePrincipalSource.ADMIN_MANAGED,
        valid_until=body.valid_until,
    )
    session.add(assignment)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Enterprise principal assignment conflicts",
        ) from exc
    await session.refresh(assignment)
    await log_audit(
        session,
        user_id=admin.id,
        action="enterprise_principal.create",
        target_type="enterprise_principal",
        target_id=assignment.id,
        status=AuditStatus.SUCCESS,
        required=True,
    )
    return _principal_response(assignment)


@router.get("/users/{user_id}/principals")
async def list_principals(
    user_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    assignments = (
        await session.execute(
            select(EnterprisePrincipalAssignment)
            .where(EnterprisePrincipalAssignment.pas_user_id == user_id)
            .order_by(EnterprisePrincipalAssignment.created_at)
        )
    ).scalars()
    return {
        "items": [_principal_response(item) for item in assignments]
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
    await session.commit()
    await session.refresh(assignment)
    await log_audit(
        session,
        user_id=admin.id,
        action="enterprise_principal.update",
        target_type="enterprise_principal",
        target_id=assignment.id,
        status=AuditStatus.SUCCESS,
        required=True,
    )
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
    await session.commit()
    await log_audit(
        session,
        user_id=admin.id,
        action="enterprise_principal.delete",
        target_type="enterprise_principal",
        target_id=principal_id,
        status=AuditStatus.SUCCESS,
        required=True,
    )
    return Response(status_code=204)
