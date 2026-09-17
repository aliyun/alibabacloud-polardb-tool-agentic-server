from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from mcp.server.auth.provider import AccessToken
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import bearer_scheme
from server.auth.oauth_provider import PASAuthProvider
from server.auth.principal import InvalidPrincipalSubject, PrincipalKind, parse_subject
from server.auth.token_claims import access_token_agent_id
from server.config import get_config
from server.core.agent_access import has_agent_access
from server.db.engine import get_session, get_session_factory
from server.models import Agent, AgentStatus, User, UserStatus


@dataclass(frozen=True)
class PlatformAccessContext:
    access_token: AccessToken
    user: User
    agent: Agent


def platform_resource_url() -> str:
    return f"{get_config().server.public_base_url.rstrip('/')}/api/v1"


async def get_platform_access_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: AsyncSession = Depends(get_session),
) -> PlatformAccessContext:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REQUIRED", "message": "Authentication required."},
        )
    provider = PASAuthProvider(get_session_factory(), get_config())
    access_token = await provider.load_access_token(credentials.credentials)
    if (
        access_token is None
        or access_token.resource != platform_resource_url()
        or "polarrag" not in access_token.scopes
        or access_token_agent_id(access_token) is None
        or not access_token.subject
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REQUIRED", "message": "Invalid or expired token."},
        )
    try:
        principal = parse_subject(access_token.subject)
    except InvalidPrincipalSubject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REQUIRED", "message": "Invalid token subject."},
        ) from None
    if principal.kind != PrincipalKind.USER:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTH_REQUIRED", "message": "Invalid token subject."},
        )
    user = await session.get(User, principal.id)
    if user is None or user.status != UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTH_REQUIRED",
                "message": "Token authorization is no longer active.",
            },
        )
    try:
        agent = await _require_agent_grant(
            session,
            access_token_agent_id(access_token),
            principal.id,
        )
    except HTTPException:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "AUTH_REQUIRED",
                "message": "Token authorization is no longer active.",
            },
        ) from None
    return PlatformAccessContext(
        access_token=access_token,
        user=user,
        agent=agent,
    )


async def _require_agent_grant(
    session: AsyncSession,
    agent_id: str | None,
    user_id: str,
) -> Agent:
    if not agent_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    agent = await session.get(Agent, agent_id)
    if (
        agent is None
        or agent.status != AgentStatus.ACTIVE
        or not await has_agent_access(session, agent.id, user_id)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return agent
