from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.principal import (
    PrincipalAuthenticationError,
    Principal,
    PrincipalKind,
    get_current_principal,
)
from server.core.user_workspace import (
    UserWorkspaceStatus,
    resolve_authorized_agent,
    resolve_user_workspace,
)
from server.models import Agent, User


class MCPWorkspaceUnavailable(Exception):
    def __init__(self, status: UserWorkspaceStatus) -> None:
        self.status = status
        super().__init__(status.value)

    @property
    def code(self) -> str:
        return f"WORKSPACE_{self.status.value.upper()}"

    @property
    def message(self) -> str:
        return {
            UserWorkspaceStatus.SELECTION_REQUIRED: (
                "Select a default Agent in your workspace before using MCP tools."
            ),
            UserWorkspaceStatus.NO_AGENT_ACCESS: (
                "No active Agent is authorized for this user."
            ),
            UserWorkspaceStatus.DEFAULT_AGENT_UNAVAILABLE: (
                "The selected default Agent is no longer available."
            ),
            UserWorkspaceStatus.READY: "Workspace is ready.",
        }[self.status]


@dataclass(frozen=True)
class WorkspaceSQLActor:
    user: User
    agent: Agent

    @property
    def id(self) -> str:
        return self.user.id


@dataclass(frozen=True)
class MCPWorkspaceContext:
    authenticated_principal: Principal
    resource_principal: Principal
    user: User | None
    agent: Agent | None

    @property
    def sql_actor(self) -> User | Agent | WorkspaceSQLActor:
        if self.agent is None and self.user is not None:
            return self.user
        if self.user is None:
            assert self.agent is not None
            return self.agent
        assert self.agent is not None
        return WorkspaceSQLActor(user=self.user, agent=self.agent)


async def resolve_mcp_workspace_context(
    session: AsyncSession,
    subject: str,
    requested_agent_id: str | None = None,
    *,
    personal: bool = False,
) -> MCPWorkspaceContext:
    principal = await get_current_principal(session, subject)
    if personal:
        if principal.kind != PrincipalKind.USER or requested_agent_id is not None:
            raise PrincipalAuthenticationError("Personal access requires a user without an Agent scope")
        user = await session.get(User, principal.id)
        return MCPWorkspaceContext(principal, principal, user, None)
    if principal.kind == PrincipalKind.AGENT:
        agent = await session.get(Agent, principal.id)
        if agent is None:
            raise MCPWorkspaceUnavailable(
                UserWorkspaceStatus.DEFAULT_AGENT_UNAVAILABLE
            )
        return MCPWorkspaceContext(
            authenticated_principal=principal,
            resource_principal=principal,
            user=None,
            agent=agent,
        )

    user = await session.get(User, principal.id)
    if user is None:
        raise MCPWorkspaceUnavailable(UserWorkspaceStatus.NO_AGENT_ACCESS)
    if requested_agent_id:
        selected_agent = await resolve_authorized_agent(
            session,
            user_id=user.id,
            requested_agent_id=requested_agent_id,
        )
        if selected_agent is None:
            raise MCPWorkspaceUnavailable(
                UserWorkspaceStatus.DEFAULT_AGENT_UNAVAILABLE
            )
        return MCPWorkspaceContext(
            authenticated_principal=principal,
            resource_principal=Principal(
                kind=PrincipalKind.AGENT,
                id=selected_agent.id,
            ),
            user=user,
            agent=selected_agent,
        )
    resolution = await resolve_user_workspace(session, user.id)
    await session.commit()
    if (
        resolution.status != UserWorkspaceStatus.READY
        or resolution.default_agent is None
    ):
        raise MCPWorkspaceUnavailable(resolution.status)
    return MCPWorkspaceContext(
        authenticated_principal=principal,
        resource_principal=Principal(
            kind=PrincipalKind.AGENT,
            id=resolution.default_agent.id,
        ),
        user=user,
        agent=resolution.default_agent,
    )
