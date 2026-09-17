from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import get_current_user
from server.auth.principal import Principal, PrincipalKind
from server.core.agent_access import has_agent_access
from server.core.db_instance_query import query_db_instances
from server.db.engine import get_session
from server.models import (
    Agent,
    AgentStatus,
    KnowledgeResourceManagementMode,
    User,
)
from server.mcp.agent_user_context import (
    resolve_polarrag_resource_scope_for_agent,
)

router = APIRouter(prefix="/me", tags=["my-resources"])


@router.get("/resources")
async def list_my_resources(
    agent_id: str | None = Query(default=None),
    personal: bool = Query(default=False),
    database_cursor: str | None = Query(default=None, max_length=4096),
    database_limit: int = Query(default=20, ge=1, le=200),
    knowledge_offset: int = Query(default=0, ge=0),
    knowledge_limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    database_page = await query_db_instances(
        session,
        Principal(PrincipalKind.USER, user.id),
        cursor=database_cursor,
        limit=database_limit,
        personal=personal,
    )
    if personal and agent_id is not None:
        raise HTTPException(422, "Personal resources cannot use an Agent scope")
    resource_scope = None
    if agent_id is not None:
        agent = await session.get(Agent, agent_id)
        if (
            agent is None
            or agent.status != AgentStatus.ACTIVE
            or not await has_agent_access(session, agent_id, user.id)
        ):
            raise HTTPException(status_code=404, detail="Agent connection not found")
        resource_scope = await resolve_polarrag_resource_scope_for_agent(
            session, agent_id, user.id
        )
    from server.features.knowledge import runtime, KnowledgeUnavailable
    feature = runtime()
    knowledge_resources, knowledge_resource_total = [], 0
    if feature is None or await feature.available():
        from server.polarrag.access import list_visible_knowledge_resources_page
        async def query_knowledge():
            return await list_visible_knowledge_resources_page(
                session, user, resource_scope=resource_scope,
                offset=knowledge_offset, limit=knowledge_limit,
            )
        if feature is None:
            knowledge_resources, knowledge_resource_total = await query_knowledge()
        else:
            try:
                async with feature.operation("resource_catalog"):
                    knowledge_resources, knowledge_resource_total = await query_knowledge()
            except KnowledgeUnavailable:
                pass
    access_sources = {}
    if personal:
        from server.core.access_control import resolve_user_instance_access
        from server.models import BindingOrigin
        for instance in database_page.instances:
            access = await resolve_user_instance_access(session, user.id, instance.db_instance_id)
            if access is not None:
                access_sources[instance.db_instance_id] = (
                    "department" if access.access_type == "department"
                    else "system" if access.binding is not None and access.binding.origin == BindingOrigin.SYSTEM
                    else "admin"
                )
    return {
        "database_instances": [
            {
                "db_instance_id": instance.db_instance_id,
                "name": instance.name,
                "db_type": instance.db_type,
                "source": instance.source,
                "status": instance.status,
                "permission": instance.permission,
                "capabilities": list(instance.capabilities),
                **({"access_source": access_sources.get(instance.db_instance_id, "admin")} if personal else {}),
            }
            for instance in database_page.instances
        ],
        "database_has_more": database_page.has_more,
        "database_next_cursor": database_page.next_cursor,
        "database_limit": database_limit,
        "knowledge_resources": [
            {
                "knowledge_resource_id": resource.id,
                "knowledge_space_id": resource.knowledge_space_id,
                "knowledge_space_name": resource.space.name,
                "polarrag_instance_id": resource.polarrag_instance_id,
                "polarrag_instance_name": resource.space.instance.name,
                "name": resource.name,
                "kb_id": resource.kb_id,
                "kb_type": resource.kb_type,
                "usage": resource.usage,
                "upload_ready": bool(
                    resource.management_mode
                    == KnowledgeResourceManagementMode.NATIVE
                    and resource.space.oss_config_validated
                    and resource.space.oss_bucket
                    and resource.space.oss_endpoint
                    and resource.space.oss_object_prefix
                    and resource.space.oss_access_key_id_ciphertext
                    and resource.space.oss_access_key_secret_ciphertext
                ),
            }
            for resource in knowledge_resources
        ],
        "knowledge_resource_total": knowledge_resource_total,
        "knowledge_resource_offset": knowledge_offset,
        "knowledge_resource_limit": knowledge_limit,
    }
