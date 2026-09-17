from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.agent_access import list_accessible_agent_ids
from server.models import Agent, AgentStatus, UserWorkspace

_WORKSPACE_ID_NAMESPACE = uuid.UUID("eaa2ae64-d485-46aa-b41b-26c8318edcb2")


class UserWorkspaceStatus(str, enum.Enum):
    READY = "ready"
    SELECTION_REQUIRED = "selection_required"
    NO_AGENT_ACCESS = "no_agent_access"
    DEFAULT_AGENT_UNAVAILABLE = "default_agent_unavailable"


@dataclass(frozen=True)
class WorkspaceAgent:
    id: str
    name: str


@dataclass(frozen=True)
class UserWorkspaceResolution:
    workspace: UserWorkspace
    status: UserWorkspaceStatus
    available_agents: tuple[WorkspaceAgent, ...]
    default_agent: Agent | None
    default_changed: bool = False


def workspace_id_for_user(user_id: str) -> str:
    return str(uuid.uuid5(_WORKSPACE_ID_NAMESPACE, user_id))


async def _available_agents(
    session: AsyncSession,
    user_id: str,
) -> list[Agent]:
    agent_ids = await list_accessible_agent_ids(session, user_id)
    if not agent_ids:
        return []
    return list(
        (
            await session.execute(
                select(Agent)
                .where(
                    Agent.id.in_(agent_ids),
                    Agent.status == AgentStatus.ACTIVE,
                )
                .order_by(Agent.name, Agent.id)
            )
        ).scalars()
    )


async def ensure_user_workspace(
    session: AsyncSession,
    user_id: str,
) -> UserWorkspace:
    workspace = await session.scalar(
        select(UserWorkspace).where(UserWorkspace.user_id == user_id)
    )
    if workspace is not None:
        return workspace

    try:
        async with session.begin_nested():
            workspace = UserWorkspace(
                id=workspace_id_for_user(user_id),
                user_id=user_id,
            )
            session.add(workspace)
            await session.flush()
    except IntegrityError:
        workspace = await session.scalar(
            select(UserWorkspace).where(UserWorkspace.user_id == user_id)
        )
        if workspace is None:
            raise
    return workspace


async def resolve_user_workspace(
    session: AsyncSession,
    user_id: str,
) -> UserWorkspaceResolution:
    workspace = await ensure_user_workspace(session, user_id)
    agents = await _available_agents(session, user_id)
    by_id = {agent.id: agent for agent in agents}
    default_changed = False

    if workspace.default_agent_id is not None:
        default_agent = by_id.get(workspace.default_agent_id)
        status = (
            UserWorkspaceStatus.READY
            if default_agent is not None
            else UserWorkspaceStatus.DEFAULT_AGENT_UNAVAILABLE
        )
    elif len(agents) == 1:
        default_agent = agents[0]
        workspace.default_agent_id = default_agent.id
        await session.flush()
        default_changed = True
        status = UserWorkspaceStatus.READY
    elif agents:
        default_agent = None
        status = UserWorkspaceStatus.SELECTION_REQUIRED
    else:
        default_agent = None
        status = UserWorkspaceStatus.NO_AGENT_ACCESS

    return UserWorkspaceResolution(
        workspace=workspace,
        status=status,
        available_agents=tuple(
            WorkspaceAgent(id=agent.id, name=agent.name) for agent in agents
        ),
        default_agent=default_agent,
        default_changed=default_changed,
    )


async def select_default_agent(
    session: AsyncSession,
    *,
    user_id: str,
    agent_id: str,
) -> UserWorkspaceResolution | None:
    agents = await _available_agents(session, user_id)
    selected = next((agent for agent in agents if agent.id == agent_id), None)
    if selected is None:
        return None

    workspace = await ensure_user_workspace(session, user_id)
    changed = workspace.default_agent_id != selected.id
    workspace.default_agent_id = selected.id
    await session.flush()
    return UserWorkspaceResolution(
        workspace=workspace,
        status=UserWorkspaceStatus.READY,
        available_agents=tuple(
            WorkspaceAgent(id=agent.id, name=agent.name) for agent in agents
        ),
        default_agent=selected,
        default_changed=changed,
    )


async def resolve_authorized_agent(
    session: AsyncSession,
    *,
    user_id: str,
    requested_agent_id: str | None,
) -> Agent | None:
    """Resolve an explicit Agent or the ready Workspace default."""
    if requested_agent_id:
        agents = await _available_agents(session, user_id)
        return next(
            (agent for agent in agents if agent.id == requested_agent_id),
            None,
        )
    resolution = await resolve_user_workspace(session, user_id)
    return (
        resolution.default_agent
        if resolution.status == UserWorkspaceStatus.READY
        else None
    )
