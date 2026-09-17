from __future__ import annotations

import base64
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server import config as config_module
from server.auth.builtin import hash_password
from server.auth.identity_federation import IdentityFederation, UserIdentity
from server.auth.jwt_manager import reset_keys
from server.auth.rate_limit import reset_auth_rate_limiters
from server.auth.router import router as auth_router
from server.config import AppConfig, reset_config
from server.db import engine as engine_mod
from server.models import (
    Base,
    OIDCLoginState,
    User,
    UserExternalIdentity,
    UserRole,
    UserWorkspace,
)
from tests._helpers import init_test_jwt_keys


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    reset_keys()
    reset_config()
    reset_auth_rate_limiters()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", key)
    yield
    reset_keys()
    reset_config()
    reset_auth_rate_limiters()
    engine_mod.reset_engine()


@pytest.fixture
async def engine():
    value = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with value.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    engine_mod._engine = value
    engine_mod._session_factory = async_sessionmaker(
        value,
        expire_on_commit=False,
    )
    yield value
    await value.dispose()


def _oidc_config() -> AppConfig:
    return AppConfig(
        server={"public_base_url": "https://pas.example.com"},
        auth={
            "mode": "oidc",
            "oidc": {
                "issuer": "https://idp.example.com",
                "authorization_endpoint": (
                    "https://idp.example.com/authorize"
                ),
                "token_endpoint": "https://idp.example.com/token",
                "userinfo_endpoint": "https://idp.example.com/userinfo",
                "jwks_uri": "https://idp.example.com/jwks",
                "client_id": "pas-console",
                "client_secret": "secret",
                "provider_name": "enterprise",
                "idp_pkce": True,
            },
        },
    )


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    return app


async def test_console_oidc_login_creates_pas_session_and_workspace(
    engine,
) -> None:
    config_module._config = _oidc_config()
    identity = UserIdentity(
        subject="employee-1001",
        display_name="OIDC User",
        email="oidc@example.com",
    )

    with (
        patch.object(
            IdentityFederation,
            "discover_endpoints",
            new=AsyncMock(),
        ),
        patch.object(
            IdentityFederation,
            "build_authorize_url",
            side_effect=lambda _redirect_uri, state, **_kwargs: (
                f"https://idp.example.com/authorize?state={state}",
                "idp-code-verifier",
            ),
        ),
        patch.object(
            IdentityFederation,
            "exchange_code",
            new=AsyncMock(
                return_value={
                    "access_token": "idp-access-token",
                    "id_token": "idp-id-token",
                }
            ),
        ) as exchange_code,
        patch.object(
            IdentityFederation,
            "extract_user_identity",
            new=AsyncMock(return_value=identity),
        ) as extract_identity,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=_app()),
            base_url="https://pas.example.com",
        ) as client:
            start = await client.get(
                "/auth/oidc/login",
                params={"next": "/agents?tab=mine"},
                follow_redirects=False,
            )
            assert start.status_code == 303
            assert start.headers["location"].startswith(
                "https://idp.example.com/authorize"
            )

            async with engine_mod._session_factory() as session:
                login_state = (
                    await session.execute(select(OIDCLoginState))
                ).scalar_one()
                assert login_state.status == "pending"
                assert login_state.redirect_path == "/agents?tab=mine"
                assert (
                    login_state.nonce_ciphertext != "idp-code-verifier"
                )
                raw_state = start.headers["location"].split(
                    "state=",
                    maxsplit=1,
                )[1]

            callback = await client.get(
                "/auth/oidc/callback",
                params={"code": "idp-code", "state": raw_state},
                follow_redirects=False,
            )
            assert callback.status_code == 303
            assert callback.headers["location"] == "/agents?tab=mine"
            assert "session_token" in client.cookies
            assert "refresh_token" in client.cookies

    exchange_code.assert_awaited_once_with(
        "idp-code",
        "https://pas.example.com/auth/oidc/callback",
        code_verifier="idp-code-verifier",
    )
    extract_identity.assert_awaited_once()

    async with engine_mod._session_factory() as session:
        user = (
            await session.execute(
                select(User).where(
                    User.external_id == "enterprise:employee-1001"
                )
            )
        ).scalar_one()
        assert await session.scalar(
            select(UserWorkspace).where(UserWorkspace.user_id == user.id)
        )
        assert await session.scalar(
            select(UserExternalIdentity).where(
                UserExternalIdentity.user_id == user.id
            )
        )
        stored_state = (
            await session.execute(select(OIDCLoginState))
        ).scalar_one()
        assert stored_state.status == "consumed"
        assert stored_state.consumed_at is not None


async def test_oidc_mode_hides_builtin_login_and_keeps_admin_recovery(
    engine,
) -> None:
    config_module._config = _oidc_config()
    async with engine_mod._session_factory() as session:
        session.add_all(
            [
                User(
                    external_id="admin",
                    display_name="Admin",
                    password_hash=hash_password("admin-password"),
                    role=UserRole.ADMIN,
                ),
                User(
                    external_id="member",
                    display_name="Member",
                    password_hash=hash_password("member-password"),
                    role=UserRole.MEMBER,
                ),
            ]
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=_app()),
        base_url="https://pas.example.com",
    ) as client:
        normal = await client.post(
            "/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        assert normal.status_code == 409

        member = await client.post(
            "/auth/recovery/login",
            json={"username": "member", "password": "member-password"},
        )
        assert member.status_code == 401

        recovery = await client.post(
            "/auth/recovery/login",
            json={"username": "admin", "password": "admin-password"},
        )
        assert recovery.status_code == 200
        assert "session_token" in client.cookies
        assert "refresh_token" in client.cookies

        mode = await client.get("/auth/mode")
        assert mode.json() == {
            "mode": "oidc",
            "provider_name": "enterprise",
            "sso_login_url": "/auth/oidc/login",
            "recovery_login_path": "/login/recovery",
        }
