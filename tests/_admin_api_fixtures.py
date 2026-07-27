from __future__ import annotations

import base64
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.jwt_manager import create_access_token, reset_keys
from server.config import reset_config
from server.db import engine as engine_mod
from server.db.engine import enable_sqlite_foreign_keys
from server.models import AuthProvider, Base, User, UserRole
from tests._helpers import init_test_jwt_keys


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
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
async def setup():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    enable_sqlite_foreign_keys(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    engine_mod._engine = engine
    engine_mod._session_factory = factory
    async with factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.ADMIN,
        )
        member = User(
            external_id="member",
            display_name="Member",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.MEMBER,
        )
        session.add_all([admin, member])
        await session.commit()
        await session.refresh(admin)
        await session.refresh(member)
    yield factory, admin, member
    await engine.dispose()


@pytest.fixture
async def client(setup):
    _, admin, member = setup
    app = create_app()
    admin_headers = {
        "Authorization": (
            "Bearer "
            + create_access_token({"sub": admin.id, "role": "admin"})
        )
    }
    member_headers = {
        "Authorization": (
            "Bearer "
            + create_access_token({"sub": member.id, "role": "member"})
        )
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http, admin_headers, member_headers
