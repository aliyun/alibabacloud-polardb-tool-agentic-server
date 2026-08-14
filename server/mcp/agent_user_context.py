from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp.server.auth.middleware.auth_context import get_access_token

from server.core.agent_user_token_service import (
    TOKEN_PREFIX,
    AgentUserTokenContext,
    resolve_token,
)
from server.models import AgentPolarRAGInstanceBinding
from server.polarrag.access import KnowledgeResourceScope


async def current_agent_user_context(
    session: AsyncSession,
) -> AgentUserTokenContext | None:
    access_token = get_access_token()
    if access_token is None or not access_token.token.startswith(TOKEN_PREFIX):
        return None
    return await resolve_token(session, access_token.token)


async def resolve_polarrag_resource_scope(
    session: AsyncSession,
    context: AgentUserTokenContext,
) -> KnowledgeResourceScope:
    return await resolve_polarrag_resource_scope_for_agent(
        session, context.agent.id
    )


async def resolve_polarrag_resource_scope_for_agent(
    session: AsyncSession,
    agent_id: str,
) -> KnowledgeResourceScope:
    rows = (
        await session.execute(
            select(
                AgentPolarRAGInstanceBinding.polarrag_instance_id,
                AgentPolarRAGInstanceBinding.public_knowledge_resource_ids_json,
            ).where(
                AgentPolarRAGInstanceBinding.agent_id == agent_id
            )
        )
    ).all()
    bindings: dict[str, set[str] | None] = {}
    for instance_id, raw in rows:
        if raw is None:
            bindings[instance_id] = None
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = None
        bindings[instance_id] = (
            set(value)
            if isinstance(value, list)
            and all(isinstance(item, str) for item in value)
            else set()
        )
    return KnowledgeResourceScope(bindings)


async def current_polarrag_resource_scope(
    session: AsyncSession,
    *,
    context: AgentUserTokenContext | None = None,
) -> KnowledgeResourceScope | None:
    access_token = get_access_token()
    if access_token is None or not access_token.token.startswith(TOKEN_PREFIX):
        return None
    context = context or await resolve_token(session, access_token.token)
    if context is None:
        return KnowledgeResourceScope({})
    return await resolve_polarrag_resource_scope(session, context)
