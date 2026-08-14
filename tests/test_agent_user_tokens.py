from __future__ import annotations

import base64
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core import agent_user_token_service
from server.mcp.agent_user_context import resolve_polarrag_resource_scope
from server.models import (
    Agent,
    AgentPolarRAGInstanceBinding,
    AgentStatus,
    AgentUserAssignment,
    AgentUserToken,
    AuthProvider,
    Base,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    User,
    UserStatus,
)
from server.models.base import utc_now


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
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
    assert row.expires_at is None
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


async def test_agent_user_token_expiry_is_replaced_on_regeneration(
    session,
) -> None:
    assignment, _, _ = await _assignment(session)
    first_expiry = utc_now() + timedelta(hours=1)
    row, plaintext = await agent_user_token_service.issue_token(
        session, assignment.id, first_expiry
    )
    await session.commit()

    assert row.expires_at == first_expiry
    assert await agent_user_token_service.resolve_token(session, plaintext)

    replacement_expiry = utc_now() + timedelta(days=1)
    row, replacement = await agent_user_token_service.regenerate_token(
        session, assignment.id, replacement_expiry
    )
    await session.commit()

    assert row.expires_at == replacement_expiry
    assert await agent_user_token_service.resolve_token(session, plaintext) is None
    assert await agent_user_token_service.resolve_token(session, replacement)


async def test_expiry_is_normalized_before_sqlite_round_trip(
    session, monkeypatch
) -> None:
    assignment, _, _ = await _assignment(session)
    source_expiry = datetime(
        2030,
        1,
        2,
        12,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )
    row, plaintext = await agent_user_token_service.issue_token(
        session, assignment.id, source_expiry
    )
    await session.commit()

    bind = session.bind
    assert bind is not None
    factory = async_sessionmaker(bind, expire_on_commit=False)
    async with factory() as reloaded_session:
        reloaded = await reloaded_session.get(AgentUserToken, row.id)
        assert reloaded is not None
        assert reloaded.expires_at == datetime(2030, 1, 2, 4, 0)

        monkeypatch.setattr(
            agent_user_token_service,
            "utc_now",
            lambda: datetime(2030, 1, 2, 3, 59, 59, tzinfo=timezone.utc),
        )
        assert await agent_user_token_service.resolve_token(
            reloaded_session, plaintext
        )

        monkeypatch.setattr(
            agent_user_token_service,
            "utc_now",
            lambda: datetime(2030, 1, 2, 4, 0, tzinfo=timezone.utc),
        )
        assert await agent_user_token_service.resolve_token(
            reloaded_session, plaintext
        ) is None


async def test_naive_expiry_is_rejected(session) -> None:
    assignment, _, _ = await _assignment(session)

    with pytest.raises(ValueError, match="timezone"):
        await agent_user_token_service.issue_token(
            session,
            assignment.id,
            datetime(2030, 1, 2, 4, 0),
        )


async def test_agent_user_scope_reloads_without_token_regeneration(
    session,
) -> None:
    assignment, agent, _user = await _assignment(session)
    _row, plaintext = await agent_user_token_service.issue_token(
        session,
        assignment.id,
    )
    await session.commit()
    context = await agent_user_token_service.resolve_token(session, plaintext)
    assert context is not None
    binding = (
        await session.execute(
            select(AgentPolarRAGInstanceBinding).where(
                AgentPolarRAGInstanceBinding.agent_id == agent.id
            )
        )
    ).scalar_one()

    initial = await resolve_polarrag_resource_scope(session, context)
    assert initial.bindings == {binding.polarrag_instance_id: None}

    resource_id = "00000000-0000-0000-0000-000000000001"
    binding.public_knowledge_resource_ids_json = f'["{resource_id}"]'
    await session.commit()
    selected = await resolve_polarrag_resource_scope(session, context)
    assert selected.bindings == {
        binding.polarrag_instance_id: {resource_id}
    }

    binding.public_knowledge_resource_ids_json = "not-json"
    await session.commit()
    corrupt = await resolve_polarrag_resource_scope(session, context)
    assert corrupt.bindings == {binding.polarrag_instance_id: set()}


async def test_expired_agent_user_token_and_old_last_use_fail_correctly(
    session,
) -> None:
    assignment, _, _ = await _assignment(session)
    row, plaintext = await agent_user_token_service.issue_token(
        session, assignment.id, utc_now() + timedelta(hours=1)
    )
    row.last_used_at = utc_now() - timedelta(days=365)
    await session.commit()

    assert await agent_user_token_service.resolve_token(session, plaintext)

    row.expires_at = utc_now() - timedelta(seconds=1)
    await session.commit()
    assert await agent_user_token_service.resolve_token(session, plaintext) is None


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
