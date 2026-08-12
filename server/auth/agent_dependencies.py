from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.agent_token_service import hash_agent_token
from server.db.engine import get_session
from server.models import Agent, AgentAPIToken, AgentStatus

agent_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="AgentBearer",
    description="Opaque PAS Agent API token.",
)


@dataclass(frozen=True, slots=True)
class AgentPrincipal:
    agent_id: str
    token_id: str


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "code": "UNAUTHORIZED",
            "message": "A valid Agent Bearer token is required.",
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_agent(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        agent_bearer_scheme
    ),
    session: AsyncSession = Depends(get_session),
) -> AgentPrincipal:
    """Authenticate only opaque Agent tokens; never fall back to cookies."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()

    match = (
        await session.execute(
            select(
                AgentAPIToken.id,
                AgentAPIToken.agent_id,
                AgentAPIToken.expires_at,
                AgentAPIToken.revoked_at,
                Agent.status,
            )
            .join(Agent, Agent.id == AgentAPIToken.agent_id)
            .where(
                AgentAPIToken.token_hash
                == hash_agent_token(credentials.credentials)
            )
        )
    ).one_or_none()
    # Authentication must leave the shared request session idle because the
    # lifecycle service owns its serialized transaction boundary.
    await session.rollback()
    if match is None:
        raise _unauthorized()

    token_id, agent_id, expires_at, revoked_at, agent_status = match
    if revoked_at is not None or agent_status != AgentStatus.ACTIVE:
        raise _unauthorized()
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise _unauthorized()
    return AgentPrincipal(agent_id=agent_id, token_id=token_id)
