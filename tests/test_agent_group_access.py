from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.agent_access import has_agent_access, list_accessible_agent_ids
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentUserAssignment,
    AuthProvider,
    Base,
    Department,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalType,
    User,
    UserDepartment,
)
from server.models.base import utc_now


@pytest.fixture
async def access_rows():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
        )
        alice = User(
            external_id="alice",
            display_name="Alice",
            auth_provider=AuthProvider.BUILTIN,
        )
        agent = Agent(name="knowledge-agent")
        department = Department(name="Engineering")
        session.add_all([admin, alice, agent, department])
        await session.commit()
        yield session, admin, alice, agent, department
    await engine.dispose()


async def test_department_grant_tracks_current_membership(access_rows) -> None:
    session, admin, alice, agent, department = access_rows
    session.add(
        AgentGroupAssignment.for_department(
            agent_id=agent.id,
            department_id=department.id,
            created_by_user_id=admin.id,
        )
    )
    await session.commit()

    assert not await has_agent_access(session, agent.id, alice.id)
    membership = UserDepartment(
        user_id=alice.id,
        department_id=department.id,
    )
    session.add(membership)
    await session.commit()
    assert await has_agent_access(session, agent.id, alice.id)
    assert await list_accessible_agent_ids(session, alice.id) == {agent.id}

    await session.delete(membership)
    await session.commit()
    assert not await has_agent_access(session, agent.id, alice.id)


async def test_enterprise_group_grant_uses_active_registered_principal(
    access_rows,
) -> None:
    session, admin, alice, agent, _department = access_rows
    session.add(
        AgentGroupAssignment.for_enterprise_group(
            agent_id=agent.id,
            identity_domain="mcp-e2e-domain",
            provider="feishu",
            principal_id="engineering-e2e",
            created_by_user_id=admin.id,
        )
    )
    principal = EnterprisePrincipalAssignment.create(
        pas_user_id=alice.id,
        identity_domain="mcp-e2e-domain",
        provider="feishu",
        principal_type=EnterprisePrincipalType.GROUP,
        principal_id="engineering-e2e",
        source=EnterprisePrincipalSource.ADMIN_MANAGED,
    )
    session.add(principal)
    await session.commit()

    assert await has_agent_access(session, agent.id, alice.id)

    principal.valid_until = utc_now() - timedelta(seconds=1)
    await session.commit()
    assert not await has_agent_access(session, agent.id, alice.id)


async def test_direct_grant_is_independent_from_groups(access_rows) -> None:
    session, admin, alice, agent, _department = access_rows
    session.add(
        AgentUserAssignment(
            agent_id=agent.id,
            user_id=alice.id,
            created_by_user_id=admin.id,
            is_direct=True,
        )
    )
    await session.commit()

    assert await has_agent_access(session, agent.id, alice.id)
