from __future__ import annotations

import base64
import os

import pytest
from datetime import datetime, timezone, timedelta
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from starlette.applications import Starlette
from starlette.routing import Route

from server.auth.auth_routes import handle_login_page, handle_login_callback
from server.auth.builtin import hash_password
from server.auth.jwt_manager import reset_keys
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.db import engine as engine_mod
from server.models import Base, User, AuthProvider, UserRole
from server.models.oauth import OAuthPendingAuth


@pytest.fixture(autouse=True)
def clean():
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    yield
    reset_keys()
    reset_config()
    engine_mod.reset_engine()


@pytest.fixture
def encryption_key():
    key = os.urandom(32)
    os.environ["PAS_ENCRYPTION_KEY"] = base64.b64encode(key).decode()
    yield key
    del os.environ["PAS_ENCRYPTION_KEY"]


@pytest.fixture
async def setup(encryption_key):
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._engine = e
    engine_mod._session_factory = async_sessionmaker(e, expire_on_commit=False)
    # Create admin user
    async with engine_mod._session_factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("testpass"),
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
    try:
        yield {"admin": admin}
    finally:
        await e.dispose()


@pytest.fixture
async def pending_session(setup):
    async with engine_mod._session_factory() as session:
        pending = OAuthPendingAuth(
            client_id="test-client",
            redirect_uri="http://localhost/callback",
            code_challenge="test-challenge",
            code_challenge_method="S256",
            resource="http://localhost:18760/mcp",
            scopes='["openid"]',
            state="test-state-123",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        session.add(pending)
        await session.commit()
        await session.refresh(pending)
    return pending


@pytest.fixture
async def expired_session(setup):
    async with engine_mod._session_factory() as session:
        pending = OAuthPendingAuth(
            client_id="test-client",
            redirect_uri="http://localhost/callback",
            code_challenge="test-challenge",
            code_challenge_method="S256",
            resource="http://localhost:18760/mcp",
            scopes='["openid"]',
            state="test-state-expired",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        session.add(pending)
        await session.commit()
        await session.refresh(pending)
    return pending


@pytest.fixture
async def client(setup):
    app = Starlette(
        routes=[
            Route("/mcp-auth/login", handle_login_page, methods=["GET"]),
            Route(
                "/mcp-auth/login/callback",
                handle_login_callback,
                methods=["POST"],
            ),
        ]
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestBuiltinLoginPage:
    async def test_renders_login_form(self, client, pending_session):
        resp = await client.get(
            f"/mcp-auth/login?session_id={pending_session.session_id}"
        )
        assert resp.status_code == 200
        assert "Sign In" in resp.text
        assert pending_session.session_id in resp.text

    async def test_missing_session_id(self, client, setup):
        resp = await client.get("/mcp-auth/login")
        assert resp.status_code == 400
        assert "Missing session_id" in resp.text

    async def test_invalid_session_id(self, client, setup):
        resp = await client.get("/mcp-auth/login?session_id=nonexistent")
        assert resp.status_code == 404
        assert "Invalid or expired session" in resp.text

    async def test_expired_session(self, client, expired_session):
        resp = await client.get(
            f"/mcp-auth/login?session_id={expired_session.session_id}"
        )
        assert resp.status_code == 410
        assert "Session expired" in resp.text


class TestBuiltinLoginCallback:
    async def test_successful_login_redirects_with_code(
        self, client, pending_session
    ):
        resp = await client.post(
            "/mcp-auth/login/callback",
            data={
                "session_id": pending_session.session_id,
                "username": "admin",
                "password": "testpass",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "code=" in location
        assert "state=test-state-123" in location
        assert location.startswith("http://localhost/callback")

    async def test_invalid_credentials(self, client, pending_session):
        resp = await client.post(
            "/mcp-auth/login/callback",
            data={
                "session_id": pending_session.session_id,
                "username": "admin",
                "password": "wrongpass",
            },
        )
        assert resp.status_code == 401
        assert "Invalid credentials" in resp.text

    async def test_missing_fields(self, client, setup):
        resp = await client.post(
            "/mcp-auth/login/callback",
            data={"session_id": "some-id"},
        )
        assert resp.status_code == 400
        assert "Missing required fields" in resp.text

    async def test_session_consumed_after_use(self, client, pending_session):
        # First login succeeds
        await client.post(
            "/mcp-auth/login/callback",
            data={
                "session_id": pending_session.session_id,
                "username": "admin",
                "password": "testpass",
            },
            follow_redirects=False,
        )
        # Second attempt should fail (session consumed)
        resp = await client.post(
            "/mcp-auth/login/callback",
            data={
                "session_id": pending_session.session_id,
                "username": "admin",
                "password": "testpass",
            },
        )
        assert resp.status_code == 404

    async def test_expired_session_callback(self, client, expired_session):
        resp = await client.post(
            "/mcp-auth/login/callback",
            data={
                "session_id": expired_session.session_id,
                "username": "admin",
                "password": "testpass",
            },
        )
        assert resp.status_code == 410
        assert "Session expired" in resp.text

    async def test_invalid_session_id_callback(self, client, setup):
        resp = await client.post(
            "/mcp-auth/login/callback",
            data={
                "session_id": "nonexistent-session",
                "username": "admin",
                "password": "testpass",
            },
        )
        assert resp.status_code == 404
