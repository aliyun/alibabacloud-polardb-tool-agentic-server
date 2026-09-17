from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import get_current_user
from server.core.audit_logger import log_audit
from server.core.user_workspace import (
    UserWorkspaceResolution,
    resolve_user_workspace,
    select_default_agent,
)
from server.db.engine import get_session
from server.models import AuditStatus, User

router = APIRouter(prefix="/me/workspace", tags=["my-workspace"])


class WorkspaceAgentResponse(BaseModel):
    id: str
    name: str


class UserWorkspaceResponse(BaseModel):
    id: str
    status: str
    default_agent: WorkspaceAgentResponse | None
    available_agents: list[WorkspaceAgentResponse]


class SelectDefaultAgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str


def _response(resolution: UserWorkspaceResolution) -> UserWorkspaceResponse:
    default_agent = resolution.default_agent
    return UserWorkspaceResponse(
        id=resolution.workspace.id,
        status=resolution.status.value,
        default_agent=(
            WorkspaceAgentResponse(
                id=default_agent.id,
                name=default_agent.name,
            )
            if default_agent is not None
            else None
        ),
        available_agents=[
            WorkspaceAgentResponse(id=agent.id, name=agent.name)
            for agent in resolution.available_agents
        ],
    )


async def _audit_selection(
    session: AsyncSession,
    *,
    user: User,
    resolution: UserWorkspaceResolution,
    automatic: bool,
) -> None:
    if not resolution.default_changed or resolution.default_agent is None:
        return
    await log_audit(
        session,
        user_id=user.id,
        action="user_workspace.default_agent.select",
        status=AuditStatus.SUCCESS,
        user_name=user.display_name,
        target_type="agent",
        target_id=resolution.default_agent.id,
        client_info="automatic" if automatic else "user",
        required=True,
        commit=False,
    )


@router.get("", response_model=UserWorkspaceResponse)
async def get_workspace(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserWorkspaceResponse:
    resolution = await resolve_user_workspace(session, user.id)
    await _audit_selection(
        session,
        user=user,
        resolution=resolution,
        automatic=True,
    )
    await session.commit()
    return _response(resolution)


@router.put("/default-agent", response_model=UserWorkspaceResponse)
async def update_default_agent(
    body: SelectDefaultAgentRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserWorkspaceResponse:
    resolution = await select_default_agent(
        session,
        user_id=user.id,
        agent_id=body.agent_id,
    )
    if resolution is None:
        raise HTTPException(status_code=404, detail="Agent is not available")
    await _audit_selection(
        session,
        user=user,
        resolution=resolution,
        automatic=False,
    )
    await session.commit()
    return _response(resolution)
