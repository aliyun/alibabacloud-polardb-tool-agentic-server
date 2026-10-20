from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import get_current_user, require_admin
from server.core.agent_access import (
    has_agent_access,
    list_matching_agent_group_assignment_ids,
)
from server.core.agent_knowledge_scope import resolve_agent_global_resource_scope, resolve_agent_knowledge_resource_scope
from server.core.external_knowledge_scope_sync import (
    ExternalKnowledgeScopeSyncError,
    clear_agent_external_knowledge,
    lock_agent_knowledge_configuration,
    ImportedGrant,
    ImportedSnapshot,
    import_external_knowledge_scope_snapshot,
)
from server.db.engine import get_session
from server.enterprise_identity.service import identity_provider_key
from server.models import (
    Agent,
    AgentStatus,
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeOrigin,
    EnterpriseIdentitySource,
    ExternalKnowledgeScopeGrant,
    ExternalKnowledgeScopeSnapshot,
    KnowledgeResource,
    User,
    UserStatus,
    UserExternalIdentity,
)
from server.polarrag.access import (
    KnowledgeResourceScope,
    list_visible_knowledge_resources_cursor_page,
)


router = APIRouter(tags=["external-knowledge-scopes"])


class ExternalScopeGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    subject_type: str = Field(min_length=1, max_length=32)
    subject_id: str = Field(min_length=1, max_length=255)


class ExternalScopeSnapshotRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    snapshot_id: str = Field(min_length=1, max_length=64)
    tenant_key: str = Field(min_length=1, max_length=255)
    source_scope_type: str = Field(min_length=1, max_length=32)
    source_scope_id: str = Field(min_length=1, max_length=255)
    source_scope_name: str = Field(default="", max_length=512)
    space_id: str = Field(min_length=1, max_length=255)
    kb_id: str = Field(min_length=1, max_length=255)
    generation: str = Field(min_length=1, max_length=64)
    fingerprint: str = Field(min_length=1, max_length=64)
    status: str = Field(pattern="^(ACTIVE|DENY_ALL|UNSUPPORTED)$")
    source_updated_at: datetime | None = None
    grants: list[ExternalScopeGrantRequest] = Field(default_factory=list, max_length=100_000)


class AutoBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class ExternalKnowledgeResourceItem(BaseModel):
    knowledge_resource_id: str
    knowledge_space_id: str
    polarrag_instance_id: str
    space_id: str
    kb_id: str
    name: str


class ExternalKnowledgeResourcePage(BaseModel):
    items: list[ExternalKnowledgeResourceItem]
    next_cursor: str | None


