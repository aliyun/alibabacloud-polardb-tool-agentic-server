from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from server.api.configuration import get_config_service, resolve_config_actor
from server.auth.dependencies import get_current_user, require_admin
from server.configuration.service import ConfigService
from server.configuration.types import ConfigActor, ConfigError
from server.features.knowledge import KnowledgeRuntime, validate_read_access
from server.models import User
from server.polarrag.connection_config import InstanceCreate

router = APIRouter(prefix="/features", tags=["features"])


def feature_runtime(request: Request) -> KnowledgeRuntime:
    value = getattr(request.app.state, "knowledge_runtime", None)
    if value is None:
        raise HTTPException(503, detail={"code": "FEATURE_STATUS_UNAVAILABLE"})
    return value


async def writable_feature(
    feature: KnowledgeRuntime = Depends(feature_runtime),
    actor: ConfigActor = Depends(resolve_config_actor),
) -> KnowledgeRuntime:
    if actor.actor_type != "admin":
        raise HTTPException(403, detail={"code": "ADMIN_REQUIRED"})
    return feature


@router.get("")
async def capabilities(_user: User = Depends(get_current_user), feature=Depends(feature_runtime)):
    return {"knowledge": {"available": await feature.available()}}


@router.get("/knowledge")
async def knowledge_status(_admin: User = Depends(require_admin), feature=Depends(feature_runtime)):
    return await feature.status()


@router.post("/knowledge/drain")
async def drain(feature=Depends(writable_feature)):
    await feature.state.begin_drain()
    return await feature.status()


class RevisionRequest(BaseModel):
    expected_revision: int = Field(ge=1)


@router.post("/knowledge/cancel-drain")
async def cancel_drain(body: RevisionRequest, feature=Depends(writable_feature)):
    try:
        await feature.state.cancel_drain(body.expected_revision)
    except ConfigError as error:
        raise HTTPException(409, detail={"code": error.code, "message": error.message}) from error
    return await feature.status()


class ActivationRequest(RevisionRequest):
    replica_ids: list[str] = Field(min_length=1, max_length=100)


@router.post("/knowledge/confirm-activation")
async def confirm_activation(body: ActivationRequest, feature=Depends(writable_feature)):
    if feature.managed:
        raise HTTPException(
            409,
            detail={
                "code": "KNOWLEDGE_MANAGED_BY_CONTROL_PLANE",
                "message": "Only the control plane can confirm managed replica readiness",
            },
        )
    try:
        await feature.state.confirm_activation(body.expected_revision, body.replica_ids)
    except ConfigError as error:
        raise HTTPException(409, detail={"code": error.code, "message": error.message}) from error
    return await feature.status()


@router.get("/knowledge/options")
async def options(_admin: User = Depends(require_admin), service: ConfigService = Depends(get_config_service)):
    from server.models import KnowledgeResource, PolarRAGInstance, UserStatus

    async with service.repository.session_factory() as session:
        instances = (await session.scalars(select(PolarRAGInstance).order_by(PolarRAGInstance.name))).all()
        resources = (
            await session.scalars(
                select(KnowledgeResource)
                .where(KnowledgeResource.enabled.is_(True))
                .order_by(KnowledgeResource.name)
                .limit(500)
            )
        ).all()
        users = (
            await session.scalars(
                select(User).where(User.status == UserStatus.ACTIVE).order_by(User.display_name).limit(500)
            )
        ).all()
    return {
        "connections": [{"id": i.id, "name": i.name, "status": i.status.value} for i in instances],
        "resources": [
            {"id": r.id, "name": r.name, "instance_id": r.polarrag_instance_id, "space_id": r.space_id}
            for r in resources
        ],
        "users": [{"id": u.id, "name": u.display_name, "external_id": u.external_id} for u in users],
    }


@router.post("/knowledge/connections", status_code=201)
async def save_connection(
    body: InstanceCreate, admin: User = Depends(require_admin), feature=Depends(writable_feature)
):
    from server.core.crypto import encrypt
    from server.core.audit_logger import log_audit
    from server.models import AuditStatus, PolarRAGInstance, PolarRAGInstanceStatus

    instance = PolarRAGInstance(
        name=body.name,
        scheme=body.scheme,
        host=body.host,
        port=body.port,
        username_ciphertext=encrypt(body.username),
        password_ciphertext=encrypt(body.password),
        tls_verify=body.tls_verify,
        ca_bundle_ciphertext=encrypt(body.ca_bundle) if body.ca_bundle else None,
        status=PolarRAGInstanceStatus.PENDING,
        created_by=admin.id,
    )
    async with feature.state.repository.session_factory() as session:
        session.add(instance)
        try:
            await session.flush()
            await log_audit(
                session,
                user_id=admin.id,
                action="knowledge.connection.save",
                target_type="polarrag_instance",
                target_id=instance.id,
                status=AuditStatus.SUCCESS,
                required=True,
                commit=False,
            )
            await session.commit()
        except IntegrityError:
            raise HTTPException(
                409, detail={"code": "CONNECTION_NAME_EXISTS", "message": "Choose a unique connection name"}
            ) from None
    return {"id": instance.id, "name": instance.name, "status": instance.status.value}


