from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp.server.auth.middleware.auth_context import get_access_token

from server.core.agent_user_token_service import (
    TOKEN_PREFIX,
    AgentUserTokenContext,
    resolve_token,
)
from server.models import AgentPolarRAGInstanceBinding


async def current_agent_user_context(
    session: AsyncSession,
) -> AgentUserTokenContext | None:
    access_token = get_access_token()
    if access_token is None or not access_token.token.startswith(TOKEN_PREFIX):
        return None
    return await resolve_token(session, access_token.token)


async def allowed_polarrag_instance_ids(
    session: AsyncSession,
    *,
    context: AgentUserTokenContext | None = None,
) -> set[str] | None:
    access_token = get_access_token()
    if access_token is None or not access_token.token.startswith(TOKEN_PREFIX):
        return None
    context = context or await resolve_token(session, access_token.token)
    if context is None:
        return set()
    return set(
        (
            await session.execute(
                select(
                    AgentPolarRAGInstanceBinding.polarrag_instance_id
                ).where(
                    AgentPolarRAGInstanceBinding.agent_id
                    == context.agent.id
                )
            )
        ).scalars()
    )