@router.put("/admin/identity-sources/{source_id}/external-knowledge-scope-snapshots/{snapshot_id}")
async def import_external_scope_snapshot(
    source_id: str,
    snapshot_id: str,
    body: ExternalScopeSnapshotRequest,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if snapshot_id != body.snapshot_id:
        raise HTTPException(status_code=422, detail="snapshot id mismatch")
    try:
        result = await import_external_knowledge_scope_snapshot(
            session,
            identity_source_id=source_id,
            snapshot=ImportedSnapshot(
                snapshot_id=body.snapshot_id,
                tenant_key=body.tenant_key,
                source_scope_type=body.source_scope_type,
                source_scope_id=body.source_scope_id,
                source_scope_name=body.source_scope_name,
                space_id=body.space_id,
                kb_id=body.kb_id,
                generation=body.generation,
                fingerprint=body.fingerprint,
                status=body.status,
                source_updated_at=body.source_updated_at,
                grants=tuple(
                    ImportedGrant(
                        subject_type=item.subject_type.lower(),
                        subject_id=item.subject_id,
                    )
                    for item in body.grants
                ),
            ),
        )
        await session.commit()
    except ExternalKnowledgeScopeSyncError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@router.get("/admin/agents/{agent_id}/external-knowledge-auto-bind")
async def get_external_knowledge_auto_bind(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return {
        "agent_id": agent.id,
        "enabled": agent.external_knowledge_auto_bind_enabled,
        "knowledge_scope_mode": agent.knowledge_scope_mode.value,
    }


async def reproject_agent_external_knowledge(session: AsyncSession, agent: Agent) -> dict:
    """Recompute a single Agent from PAS snapshots without fetching ETL."""
    from server.models import AgentKnowledgeScopeMode

    if agent.knowledge_scope_mode != AgentKnowledgeScopeMode.SCOPED or not agent.external_knowledge_auto_bind_enabled:
        raise HTTPException(status_code=409, detail="SCOPED with external auto-binding enabled is required")
    counts = {"snapshots_processed": 0, "snapshots_skipped": 0, "bindings_projected": 0}
    snapshots = list((await session.execute(select(ExternalKnowledgeScopeSnapshot))).scalars())
    sources = {
        item.id: item
        for item in (
            await session.execute(
                select(EnterpriseIdentitySource).where(
                    EnterpriseIdentitySource.id.in_({snapshot.identity_source_id for snapshot in snapshots})
                )
            )
        ).scalars()
    }
    resources = {
        item.id: item
        for item in (
            await session.execute(
                select(KnowledgeResource).where(
                    KnowledgeResource.id.in_({snapshot.knowledge_resource_id for snapshot in snapshots})
                )
            )
        ).scalars()
    }
    for snapshot in snapshots:
        resource = resources.get(snapshot.knowledge_resource_id)
        source = sources.get(snapshot.identity_source_id)
        if resource is None or source is None or not source.tenant_id:
            counts["snapshots_skipped"] += 1
            continue
        grants = tuple(
            ImportedGrant(subject_type=subject_type, subject_id=subject_id)
            for subject_type, subject_id in (
                await session.execute(
                    select(
                        ExternalKnowledgeScopeGrant.subject_type,
                        ExternalKnowledgeScopeGrant.subject_id,
                    ).where(ExternalKnowledgeScopeGrant.snapshot_id == snapshot.id)
                )
            ).tuples()
        )
        result = await import_external_knowledge_scope_snapshot(
            session,
            identity_source_id=snapshot.identity_source_id,
            agent_ids={agent.id},
            snapshot=ImportedSnapshot(
                snapshot_id=snapshot.id,
                tenant_key=source.tenant_id,
                source_scope_type=snapshot.source_scope_type,
                source_scope_id=snapshot.source_scope_id,
                source_scope_name=snapshot.source_scope_name,
                space_id=resource.space_id,
                kb_id=resource.kb_id,
                generation=snapshot.generation,
                fingerprint=snapshot.fingerprint,
                status=snapshot.status,
                grants=grants,
                source_updated_at=snapshot.source_updated_at,
            ),
        )
        counts["snapshots_processed"] += 1
        counts["bindings_projected"] += result["bindings_projected"]
    return counts


@router.post("/admin/agents/{agent_id}/external-knowledge-bindings/reconcile")
async def reconcile_agent_external_knowledge(
    agent_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await lock_agent_knowledge_configuration(session, agent_id)
    agent = await session.get(Agent, agent_id, populate_existing=True)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    try:
        result = await reproject_agent_external_knowledge(session, agent)
        from server.api.identity_sources import _commit_with_audit

        await _commit_with_audit(
            session,
            user_id=admin.id,
            action="agent.external_knowledge_reconcile",
            target_type="agent",
            target_id=agent.id,
        )
    except ExternalKnowledgeScopeSyncError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"agent_id": agent.id, **result}


@router.put("/admin/agents/{agent_id}/external-knowledge-auto-bind")
async def update_external_knowledge_auto_bind(
    agent_id: str,
    body: AutoBindRequest,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    await lock_agent_knowledge_configuration(session, agent_id)
    agent = await session.get(Agent, agent_id, populate_existing=True)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.external_knowledge_auto_bind_enabled = body.enabled
    await session.flush()
    if body.enabled:
        await reproject_agent_external_knowledge(session, agent)
    else:
        await clear_agent_external_knowledge(session, agent.id)
    await session.commit()
    return {"agent_id": agent.id, "enabled": agent.external_knowledge_auto_bind_enabled}


async def _external_resource_page(
    session: AsyncSession,
    *,
    source_ids: set[str],
    agent: Agent,
    user: User,
    cursor: str | None,
    limit: int,
) -> ExternalKnowledgeResourcePage:
    if (
        agent.status != AgentStatus.ACTIVE
        or user.status != UserStatus.ACTIVE
        or not await has_agent_access(session, agent.id, user.id)
    ):
        return ExternalKnowledgeResourcePage(items=[], next_cursor=None)
    group_ids = await list_matching_agent_group_assignment_ids(session, agent.id, user.id)
    subject_filter = AgentKnowledgeScopeBinding.user_id == user.id
    if group_ids:
        subject_filter = or_(
            subject_filter,
            AgentKnowledgeScopeBinding.group_assignment_id.in_(group_ids),
        )
    raw_values = list(
        (
            await session.execute(
                select(AgentKnowledgeScope.knowledge_resource_ids_json)
                .join(
                    AgentKnowledgeScopeBinding,
                    AgentKnowledgeScopeBinding.scope_id == AgentKnowledgeScope.id,
                )
                .where(
                    AgentKnowledgeScope.agent_id == agent.id,
                    AgentKnowledgeScope.identity_source_id.in_(source_ids),
                    AgentKnowledgeScope.origin == AgentKnowledgeScopeOrigin.EXTERNAL_SYNC,
                    subject_filter,
                )
                .distinct()
            )
        ).scalars()
    )
    allowed: set[str] = set()
    for raw in raw_values:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(value, list):
            allowed.update(item for item in value if isinstance(item, str))
    base = await resolve_agent_global_resource_scope(session, agent.id)
    scope = KnowledgeResourceScope(
        base.bindings,
        allowed_resource_ids=allowed,
        excluded_resource_ids=base.excluded_resource_ids,
        personal_bindings=base.personal_bindings,
    )
    return await _resource_page(session, user=user, scope=scope, cursor=cursor, limit=limit)


async def _resource_page(session, *, user, scope, cursor, limit):
    resources, has_more, cursor_valid = await list_visible_knowledge_resources_cursor_page(
        session, user, resource_scope=scope, cursor=cursor, limit=limit
    )
    if not cursor_valid:
        raise HTTPException(status_code=422, detail="Invalid cursor")
    return ExternalKnowledgeResourcePage(
        items=[
            ExternalKnowledgeResourceItem(
                knowledge_resource_id=item.id,
                knowledge_space_id=item.knowledge_space_id,
                polarrag_instance_id=item.polarrag_instance_id,
                space_id=item.space_id,
                kb_id=item.kb_id,
                name=item.name,
            )
            for item in resources
        ],
        next_cursor=resources[-1].id if has_more and resources else None,
    )


@router.get(
    "/admin/identity-sources/{source_id}/agents/{agent_id}/users/{external_user_id}/knowledge-resources",
    response_model=ExternalKnowledgeResourcePage,
)
async def list_external_user_knowledge_resources(
    source_id: str,
    agent_id: str,
    external_user_id: str,
    access_origin: str = Query(default="ALL", pattern="^(EXTERNAL_SYNC|MANUAL|ALL)$"),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    agent = await session.get(Agent, agent_id)
    if source is None or agent is None:
        raise HTTPException(status_code=404, detail="Identity source or Agent not found")
    user_id = (
        await session.execute(
            select(UserExternalIdentity.user_id).where(
                UserExternalIdentity.identity_provider == identity_provider_key(source),
                UserExternalIdentity.external_subject == external_user_id,
            )
        )
    ).scalar_one_or_none()
    if user_id is None:
        return ExternalKnowledgeResourcePage(items=[], next_cursor=None)
    user = await session.get(User, user_id)
    if user is None:
        return ExternalKnowledgeResourcePage(items=[], next_cursor=None)
    if access_origin in {"ALL", "MANUAL"}:
        if agent.status != AgentStatus.ACTIVE or user.status != UserStatus.ACTIVE or not await has_agent_access(session, agent.id, user.id):
            return ExternalKnowledgeResourcePage(items=[], next_cursor=None)
        scope = await resolve_agent_knowledge_resource_scope(session, agent.id, user.id, manual_only=access_origin == "MANUAL")
        return await _resource_page(session, user=user, scope=scope, cursor=cursor, limit=limit)
    return await _external_resource_page(
        session,
        source_ids={source.id},
        agent=agent,
        user=user,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/me/agents/{agent_id}/knowledge-resources",
    response_model=ExternalKnowledgeResourcePage,
)
async def list_my_external_knowledge_resources(
    agent_id: str,
    access_origin: str = Query(default="FEISHU", pattern="^(FEISHU|DINGTALK)$"),
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    source_ids = set(
        (
            await session.execute(
                select(EnterpriseIdentitySource.id).where(EnterpriseIdentitySource.provider == access_origin.lower())
            )
        ).scalars()
    )
    if not source_ids:
        return ExternalKnowledgeResourcePage(items=[], next_cursor=None)
    return await _external_resource_page(
        session,
        source_ids=source_ids,
        agent=agent,
        user=user,
        cursor=cursor,
        limit=limit,
    )


RefreshTargetId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class KnowledgePermissionRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_source_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    knowledge_resource_ids: list[str] | None = Field(default=None, min_length=1, max_length=500)
    kb_ids: list[RefreshTargetId] | None = Field(default=None, min_length=1, max_length=500)
    space_id: RefreshTargetId | None = None
    polarrag_instance_id: RefreshTargetId | None = None
    wait_timeout_seconds: float = Field(default=0, ge=0, le=30, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_target_identifiers(self):
        if self.kb_ids is not None and self.knowledge_resource_ids is not None:
            raise ValueError("Use kb_ids or knowledge_resource_ids, not both")
        if self.kb_ids is None and (self.space_id is not None or self.polarrag_instance_id is not None):
            raise ValueError("Instance and Space selectors require kb_ids")
        return self


@router.post(
    "/admin/agents/{agent_id}/knowledge-permissions/refresh",
    status_code=202,
    responses={
        200: {"description": "Refresh completed during bounded waiting"},
        409: {"description": "Configuration conflict or existing active refresh"},
        413: {"description": "Refresh grant capacity exceeded"},
        422: {"description": "Invalid refresh scope or waiting parameters"},
        502: {"description": "Refresh failed while reading or publishing snapshots"},
    },
)
async def refresh_agent_knowledge_permissions(
    agent_id: str, body: KnowledgePermissionRefreshRequest, background_tasks: BackgroundTasks,
    admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session),
):
    from server.enterprise_identity.knowledge_refresh import enqueue, run, start, wait_for_result
    from server.api.identity_sources import _commit_with_audit

    if body.wait_timeout_seconds and len(set(body.knowledge_resource_ids or body.kb_ids or [])) != 1:
        raise HTTPException(422, "Waiting requires exactly one explicit knowledge resource")
    result = await enqueue(session, agent_id, admin.id,
        source_ids=body.identity_source_ids, resource_ids=body.knowledge_resource_ids,
        kb_ids=body.kb_ids, space_id=body.space_id, polarrag_instance_id=body.polarrag_instance_id)
    await _commit_with_audit(session, user_id=admin.id, action="agent.knowledge_permissions_refresh",
        target_type="agent", target_id=agent_id)
    if body.wait_timeout_seconds:
        start(result["job_id"])
        result = await wait_for_result(agent_id, result["job_id"], body.wait_timeout_seconds)
        code = 202
        if result["state"] == "SUCCEEDED":
            code = 200
        elif result["state"] in {"FAILED", "PARTIAL"}:
            code = {
                "REFRESH_AGENT_CHANGED": 409,
                "REFRESH_RESOURCE_CHANGED": 409,
                "REFRESH_SOURCE_CHANGED": 409,
                "REFRESH_CAPACITY_LIMIT": 413,
            }.get(result.get("error"), 502)
        return JSONResponse(result, status_code=code)
    background_tasks.add_task(run, result["job_id"])
    return result


@router.get("/admin/agents/{agent_id}/knowledge-permissions/refresh/{job_id}")
async def get_agent_knowledge_permission_refresh(
    agent_id: str, job_id: str, _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    from server.enterprise_identity.knowledge_refresh import status

    return await status(session, agent_id, job_id)
