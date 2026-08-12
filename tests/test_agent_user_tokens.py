from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core import agent_user_token_service
from server.models import (
    Agent,
    AgentPolarRAGInstanceBinding,
    AgentStatus,
    AgentUserAssignment,
    AuthProvider,
    Base,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    User,
    UserStatus,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _assignment(session):
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
    agent = Agent(name="knowledge-agent", created_by=admin.id)
    session.add_all([admin, alice, agent])
    await session.flush()
    instance = PolarRAGInstance(
        name="rag",
        scheme="http",
        host="rag.test",
        port=9200,
        username_ciphertext="encrypted",
        password_ciphertext="encrypted",
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by=admin.id,
    )
    session.add(instance)
    await session.flush()
    session.add(
        AgentPolarRAGInstanceBinding(
            agent_id=agent.id,
            polarrag_instance_id=instance.id,
            created_by_user_id=admin.id,
        )
    )
    assignment = AgentUserAssignment(
        agent_id=agent.id,
        user_id=alice.id,
        created_by_user_id=admin.id,
    )
    session.add(assignment)
    await session.commit()
    return assignment, agent, alice


async def test_agent_user_token_lifecycle(session) -> None:
    assignment, _, _ = await _assignment(session)
    row, plaintext = await agent_user_token_service.issue_token(
        session, assignment.id
    )
    await session.commit()

    assert plaintext.startswith("pas_user_agent_")
    assert row.token_hash != plaintext
    assert row.token_ciphertext not in {None, plaintext}
    assert (await agent_user_token_service.reveal_token(session, assignment.id))[1] == plaintext
    context = await agent_user_token_service.resolve_token(session, plaintext)
    assert context is not None
    assert context.assignment.id == assignment.id

    with pytest.raises(agent_user_token_service.ActiveTokenExists):
        await agent_user_token_service.issue_token(session, assignment.id)

    _, replacement = await agent_user_token_service.regenerate_token(
        session, assignment.id
    )
    await session.commit()
    assert replacement != plaintext
    assert await agent_user_token_service.resolve_token(session, plaintext) is None
    assert await agent_user_token_service.resolve_token(session, replacement) is not None

    await agent_user_token_service.revoke_token(session, assignment.id)
    await session.commit()
    assert await agent_user_token_service.resolve_token(session, replacement) is None


@pytest.mark.parametrize("disabled", ["agent", "user"])
async def test_agent_user_token_fails_closed_for_disabled_identity(
    session, disabled: str
) -> None:
    assignment, agent, user = await _assignment(session)
    _, plaintext = await agent_user_token_service.issue_token(
        session, assignment.id
    )
    await session.commit()
    if disabled == "agent":
        agent.status = AgentStatus.DISABLED
    else:
        user.status = UserStatus.DISABLED
    await session.commit()

    assert await agent_user_token_service.resolve_token(session, plaintext) is None
