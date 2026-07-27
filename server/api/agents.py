from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import agent_service, agent_token_service
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import Agent, AgentAPIToken, AgentStatus, AuditStatus, User
from server.models.base import utc_now

router = APIRouter(prefix="/agents", tags=["agents"])


class SecurityAuditUnavailable(Exception):
    pass


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    max_active_resources: int | None = Field(default=None, gt=0)


class UpdateAgentRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    status: AgentStatus | None = None
    max_active_resources: int | None = Field(default=None, gt=0)


class AgentTokenSummaryResponse(BaseModel):
    id: str
    token_prefix: str
    status: Literal["active", "revoked", "expired"]
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def from_model(cls, row: AgentAPIToken) -> "AgentTokenSummaryResponse":
        now = utc_now()
        if row.revoked_at is not None:
            status: Literal["active", "revoked", "expired"] = "revoked"
        elif row.expires_at is not None and (
            row.expires_at
            if row.expires_at.tzinfo is not None
            else row.expires_at.replace(tzinfo=timezone.utc)
        ) <= now:
            status = "expired"
        elif row.token_ciphertext is None:
            status = "revoked"
        else:
            status = "active"
        return cls(
            id=row.id,
            token_prefix=row.token_prefix,
            status=status,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            last_used_at=row.last_used_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class AgentResponse(BaseModel):
    id: str
    name: str
    description: str | None
    status: AgentStatus
    max_active_resources: int | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime | None
    token_summary: AgentTokenSummaryResponse | None

    @classmethod
    def from_model(cls, agent: Agent) -> "AgentResponse":
        return cls(
            id=agent.id,
            name=agent.name,
            description=agent.description,
            status=agent.status,
            max_active_resources=agent.max_active_resources,
            created_by=agent.created_by,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
            token_summary=(
                AgentTokenSummaryResponse.from_model(agent.api_token)
                if agent.api_token is not None
                else None
            ),
        )


class AgentCreatedResponse(AgentResponse):
    token_id: str
    token_prefix: str
    token_expires_at: datetime | None
    token: str


class TokenRequest(BaseModel):
    expires_at: datetime | None = None


class AgentTokenRevealRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: Literal[True]


class TokenResponse(BaseModel):
    id: str
    agent_id: str
    token_prefix: str
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime | None
    token: str | None = None

    @classmethod
    def from_model(
        cls, row: AgentAPIToken, *, plaintext: str | None = None
    ) -> "TokenResponse":
        return cls(
            id=row.id,
            agent_id=row.agent_id,
            token_prefix=row.token_prefix,
            expires_at=row.expires_at,
            revoked_at=row.revoked_at,
            last_used_at=row.last_used_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
            token=plaintext,
        )


async def _audit_token_action(
    session: AsyncSession,
    admin: User,
    action: str,
    agent_id: str,
    token_id: str,
) -> None:
    try:
        await log_audit(
            session,
            user_id=admin.id,
            action=action,
            status=AuditStatus.SUCCESS,
            client_info=f"agent_id={agent_id}",
            user_name=admin.display_name,
            target_type="agent_token",
            target_id=token_id,
            required=True,
            commit=False,
        )
    except Exception as exc:
        raise SecurityAuditUnavailable from exc


@router.post("", response_model=AgentCreatedResponse, status_code=201)
async def create_agent(
    body: CreateAgentRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        agent = await agent_service.create_agent(
            session,
            name=body.name,
            description=body.description,
            max_active_resources=body.max_active_resources,
            admin_id=admin.id,
        )
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Agent name already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        token_row, plaintext = await agent_token_service.regenerate_token(
            session, agent.id, None
        )
        await log_audit(
            session,
            user_id=admin.id,
            action="agent.create",
            status=AuditStatus.SUCCESS,
            user_name=admin.display_name,
            target_type="agent",
            target_id=agent.id,
            required=True,
            commit=False,
        )
        await _audit_token_action(
            session,
            admin,
            "agent_token.regenerate",
            agent.id,
            token_row.id,
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503, detail="Agent credential creation unavailable"
        ) from exc

    response = AgentResponse.from_model(agent).model_dump()
    response["token_summary"] = AgentTokenSummaryResponse.from_model(
        token_row
    ).model_dump()
    return Response(
        content=AgentCreatedResponse(
            **response,
            token_id=token_row.id,
            token_prefix=token_row.token_prefix,
            token_expires_at=token_row.expires_at,
            token=plaintext,
        ).model_dump_json(),
        media_type="application/json",
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


@router.get("", response_model=list[AgentResponse])
async def list_agents(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    return [
        AgentResponse.from_model(agent)
        for agent in await agent_service.list_agents(session)
    ]


@router.get("/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        return AgentResponse.from_model(await agent_service.get_agent(session, agent_id))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str,
    body: UpdateAgentRequest,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        existing = await agent_service.get_agent(session, agent_id)
        previous_status = existing.status
        agent = await agent_service.update_agent(
            session,
            agent_id,
            name=body.name,
            description=body.description,
            update_description="description" in body.model_fields_set,
            status=body.status,
            max_active_resources=body.max_active_resources,
            update_max_active_resources="max_active_resources"
            in body.model_fields_set,
        )
        action = "agent.update"
        if body.status == AgentStatus.DISABLED and previous_status != body.status:
            action = "agent.disable"
        elif body.status == AgentStatus.ACTIVE and previous_status != body.status:
            action = "agent.enable"
        await log_audit(
            session,
            user_id=_admin.id,
            action=action,
            status=AuditStatus.SUCCESS,
            user_name=_admin.display_name,
            target_type="agent",
            target_id=agent.id,
            required=action in {"agent.enable", "agent.disable"},
            commit=False,
        )
        await session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Agent name already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return AgentResponse.from_model(agent)


@router.post("/{agent_id}/token/regenerate", response_model=TokenResponse)
async def regenerate_agent_token(
    agent_id: str,
    body: TokenRequest | None = None,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row, plaintext = await agent_token_service.regenerate_token(
            session, agent_id, body.expires_at if body else None
        )
        await _audit_token_action(
            session, admin, "agent_token.regenerate", agent_id, row.id
        )
        await session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Token encryption unavailable") from exc
    except (SecurityAuditUnavailable, IntegrityError) as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503, detail="Token regeneration unavailable"
        ) from exc
    return Response(
        content=TokenResponse.from_model(row, plaintext=plaintext).model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{agent_id}/token/reveal", response_model=TokenResponse)
async def reveal_agent_token(
    agent_id: str,
    _body: AgentTokenRevealRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        await agent_token_service.consume_reveal_budget(
            session, admin.id, agent_id
        )
        plaintext = await agent_token_service.reveal_token(session, agent_id)
        row = await agent_token_service._get_token(session, agent_id)
        assert row is not None
        await _audit_token_action(
            session, admin, "agent_token.reveal", agent_id, row.id
        )
        await session.commit()
    except agent_token_service.TokenRevealRateLimitExceeded as exc:
        await session.rollback()
        raise HTTPException(
            status_code=429, detail="Token reveal rate limit exceeded"
        ) from exc
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        await session.commit()
        raise HTTPException(status_code=409, detail="Agent token is not active") from exc
    except SecurityAuditUnavailable as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503, detail="Token reveal unavailable"
        ) from exc
    return Response(
        content=TokenResponse.from_model(row, plaintext=plaintext).model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.post("/{agent_id}/token/revoke", response_model=TokenResponse)
async def revoke_agent_token(
    agent_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    try:
        row = await agent_token_service.revoke_token(session, agent_id)
        await _audit_token_action(
            session, admin, "agent_token.revoke", agent_id, row.id
        )
        await session.commit()
    except LookupError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SecurityAuditUnavailable as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503, detail="Token revocation unavailable"
        ) from exc
    return TokenResponse.from_model(row)
