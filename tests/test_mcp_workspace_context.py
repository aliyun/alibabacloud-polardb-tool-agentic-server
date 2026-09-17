from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.auth.principal import PrincipalKind, agent_subject, user_subject
from server.mcp.workspace_context import (
    MCPWorkspaceUnavailable,
    WorkspaceSQLActor,
    resolve_mcp_workspace_context,
)
from server.models import (
    Agent,
    AgentStatus,
    AgentUserAssignment,
    Base,
    User,
    UserWorkspace,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


async def _user_and_agent(session):
    user = User(
        external_id="workspace-user",
        display_name="Workspace User",
    )
    agent = Agent(name="workspace-agent", creator=user)
    session.add_all([user, agent])
    await session.flush()
    session.add(
        AgentUserAssignment(
            agent_id=agent.id,
            user_id=user.id,
            created_by_user_id=user.id,
            is_direct=True,
        )
    )
    await session.commit()
    return user, agent


async def test_user_context_uses_default_agent_resources(session) -> None:
    user, agent = await _user_and_agent(session)

    context = await resolve_mcp_workspace_context(
        session,
        user_subject(user.id),
    )

    assert context.authenticated_principal.kind == PrincipalKind.USER
    assert context.authenticated_principal.id == user.id
    assert context.resource_principal.kind == PrincipalKind.AGENT
    assert context.resource_principal.id == agent.id
    assert context.user is user
    assert context.agent is agent
    assert isinstance(context.sql_actor, WorkspaceSQLActor)
    assert context.sql_actor.user.id == user.id
    assert context.sql_actor.agent.id == agent.id
    workspace = await session.scalar(
        select(UserWorkspace).where(UserWorkspace.user_id == user.id)
    )
    assert workspace is not None
    assert workspace.default_agent_id == agent.id


async def test_user_context_fails_closed_when_default_agent_is_disabled(
    session,
) -> None:
    user, agent = await _user_and_agent(session)
    await resolve_mcp_workspace_context(session, user_subject(user.id))
    agent.status = AgentStatus.DISABLED
    await session.commit()

    with pytest.raises(MCPWorkspaceUnavailable) as exc_info:
        await resolve_mcp_workspace_context(session, user_subject(user.id))

    assert exc_info.value.code == "WORKSPACE_DEFAULT_AGENT_UNAVAILABLE"
    workspace = await session.scalar(
        select(UserWorkspace).where(UserWorkspace.user_id == user.id)
    )
    assert workspace is not None
    assert workspace.default_agent_id == agent.id


async def test_user_context_requires_selection_for_multiple_agents(
    session,
) -> None:
    user, _agent = await _user_and_agent(session)
    second = Agent(name="workspace-agent-2", creator=user)
    session.add(second)
    await session.flush()
    session.add(
        AgentUserAssignment(
            agent_id=second.id,
            user_id=user.id,
            created_by_user_id=user.id,
            is_direct=True,
        )
    )
    await session.commit()

    with pytest.raises(MCPWorkspaceUnavailable) as exc_info:
        await resolve_mcp_workspace_context(session, user_subject(user.id))

    assert exc_info.value.code == "WORKSPACE_SELECTION_REQUIRED"


async def test_user_context_can_pin_an_authorized_agent(session) -> None:
    user, default_agent = await _user_and_agent(session)
    await resolve_mcp_workspace_context(session, user_subject(user.id))
    selected_agent = Agent(name="workspace-agent-2", creator=user)
    session.add(selected_agent)
    await session.flush()
    session.add(
        AgentUserAssignment(
            agent_id=selected_agent.id,
            user_id=user.id,
            created_by_user_id=user.id,
            is_direct=True,
        )
    )
    await session.commit()

    context = await resolve_mcp_workspace_context(
        session,
        user_subject(user.id),
        selected_agent.id,
    )

    assert context.agent.id == selected_agent.id
    workspace = await session.scalar(
        select(UserWorkspace).where(UserWorkspace.user_id == user.id)
    )
    assert workspace is not None
    assert workspace.default_agent_id == default_agent.id


async def test_user_context_rejects_an_unauthorized_pinned_agent(
    session,
) -> None:
    user, _agent = await _user_and_agent(session)

    with pytest.raises(MCPWorkspaceUnavailable) as exc_info:
        await resolve_mcp_workspace_context(
            session,
            user_subject(user.id),
            "unauthorized-agent",
        )

    assert exc_info.value.code == "WORKSPACE_DEFAULT_AGENT_UNAVAILABLE"


async def test_agent_context_preserves_agent_token_behavior(session) -> None:
    user, agent = await _user_and_agent(session)

    context = await resolve_mcp_workspace_context(
        session,
        agent_subject(agent.id),
    )

    assert context.authenticated_principal.kind == PrincipalKind.AGENT
    assert context.resource_principal == context.authenticated_principal
    assert context.user is None
    assert context.agent.id == agent.id
    assert context.sql_actor is agent
    assert (
        await session.scalar(
            select(UserWorkspace).where(UserWorkspace.user_id == user.id)
        )
        is None
    )
