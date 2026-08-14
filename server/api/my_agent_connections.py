from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from server.auth.dependencies import get_current_user
from server.auth.builtin import verify_password
from server.core import agent_token_service, agent_user_token_service
from server.core.agent_access import has_agent_access, list_accessible_agent_ids
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import (
    Agent,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    AgentUserToken,
    AuditStatus,
    AuthProvider,
    User,
)

router = APIRouter(prefix="/me/agent-connections", tags=["my-agent-connections"])


class ConfirmedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]
    expires_at: datetime | None = None


class TokenExpiryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expires_at: datetime | None = None


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=1024)


class MyTokenSummary(BaseModel):
    token_prefix: str
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None


class MyPolarRAGInstance(BaseModel):
    id: str
    name: str


class MyAgentConnection(BaseModel):
    assignment_id: str | None
    agent_id: str
    agent_name: str
    agent_status: str
    polarrag_instances: list[MyPolarRAGInstance]
    password_reveal_available: bool
    token: MyTokenSummary | None


class MyTokenResponse(BaseModel):
    assignment_id: str
    token_prefix: str
    status: str
    expires_at: datetime | None
    last_used_at: datetime | None
    token: str | None = None


async def _owned_assignment(
    session: AsyncSession,
    connection_id: str,
    user_id: str,
    *,
    create: bool = False,
) -> AgentUserAssignment:
    row = (
        await session.execute(
            select(AgentUserAssignment)
            .options(
                selectinload(AgentUserAssignment.agent),
                selectinload(AgentUserAssignment.user),
                selectinload(AgentUserAssignment.token),
            )
            .where(
                AgentUserAssignment.user_id == user_id,
                or_(
                    AgentUserAssignment.id == connection_id,
                    AgentUserAssignment.agent_id == connection_id,
                ),
            )
        )
    ).scalar_one_or_none()
    agent_id = row.agent_id if row is not None else connection_id
    if not await has_agent_access(session, agent_id, user_id):
        raise HTTPException(status_code=404, detail="Agent connection not found")
    if row is None:
        if not create:
            raise HTTPException(
                status_code=404,
                detail="Agent user token not found",
            )
        agent = await session.get(Agent, agent_id)
        user = await session.get(User, user_id)
        if agent is None or user is None:
            raise HTTPException(status_code=404, detail="Agent connection not found")
        row = AgentUserAssignment(
            agent_id=agent_id,
            user_id=user_id,
            is_direct=False,
        )
        row.agent = agent
        row.user = user
        session.add(row)
        await session.flush()
    return row


