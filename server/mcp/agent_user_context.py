from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from sqlalchemy.ext.asyncio import AsyncSession

from mcp.server.auth.middleware.auth_context import get_access_token

from server.core.agent_user_token_service import (
    TOKEN_PREFIX,
    AgentUserTokenContext,
    resolve_token,
)
from server.auth.principal import (
    InvalidPrincipalSubject,
    PrincipalKind,
    parse_subject,
)
from server.auth.token_claims import access_token_agent_id
from server.core.agent_access import has_agent_access
from server.core.agent_knowledge_scope import (
    resolve_agent_knowledge_resource_scope,
)
from server.models import (
    Agent,
    AgentStatus,
    User,
    UserStatus,
)
from server.polarrag.access import KnowledgeResourceScope


@dataclass(frozen=True)
class PlatformAgentUserContext:
    agent: Agent
    user: User


AgentUserContext: TypeAlias = AgentUserTokenContext | PlatformAgentUserContext


async def current_agent_user_context(
    session: AsyncSession,
) -> AgentUserContext | None:
    access_token = get_access_token()
    if access_token is None:
        return None
    if access_token.token.startswith(TOKEN_PREFIX):
        return await resolve_token(session, access_token.token)
    agent_id = access_token_agent_id(access_token)
    if agent_id is None or not access_token.subject:
        return None
    try:
        principal = parse_subject(access_token.subject)
    except InvalidPrincipalSubject:
        return None
    if principal.kind != PrincipalKind.USER:
        return None
    agent = await session.get(Agent, agent_id)
    user = await session.get(User, principal.id)
    if (
        agent is None
        or agent.status != AgentStatus.ACTIVE
        or user is None
        or user.status != UserStatus.ACTIVE
        or not await has_agent_access(session, agent.id, user.id)
    ):
        return None
    return PlatformAgentUserContext(agent=agent, user=user)


async def resolve_polarrag_resource_scope(
    session: AsyncSession,
    context: AgentUserContext,
) -> KnowledgeResourceScope:
    return await resolve_agent_knowledge_resource_scope(
        session,
        context.agent.id,
        context.user.id,
    )


async def resolve_polarrag_resource_scope_for_agent(
    session: AsyncSession,
    agent_id: str,
    user_id: str,
) -> KnowledgeResourceScope:
    return await resolve_agent_knowledge_resource_scope(
        session,
        agent_id,
        user_id,
    )


async def current_polarrag_resource_scope(
    session: AsyncSession,
    *,
    context: AgentUserContext | None = None,
) -> KnowledgeResourceScope | None:
    context = context or await current_agent_user_context(session)
    if context is None:
        return KnowledgeResourceScope({})
    return await resolve_polarrag_resource_scope(session, context)
