from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.auth.principal as principal_auth
from server.auth.dependencies import require_admin
from server.auth.jwt_manager import create_access_token, reset_keys
from server.auth.principal import (
    InvalidPrincipalSubject,
    Principal,
    PrincipalDisabled,
    PrincipalKind,
    agent_subject,
    get_current_principal,
    parse_subject,
    user_subject,
)
from server.config import reset_config
from server.db import engine as engine_mod
from server.db.engine import get_session
from server.models import Agent, AgentStatus, AuthProvider, Base, User, UserRole
from tests._helpers import create_test_access_token, init_test_jwt_keys


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    yield
    reset_keys()
    reset_config()
    engine_mod.reset_engine()


@pytest.fixture
async def principal_setup():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    engine_mod._engine = engine
    engine_mod._session_factory = factory

    async with factory() as session:
        user = User(
            id="same-id",
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            role=UserRole.ADMIN,
        )
        agent = Agent(id="same-id", name="same-id-agent")
        session.add_all([user, agent])
        await session.commit()

    yield factory
    await engine.dispose()


def test_subjects_are_namespaced():
    assert user_subject("same-id") == "user:same-id"
    assert agent_subject("same-id") == "agent:same-id"
    assert parse_subject("user:same-id") == Principal(
        PrincipalKind.USER, "same-id"
    )
    assert parse_subject("agent:same-id") == Principal(
        PrincipalKind.AGENT, "same-id"
    )
    with pytest.raises(InvalidPrincipalSubject):
        parse_subject("same-id")
    with pytest.raises(InvalidPrincipalSubject):
        parse_subject("service:same-id")


async def test_principal_resolution_isolates_same_ids(principal_setup):
    factory = principal_setup

    async with factory() as session:
        assert await get_current_principal(
            session, "user:same-id"
        ) == Principal(PrincipalKind.USER, "same-id")
        assert await get_current_principal(
            session, "agent:same-id"
        ) == Principal(PrincipalKind.AGENT, "same-id")

        agent = await session.get(Agent, "same-id")
        agent.status = AgentStatus.DISABLED
        await session.flush()

        assert await get_current_principal(
            session, "user:same-id"
        ) == Principal(PrincipalKind.USER, "same-id")
        with pytest.raises(PrincipalDisabled):
            await get_current_principal(session, "agent:same-id")


async def test_principal_kind_gate_routes_only_expected_actor_type(
    principal_setup,
):
    factory = principal_setup

    async with factory() as session:
        user = await principal_auth.require_current_actor(
            session, "user:same-id", PrincipalKind.USER
        )
        assert isinstance(user, User)

        with pytest.raises(principal_auth.PrincipalKindMismatch):
            await principal_auth.require_current_actor(
                session, "agent:same-id", PrincipalKind.USER
            )

        agent = await principal_auth.require_current_actor(
            session, "agent:same-id", PrincipalKind.AGENT
        )
        assert isinstance(agent, Agent)


@pytest.mark.parametrize("subject", ["same-id", "service:same-id"])
async def test_principal_kind_gate_rejects_raw_and_unknown_subjects(
    principal_setup, subject
):
    factory = principal_setup

    async with factory() as session:
        with pytest.raises(InvalidPrincipalSubject):
            await principal_auth.require_current_actor(
                session, subject, PrincipalKind.USER
            )


@pytest.fixture
async def client(principal_setup):
    factory = principal_setup
    app = FastAPI()

    async def test_session():
        async with factory() as session:
            yield session

    @app.get("/api/users")
    async def list_users(_admin: User = Depends(require_admin)):
        return []

    app.dependency_overrides[get_session] = test_session
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def test_admin_dependency_rejects_raw_subject(client):
    raw_token = create_test_access_token("same-id")
    response = await client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert response.status_code == 401


async def test_admin_dependency_rejects_namespaced_agent_subject(client):
    agent_token = create_test_access_token("agent:same-id")
    response = await client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert response.status_code == 401


async def test_admin_dependency_accepts_namespaced_user_subject(client):
    token = create_access_token({"sub": "same-id", "role": "admin"})
    response = await client.get(
        "/api/users",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