def _token_response(
    row: AgentUserToken,
    plaintext: str | None = None,
) -> MyTokenResponse:
    return MyTokenResponse(
        assignment_id=row.assignment_id,
        token_prefix=row.token_prefix,
        status=agent_user_token_service.token_status(row),
        expires_at=_as_utc(row.expires_at),
        last_used_at=row.last_used_at,
        token=plaintext,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(
        tzinfo=timezone.utc
    )


async def _audit(
    session: AsyncSession,
    user: User,
    action: str,
    token_id: str,
) -> None:
    await log_audit(
        session,
        user_id=user.id,
        action=action,
        status=AuditStatus.SUCCESS,
        user_name=user.display_name,
        target_type="agent_user_token",
        target_id=token_id,
        required=True,
        commit=False,
    )


@router.get("", response_model=list[MyAgentConnection])
async def list_connections(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    agent_ids = await list_accessible_agent_ids(session, user.id)
    if not agent_ids:
        return []
    assignments = list(
        (
            await session.execute(
                select(AgentUserAssignment)
                .options(
                    selectinload(AgentUserAssignment.agent),
                    selectinload(AgentUserAssignment.token),
                )
                .where(
                    AgentUserAssignment.user_id == user.id,
                    AgentUserAssignment.agent_id.in_(agent_ids),
                )
                .order_by(AgentUserAssignment.id)
            )
        ).scalars()
    )
    assignments_by_agent = {row.agent_id: row for row in assignments}
    agents = list(
        (
            await session.execute(
                select(Agent)
                .where(Agent.id.in_(agent_ids))
                .order_by(Agent.name, Agent.id)
            )
        ).scalars()
    )
    bindings = list(
        (
            await session.execute(
                select(AgentPolarRAGInstanceBinding)
                .options(selectinload(AgentPolarRAGInstanceBinding.instance))
                .where(AgentPolarRAGInstanceBinding.agent_id.in_(agent_ids))
                .order_by(AgentPolarRAGInstanceBinding.id)
            )
        ).scalars()
    ) if agent_ids else []
    by_agent: dict[str, list[MyPolarRAGInstance]] = {}
    for binding in bindings:
        by_agent.setdefault(binding.agent_id, []).append(
            MyPolarRAGInstance(id=binding.instance.id, name=binding.instance.name)
        )
    return [
        _connection_response(
            agent,
            assignments_by_agent.get(agent.id),
            by_agent.get(agent.id, []),
            password_reveal_available=user.password_hash is not None,
        )
        for agent in agents
    ]


def _connection_response(
    agent: Agent,
    assignment: AgentUserAssignment | None,
    instances: list[MyPolarRAGInstance],
    *,
    password_reveal_available: bool,
) -> MyAgentConnection:
    token = assignment.token if assignment is not None else None
    return MyAgentConnection(
        assignment_id=assignment.id if assignment is not None else None,
        agent_id=agent.id,
        agent_name=agent.name,
        agent_status=agent.status.value,
        polarrag_instances=instances,
        password_reveal_available=password_reveal_available,
        token=(
            MyTokenSummary(
                token_prefix=token.token_prefix,
                status=agent_user_token_service.token_status(token),
                expires_at=_as_utc(token.expires_at),
                last_used_at=token.last_used_at,
            )
            if token is not None
            else None
        ),
    )


async def _finish_sensitive(
    session: AsyncSession,
    user: User,
    action: str,
    row: AgentUserToken,
    plaintext: str | None,
) -> Response:
    try:
        await _audit(session, user, action, row.id)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Token operation unavailable") from exc
    return Response(
        content=_token_response(row, plaintext).model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{connection_id}/token/issue", response_model=MyTokenResponse)
async def issue_token(
    connection_id: str,
    body: TokenExpiryRequest | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    assignment = await _owned_assignment(
        session,
        connection_id,
        user.id,
        create=True,
    )
    try:
        row, plaintext = await agent_user_token_service.issue_token(
            session,
            assignment.id,
            body.expires_at if body is not None else None,
        )
        if user.auth_provider == AuthProvider.OIDC:
            await agent_token_service.consume_reveal_budget(
                session, user.id, assignment.agent_id
            )
    except agent_token_service.TokenRevealRateLimitExceeded as exc:
        await session.rollback()
        raise HTTPException(
            status_code=429,
            detail="Token delivery rate limit exceeded",
        ) from exc
    except agent_user_token_service.ActiveTokenExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _finish_sensitive(
        session,
        user,
        "agent_user_token.issue",
        row,
        plaintext if user.auth_provider == AuthProvider.OIDC else None,
    )


@router.post("/{connection_id}/token/reveal", response_model=MyTokenResponse)
async def reveal_token(
    connection_id: str,
    body: PasswordRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    assignment = await _owned_assignment(session, connection_id, user.id)
    if user.auth_provider == AuthProvider.OIDC:
        raise HTTPException(
            status_code=409,
            detail=(
                "Password reveal is unavailable for SSO users; regenerate "
                "the Token for one-time delivery"
            ),
        )
    if (
        user.password_hash is None
        or not verify_password(body.password, user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="Password verification failed")
    try:
        await agent_token_service.consume_reveal_budget(
            session, user.id, assignment.agent_id
        )
        row, plaintext = await agent_user_token_service.reveal_token(
            session, assignment.id
        )
    except agent_token_service.TokenRevealRateLimitExceeded as exc:
        await session.rollback()
        raise HTTPException(status_code=429, detail="Token reveal rate limit exceeded") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _finish_sensitive(
        session, user, "agent_user_token.reveal", row, plaintext
    )


@router.post("/{connection_id}/token/regenerate", response_model=MyTokenResponse)
async def regenerate_token(
    connection_id: str,
    body: ConfirmedRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    assignment = await _owned_assignment(session, connection_id, user.id)
    try:
        row, plaintext = await agent_user_token_service.regenerate_token(
            session, assignment.id, body.expires_at
        )
        if user.auth_provider == AuthProvider.OIDC:
            await agent_token_service.consume_reveal_budget(
                session, user.id, assignment.agent_id
            )
    except agent_token_service.TokenRevealRateLimitExceeded as exc:
        await session.rollback()
        raise HTTPException(
            status_code=429,
            detail="Token delivery rate limit exceeded",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _finish_sensitive(
        session,
        user,
        "agent_user_token.regenerate",
        row,
        plaintext if user.auth_provider == AuthProvider.OIDC else None,
    )


@router.post("/{connection_id}/token/revoke", response_model=MyTokenResponse)
async def revoke_token(
    connection_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    assignment = await _owned_assignment(session, connection_id, user.id)
    try:
        row = await agent_user_token_service.revoke_token(
            session,
            assignment.id,
        )
        await _audit(session, user, "agent_user_token.revoke", row.id)
        await session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Agent user token not found") from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Token revocation unavailable") from exc
    return _token_response(row)
