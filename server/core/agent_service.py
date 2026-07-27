from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import Agent, AgentStatus


async def create_agent(
    session: AsyncSession,
    *,
    name: str,
    description: str | None,
    max_active_resources: int | None,
    admin_id: str,
) -> Agent:
    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("Agent name must not be empty")
    agent = Agent(
        name=normalized_name,
        description=description,
        max_active_resources=max_active_resources,
        created_by=admin_id,
    )
    session.add(agent)
    await session.flush()
    return agent


async def list_agents(session: AsyncSession) -> list[Agent]:
    result = await session.execute(select(Agent).order_by(Agent.created_at.desc()))
    return list(result.scalars().all())


async def get_agent(session: AsyncSession, agent_id: str) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise LookupError("Agent not found")
    return agent


async def update_agent(
    session: AsyncSession,
    agent_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    update_description: bool = False,
    status: AgentStatus | None = None,
    max_active_resources: int | None = None,
    update_max_active_resources: bool = False,
) -> Agent:
    agent = await get_agent(session, agent_id)
    if name is not None:
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("Agent name must not be empty")
        agent.name = normalized_name
    if update_description:
        agent.description = description
    if status is not None:
        agent.status = status
    if update_max_active_resources:
        agent.max_active_resources = max_active_resources
    await session.flush()
    return agent
