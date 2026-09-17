from __future__ import annotations
import json
import uuid
from dataclasses import asdict
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from server.auth.dependencies import require_admin
from server.core.agent_enterprise_access_service import (
    EnterpriseAccessAuditError,
    EnterpriseAccessPreviewStaleError,
    EnterpriseAccessSelection,
    EnterpriseAccessValidationError,
    apply_enterprise_access,
    preview_enterprise_access,
)
from server.core.resource_write_guard import serialized_resource_write
from server.db.engine import get_session
from server.models import (
    AgentPolarRAGInstanceBinding,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGSpace,
    User,
)
from server.api.agent_polarrag_access import _require_agent, _audit

router = APIRouter(prefix="/agents", tags=["agent-knowledge-access"])


class PolarRAGBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    polarrag_instance_id: str


class PolarRAGPublicScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    public_knowledge_resource_ids: list[str] | None


class EnterpriseAccessSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_source_id: str = Field(min_length=1, max_length=36)
    all_synced_users: bool = False
    directory_group_ids: list[str] = Field(default_factory=list, max_length=500)
    pas_user_ids: list[str] = Field(default_factory=list, max_length=500)
    polarrag_instance_ids: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=200,
    )
    knowledge_space_ids: list[str] = Field(min_length=1, max_length=200)


class EnterpriseAccessApplyRequest(EnterpriseAccessSelectionRequest):
    preview_hash: str


class PolarRAGBindingResponse(BaseModel):
    id: str
    polarrag_instance_id: str
    instance_name: str
    public_knowledge_resource_ids: list[str] | None
    created_at: datetime


class PolarRAGBindingListResponse(BaseModel):
    items: list[PolarRAGBindingResponse]
    total: int
    offset: int
    limit: int


class PolarRAGPublicResourceResponse(BaseModel):
    knowledge_resource_id: str
    name: str
    knowledge_space_name: str


class PolarRAGPublicResourceListResponse(BaseModel):
    items: list[PolarRAGPublicResourceResponse]
    total: int
    offset: int
    limit: int


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
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return []
    return value


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


def _enterprise_access_selection(
    body: EnterpriseAccessSelectionRequest,
) -> EnterpriseAccessSelection:
    return EnterpriseAccessSelection(
        identity_source_id=body.identity_source_id,
        all_synced_users=body.all_synced_users,
        directory_group_ids=tuple(body.directory_group_ids),
        pas_user_ids=tuple(body.pas_user_ids),
        polarrag_instance_ids=tuple(body.polarrag_instance_ids or ()),
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


@router.get(
    "/{agent_id}/polarrag-bindings",
    response_model=PolarRAGBindingListResponse,
)
async def list_polarrag_bindings(
    agent_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await _require_agent(session, agent_id)
    total = (
        await session.scalar(
            select(func.count(AgentPolarRAGInstanceBinding.id)).where(AgentPolarRAGInstanceBinding.agent_id == agent_id)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(AgentPolarRAGInstanceBinding)
            .options(selectinload(AgentPolarRAGInstanceBinding.instance))
            .where(AgentPolarRAGInstanceBinding.agent_id == agent_id)
            .order_by(AgentPolarRAGInstanceBinding.id)
            .offset(offset)
            .limit(limit)
        )
    ).scalars()
    return PolarRAGBindingListResponse(
        items=[_binding_response(row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


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
        await _audit(session, admin, "agent_polarrag_binding.create", "agent_polarrag_binding", row.id)
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
    response_model=PolarRAGPublicResourceListResponse,
)
async def list_polarrag_binding_public_resources(
    agent_id: str,
    binding_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await _require_polarrag_binding(session, agent_id, binding_id)
    filters = [
        KnowledgeResource.polarrag_instance_id == row.polarrag_instance_id,
        KnowledgeResource.kb_type == "PUBLIC",
        KnowledgeResource.binding_mode == KnowledgeBindingMode.DOMAIN,
        KnowledgeResource.sync_status == KnowledgeResourceSyncStatus.ACTIVE,
        KnowledgeResource.enabled.is_(True),
        PolarRAGSpace.enabled.is_(True),
    ]
    normalized_search = search.strip() if search else None
    if normalized_search:
        filters.append(KnowledgeResource.name.ilike(f"%{normalized_search}%"))
    total = await session.scalar(
        select(func.count(KnowledgeResource.id))
        .join(
            PolarRAGSpace,
            PolarRAGSpace.knowledge_space_id == KnowledgeResource.knowledge_space_id,
        )
        .where(*filters)
    )
    resources = list(
        (
            await session.execute(
                select(KnowledgeResource)
                .join(
                    PolarRAGSpace,
                    PolarRAGSpace.knowledge_space_id == KnowledgeResource.knowledge_space_id,
                )
                .options(selectinload(KnowledgeResource.space))
                .where(*filters)
                .order_by(KnowledgeResource.name, KnowledgeResource.id)
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )
    return PolarRAGPublicResourceListResponse(
        items=[
            PolarRAGPublicResourceResponse(
                knowledge_resource_id=resource.id,
                name=resource.name,
                knowledge_space_name=resource.space.name,
            )
            for resource in resources
        ],
        total=total or 0,
        offset=offset,
        limit=limit,
    )


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
                str(uuid.UUID(resource_id)) == resource_id for resource_id in requested
            )
        except (TypeError, ValueError, AttributeError):
            valid_shape = False
        selected = set(
            (
                await session.execute(
                    select(KnowledgeResource.id)
                    .join(
                        PolarRAGSpace,
                        PolarRAGSpace.knowledge_space_id == KnowledgeResource.knowledge_space_id,
                    )
                    .where(
                        KnowledgeResource.id.in_(requested),
                        KnowledgeResource.polarrag_instance_id == row.polarrag_instance_id,
                        KnowledgeResource.kb_type == "PUBLIC",
                        KnowledgeResource.binding_mode == KnowledgeBindingMode.DOMAIN,
                        KnowledgeResource.sync_status == KnowledgeResourceSyncStatus.ACTIVE,
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
                    "Selected resources must be synchronized ACTIVE PUBLIC knowledge resources on the bound instance"
                ),
            )
        requested = sorted(requested)
    row.public_knowledge_resource_ids_json = None if requested is None else json.dumps(requested, separators=(",", ":"))
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
        await _audit(session, admin, "agent_polarrag_binding.delete", "agent_polarrag_binding", row.id)
        await session.delete(row)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Audit unavailable") from exc
