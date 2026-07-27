from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from server.auth.builtin import hash_password
from server.auth.jwt_manager import reset_keys
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.db import engine as engine_mod
from server.mcp.transport import reset_mcp
from server.models import Base, User, AuthProvider, UserRole
from server.models.user_refresh_token import UserRefreshToken


def _replace_cookie(client: AsyncClient, name: str, value: str) -> None:
    """Replace a named cookie in the httpx client jar.

    httpx stores server-set cookies under a normalized domain (e.g. ``test.local``),
    so a plain ``cookies.set(name, value, domain="test")`` creates a *second* entry
    rather than overriding the original.  This helper deletes all existing entries
    for ``name`` first, then re-inserts the new value under the same domain(s).
    """
    domains = [c.domain for c in list(client.cookies.jar) if c.name == name]
    client.cookies.delete(name)
    for domain in domains:
        client.cookies.set(name, value, domain=domain)
    if not domains:
        client.cookies.set(name, value)


@pytest.fixture(autouse=True)
def clean():
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
async def in_memory_engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._engine = e
    engine_mod._session_factory = async_sessionmaker(e, expire_on_commit=False)
    yield e
    await e.dispose()


@pytest.fixture
async def app_client(in_memory_engine):
    from server.app import create_app

    async with engine_mod._session_factory() as s:
        s.add(User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("testpass1"),
            role=UserRole.ADMIN,
        ))
        await s.commit()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestLogin:
    async def test_login_sets_both_cookies(self, app_client):
        resp = await app_client.post("/auth/login", json={"username": "admin", "password": "testpass1"})
        assert resp.status_code == 200
        assert "session_token" in resp.cookies
        assert "refresh_token" in resp.cookies
        async with engine_mod._session_factory() as s:
            rec = (await s.execute(select(UserRefreshToken))).scalar_one()
            assert rec.revoked_at is None

    async def test_login_response_has_no_refresh_token_body(self, app_client):
        resp = await app_client.post("/auth/login", json={"username": "admin", "password": "testpass1"})
        assert "refresh_token" not in resp.json()


class TestRefresh:
    async def test_refresh_issues_new_cookies(self, app_client):
        await app_client.post("/auth/login", json={"username": "admin", "password": "testpass1"})
        old_refresh = app_client.cookies["refresh_token"]

        resp = await app_client.post("/auth/refresh")
        assert resp.status_code == 200
        async with engine_mod._session_factory() as s:
            rows = (await s.execute(select(UserRefreshToken))).scalars().all()
        revoked = [r for r in rows if r.revoked_at is not None]
        active = [r for r in rows if r.revoked_at is None]
        assert len(revoked) == 1
        assert len(active) == 1
        assert revoked[0].token_family == active[0].token_family
        assert app_client.cookies["refresh_token"] != old_refresh

    async def test_refresh_reuse_revokes_family(self, app_client):
        await app_client.post("/auth/login", json={"username": "admin", "password": "testpass1"})
        r1 = await app_client.post("/auth/refresh")
        assert r1.status_code == 200
        replay_token = r1.cookies["refresh_token"]

        r2 = await app_client.post("/auth/refresh")
        assert r2.status_code == 200

        _replace_cookie(app_client, "refresh_token", replay_token)
        r3 = await app_client.post("/auth/refresh")
        assert r3.status_code == 401
        async with engine_mod._session_factory() as s:
            rows = (await s.execute(select(UserRefreshToken))).scalars().all()
        assert all(r.revoked_at is not None for r in rows)

    async def test_refresh_missing_cookie_401(self, app_client):
        resp = await app_client.post("/auth/refresh")
        assert resp.status_code == 401

    async def test_refresh_unknown_token_401_no_family_revoke(self, app_client):
        await app_client.post("/auth/login", json={"username": "admin", "password": "testpass1"})
        _replace_cookie(app_client, "refresh_token", "not-a-real-token")
        resp = await app_client.post("/auth/refresh")
        assert resp.status_code == 401
        async with engine_mod._session_factory() as s:
            rows = (await s.execute(select(UserRefreshToken))).scalars().all()
        assert all(r.revoked_at is None for r in rows)

    async def test_refresh_expired_revokes_family(self, app_client):
        await app_client.post("/auth/login", json={"username": "admin", "password": "testpass1"})
        async with engine_mod._session_factory() as s:
            await s.execute(update(UserRefreshToken).values(expires_at=datetime.now(timezone.utc) - timedelta(days=1)))
            await s.commit()
        resp = await app_client.post("/auth/refresh")
        assert resp.status_code == 401
        async with engine_mod._session_factory() as s:
            rows = (await s.execute(select(UserRefreshToken))).scalars().all()
        assert all(r.revoked_at is not None for r in rows)


class TestLogout:
    async def test_logout_revokes_and_clears_cookies(self, app_client):
        await app_client.post("/auth/login", json={"username": "admin", "password": "testpass1"})
        resp = await app_client.post("/auth/logout")
        assert resp.status_code == 200
        async with engine_mod._session_factory() as s:
            rows = (await s.execute(select(UserRefreshToken))).scalars().all()
        assert all(r.revoked_at is not None for r in rows)
        _replace_cookie(app_client, "refresh_token", "whatever")
        r = await app_client.post("/auth/refresh")
        assert r.status_code == 401
