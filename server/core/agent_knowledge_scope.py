from __future__ import annotations

import json

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.agent_access import list_matching_agent_group_assignment_ids
from server.models import (
    Agent,
    AgentKnowledgeScope,
    AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode,
    AgentPolarRAGInstanceBinding,
)
from server.polarrag.access import KnowledgeResourceScope


def resource_ids_from_json(raw: str) -> set[str]:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        return set()
    return set(value)


async def resolve_agent_global_resource_scope(
    session: AsyncSession,
    agent_id: str,
) -> KnowledgeResourceScope:
    rows = (
        await session.execute(
            select(
                AgentPolarRAGInstanceBinding.polarrag_instance_id,
                AgentPolarRAGInstanceBinding.public_knowledge_resource_ids_json,
            ).where(AgentPolarRAGInstanceBinding.agent_id == agent_id)
        )
    ).all()
    bindings: dict[str, set[str] | None] = {}
    for instance_id, raw in rows:
        bindings[instance_id] = None if raw is None else resource_ids_from_json(raw)
    return KnowledgeResourceScope(bindings)


async def resolve_agent_knowledge_resource_scope(
    session: AsyncSession,
    agent_id: str,
    user_id: str,
) -> KnowledgeResourceScope:
    base = await resolve_agent_global_resource_scope(session, agent_id)
    agent = await session.get(Agent, agent_id)
    if agent is None:
        return KnowledgeResourceScope({})
    if agent.knowledge_scope_mode == AgentKnowledgeScopeMode.LEGACY_ALL:
        return base

    group_ids = await list_matching_agent_group_assignment_ids(
        session,
        agent_id,
        user_id,
    )
    subject_filter = AgentKnowledgeScopeBinding.user_id == user_id
    if group_ids:
        subject_filter = or_(
            subject_filter,
            AgentKnowledgeScopeBinding.group_assignment_id.in_(group_ids),
        )
    rows = (
        await session.execute(
            select(AgentKnowledgeScope.knowledge_resource_ids_json)
            .join(
                AgentKnowledgeScopeBinding,
                AgentKnowledgeScopeBinding.scope_id == AgentKnowledgeScope.id,
            )
            .where(
                AgentKnowledgeScope.agent_id == agent_id,
                subject_filter,
            )
            .distinct()
        )
    ).scalars()
    allowed: set[str] = set()
    for raw in rows:
        allowed.update(resource_ids_from_json(raw))
    return KnowledgeResourceScope(
        base.bindings,
        allowed_resource_ids=allowed,
    )
