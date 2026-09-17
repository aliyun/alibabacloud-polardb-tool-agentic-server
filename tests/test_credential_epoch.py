from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.auth.builtin import hash_password
from server.auth.jwt_manager import (
    create_access_token,
    reset_keys,
    verify_token,
)
from server.config import reset_config
from server.db import engine as engine_mod
from server.mcp.transport import reset_mcp
from server.models import AuthProvider, Base, User, UserRole
from tests._helpers import init_test_jwt_keys


def test_user_credential_epoch_defaults_to_one() -> None:
    """A newly-created builtin user starts with the first credential epoch."""
    user = User(external_id="admin", display_name="Admin")

    assert user.credential_epoch == 1


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def clean() -> None:
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    reset_mcp()
    engine_mod.reset_engine()
    yield
    reset_keys()
    reset_config()
    reset_mcp()
    engine_mod.reset_engine()


@pytest.fixture
async def client_and_users():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    engine_mod._engine = engine
    engine_mod._session_factory = factory

    async with factory() as session:
        builtin_user = User(
            external_id="builtin",
            display_name="Builtin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("builtin-password"),
            role=UserRole.MEMBER,
        )
        oidc_user = User(
            external_id="oidc",
            display_name="OIDC",
            auth_provider=AuthProvider.OIDC,
            role=UserRole.MEMBER,
        )
        session.add_all([builtin_user, oidc_user])
        await session.commit()

    from server.app import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        yield client, factory, builtin_user, oidc_user
    await engine.dispose()


async def test_builtin_token_requires_matching_credential_epoch(
    client_and_users,
) -> None:
    """Builtin access tokens become unusable when their epoch is stale."""
    client, _, builtin_user, _ = client_and_users
    missing = create_access_token({"sub": builtin_user.id})
    stale = create_access_token({"sub": builtin_user.id, "credential_epoch": 0})
    current = create_access_token({"sub": builtin_user.id, "credential_epoch": 1})

    assert (await client.get("/auth/me", headers=_bearer(missing))).status_code == 401
    assert (await client.get("/auth/me", headers=_bearer(stale))).status_code == 401
    assert (await client.get("/auth/me", headers=_bearer(current))).status_code == 200


async def test_oidc_token_does_not_require_builtin_epoch(client_and_users) -> None:
    """OIDC access tokens keep their existing authentication contract."""
    client, _, _, oidc_user = client_and_users
    token = create_access_token({"sub": oidc_user.id})

    assert (await client.get("/auth/me", headers=_bearer(token))).status_code == 200


async def test_login_issues_current_credential_epoch(client_and_users) -> None:
    """A login access token contains the authenticated user's epoch."""
    client, _, _, _ = client_and_users

    response = await client.post(
        "/auth/login",
        json={"username": "builtin", "password": "builtin-password"},
    )

    assert response.status_code == 200
    assert verify_token(response.json()["access_token"])["credential_epoch"] == 1


async def test_refresh_issues_current_credential_epoch(client_and_users) -> None:
    """Refresh reads the user row so a rotated epoch reaches the new token."""
    client, factory, builtin_user, _ = client_and_users
    login_response = await client.post(
        "/auth/login",
        json={"username": "builtin", "password": "builtin-password"},
    )
    assert login_response.status_code == 200

    async with factory() as session:
        user = await session.get(User, builtin_user.id)
        assert user is not None
        user.credential_epoch = 2
        await session.commit()

    response = await client.post("/auth/refresh")

    assert response.status_code == 200
    assert verify_token(response.json()["access_token"])["credential_epoch"] == 2