@router.post("/knowledge/connections/{instance_id}/check")
async def check_connection(instance_id: str, feature=Depends(writable_feature)):
    from server.models import PolarRAGInstance, PolarRAGInstanceStatus
    from server.polarrag.client import client_from_instance
    from server.polarrag.contracts import PolarRAGUpstreamError

    async with feature.state.repository.session_factory() as session:
        instance = await session.get(PolarRAGInstance, instance_id)
        if instance is None:
            raise HTTPException(404)
        try:
            client = client_from_instance(instance)
            capabilities = await client.check_capabilities()
            if not all(v for k, v in capabilities.as_dict().items() if k != "version"):
                raise HTTPException(422, detail={"code": "KNOWLEDGE_CAPABILITY_MISSING"})
            spaces = await client.list_spaces()
        except PolarRAGUpstreamError as error:
            raise HTTPException(
                422, detail={"code": error.code.value, "message": "Unable to verify upstream knowledge APIs"}
            ) from error
        instance.status = PolarRAGInstanceStatus.ACTIVE
        instance.plugin_version = capabilities.version
        instance.capabilities_json = json.dumps(capabilities.as_dict())
        instance.last_checked_at = datetime.now(UTC)
        instance.last_error_code = None
        await session.commit()
    return {
        "spaces": [
            {"id": s.space_id, "name": s.name, "identity_domain": s.identity_domain}
            for s in spaces
            if s.status.upper() == "ACTIVE" and s.identity_domain
        ]
    }


class SpaceRequest(BaseModel):
    space_id: str = Field(min_length=1, max_length=255)


@router.post("/knowledge/connections/{instance_id}/spaces")
async def prepare_space(
    instance_id: str, body: SpaceRequest, admin: User = Depends(require_admin), feature=Depends(writable_feature)
):
    from server.core.audit_logger import log_audit
    from server.models import AuditStatus, PolarRAGInstance, PolarRAGInstanceStatus, PolarRAGSpace
    from server.polarrag.client import client_from_instance
    from server.polarrag.catalog import sync_space_catalog
    from server.polarrag.contracts import PolarRAGUpstreamError

    async with feature.state.repository.session_factory() as session:
        instance = await session.get(PolarRAGInstance, instance_id)
        if instance is None or instance.status != PolarRAGInstanceStatus.ACTIVE:
            raise HTTPException(409, detail={"code": "CHECK_CONNECTION_FIRST"})
        client = client_from_instance(instance)
        try:
            upstream = next((s for s in await client.list_spaces() if s.space_id == body.space_id), None)
            if upstream is None or not upstream.identity_domain or upstream.status.upper() != "ACTIVE":
                raise HTTPException(422, detail={"code": "KNOWLEDGE_SPACE_UNAVAILABLE"})
            local = await session.scalar(
                select(PolarRAGSpace).where(
                    PolarRAGSpace.polarrag_instance_id == instance_id, PolarRAGSpace.space_id == body.space_id
                )
            )
            if local is None:
                local = PolarRAGSpace(
                    polarrag_instance_id=instance_id,
                    space_id=upstream.space_id,
                    name=upstream.name,
                    identity_domain=upstream.identity_domain,
                    enabled=True,
                )
                session.add(local)
                await session.flush()
            local.enabled = True
            await sync_space_catalog(session, local, client, commit=False)
            await log_audit(
                session,
                user_id=admin.id,
                action="knowledge.space.prepare",
                target_type="polarrag_space",
                target_id=local.knowledge_space_id,
                status=AuditStatus.SUCCESS,
                required=True,
                commit=False,
            )
            await session.commit()
        except PolarRAGUpstreamError as error:
            raise HTTPException(
                422, detail={"code": error.code.value, "message": "Unable to read this knowledge space"}
            ) from error
    return {"prepared": True}


class ReadTestRequest(BaseModel):
    validation_resource_id: str = Field(min_length=1, max_length=36)
    validation_user_id: str = Field(min_length=1, max_length=36)


@router.post("/knowledge/test")
async def read_test(body: ReadTestRequest, feature=Depends(writable_feature)):
    try:
        await validate_read_access(feature.state.repository, body.model_dump())
    except ConfigError as error:
        raise HTTPException(422, detail={"code": error.code, "message": error.message}) from error
    return {"verified": True}
