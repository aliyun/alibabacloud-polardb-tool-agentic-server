from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from mcp.shared.auth import OAuthClientInformationFull
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request
from starlette.responses import Response

from server.auth.builtin import hash_password
from server.auth.credential_mutation import (
    CredentialMutationMode,
    mutate_builtin_password_in_session,
)
from server.auth.oauth_provider import PASAuthProvider
from server.auth.router import (
    LoginRequest,
    _create_refresh_record,
    login,
    refresh,
)
from server.config import AppConfig
from server.models import (
    AuthProvider,
    PasswordState,
    User,
    UserRefreshToken,
)
from server.models.oauth import OAuthAuthorizationCode, OAuthRefreshToken
from tests._helpers import init_test_jwt_keys
from tests._postgres_capacity_harness import (
    HarnessDisabled,
    load_harness_config,
    run_in_isolated_schema,
)


OLD_PASSWORD = "postgres-old-password"
NEW_PASSWORD = "postgres-new-password"
RESOURCE = "http://localhost:18760/mcp"


def _integration_config():
    try:
        return load_harness_config()
    except HarnessDisabled as error:
        pytest.skip(str(error))


def _request(path: str, *, refresh_token: str | None = None) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if refresh_token is not None:
        headers.append((b"cookie", f"refresh_token={refresh_token}".encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        }
    )


async def _create_builtin_user(factory: async_sessionmaker) -> str:
    async with factory() as session:
        user = User(
            external_id="postgres-user",
            display_name="PostgreSQL User",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password(OLD_PASSWORD),
            password_state=PasswordState.ACTIVE,
        )
        session.add(user)
        await session.commit()
        return user.id


async def _lock_user(session, user_id: str) -> User:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    assert user is not None
    return user


async def _assert_task_waits(task: asyncio.Task) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.25)


async def test_postgres_reset_serializes_with_builtin_login() -> None:
    config = _integration_config()
    init_test_jwt_keys()

    async def exercise(factory: async_sessionmaker, _schema: str) -> None:
        user_id = await _create_builtin_user(factory)
        async with factory() as reset_session, factory() as login_session:
            reset_user = await _lock_user(reset_session, user_id)

            async def attempt_login() -> int:
                try:
                    await login(
                        LoginRequest(
                            username="postgres-user",
                            password=OLD_PASSWORD,
                        ),
                        _request("/auth/login"),
                        Response(),
                        login_session,
                    )
                except HTTPException as error:
                    await login_session.rollback()
                    return error.status_code
                return 200

            task = asyncio.create_task(attempt_login())
            try:
                await _assert_task_waits(task)
                await mutate_builtin_password_in_session(
                    reset_session,
                    user=reset_user,
                    mode=CredentialMutationMode.RESET,
                    new_password=NEW_PASSWORD,
                )
                await reset_session.commit()
                assert await asyncio.wait_for(task, timeout=5) == 401
            finally:
                if reset_session.in_transaction():
                    await reset_session.rollback()
                if login_session.in_transaction():
                    await login_session.rollback()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        async with factory() as session:
            active = await session.scalar(
                select(func.count()).select_from(UserRefreshToken).where(UserRefreshToken.revoked_at.is_(None))
            )
            assert active == 0

    await run_in_isolated_schema(config, exercise)


async def test_postgres_reset_serializes_with_rest_refresh() -> None:
    config = _integration_config()
    init_test_jwt_keys()

    async def exercise(factory: async_sessionmaker, _schema: str) -> None:
        user_id = await _create_builtin_user(factory)
        async with factory() as setup_session:
            refresh_token = _create_refresh_record(setup_session, user_id)
            await setup_session.commit()

        async with factory() as reset_session, factory() as refresh_session:
            reset_user = await _lock_user(reset_session, user_id)

            async def attempt_refresh() -> int:
                try:
                    await refresh(
                        _request(
                            "/auth/refresh",
                            refresh_token=refresh_token,
                        ),
                        Response(),
                        refresh_session,
                    )
                except HTTPException as error:
                    await refresh_session.rollback()
                    return error.status_code
                return 200

            task = asyncio.create_task(attempt_refresh())
            try:
                await _assert_task_waits(task)
                await mutate_builtin_password_in_session(
                    reset_session,
                    user=reset_user,
                    mode=CredentialMutationMode.RESET,
                    new_password=NEW_PASSWORD,
                )
                await reset_session.commit()
                assert await asyncio.wait_for(task, timeout=5) == 401
            finally:
                if reset_session.in_transaction():
                    await reset_session.rollback()
                if refresh_session.in_transaction():
                    await refresh_session.rollback()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        async with factory() as session:
            active = await session.scalar(
                select(func.count()).select_from(UserRefreshToken).where(UserRefreshToken.revoked_at.is_(None))
            )
            assert active == 0

    await run_in_isolated_schema(config, exercise)


def _oauth_provider(factory: async_sessionmaker) -> PASAuthProvider:
    return PASAuthProvider(
        session_factory=factory,
        config=AppConfig(
            server={
                "dev_mode": True,
                "public_base_url": "http://localhost:18760",
            }
        ),
    )


def _oauth_client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id="postgres-client",
        redirect_uris=["http://localhost/callback"],
    )


async def test_postgres_reset_serializes_with_oauth_code_exchange() -> None:
    config = _integration_config()
    init_test_jwt_keys()

    async def exercise(factory: async_sessionmaker, _schema: str) -> None:
        user_id = await _create_builtin_user(factory)
        code = "postgres-authorization-code"
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        async with factory() as session:
            session.add(
                OAuthAuthorizationCode(
                    code_hash=code_hash,
                    client_id="postgres-client",
                    user_id=user_id,
                    redirect_uri="http://localhost/callback",
                    redirect_uri_provided_explicitly=True,
                    code_challenge="challenge",
                    code_challenge_method="S256",
                    resource=RESOURCE,
                    scopes="[]",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                )
            )
            await session.commit()
        provider = _oauth_provider(factory)
        loaded = await provider.load_authorization_code(_oauth_client(), code)
        assert loaded is not None

        async with factory() as reset_session:
            reset_user = await _lock_user(reset_session, user_id)

            async def exchange_code() -> str:
                try:
                    await provider.exchange_authorization_code(_oauth_client(), loaded)
                except ValueError:
                    return "rejected"
                return "issued"

            task = asyncio.create_task(exchange_code())
            try:
                await _assert_task_waits(task)
                await mutate_builtin_password_in_session(
                    reset_session,
                    user=reset_user,
                    mode=CredentialMutationMode.RESET,
                    new_password=NEW_PASSWORD,
                )
                await reset_session.commit()
                assert await asyncio.wait_for(task, timeout=5) == "rejected"
            finally:
                if reset_session.in_transaction():
                    await reset_session.rollback()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        async with factory() as session:
            active = await session.scalar(
                select(func.count()).select_from(OAuthRefreshToken).where(OAuthRefreshToken.revoked_at.is_(None))
            )
            assert active == 0

    await run_in_isolated_schema(config, exercise)


async def test_postgres_reset_serializes_with_oauth_refresh() -> None:
    config = _integration_config()
    init_test_jwt_keys()

    async def exercise(factory: async_sessionmaker, _schema: str) -> None:
        user_id = await _create_builtin_user(factory)
        plaintext = "postgres-oauth-refresh"
        token_hash = hashlib.sha256(plaintext.encode()).hexdigest()
        async with factory() as session:
            session.add(
                OAuthRefreshToken(
                    token_hash=token_hash,
                    client_id="postgres-client",
                    user_id=user_id,
                    code_id=None,
                    token_family="postgres-family",
                    scopes="[]",
                    resource=RESOURCE,
                    expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                )
            )
            await session.commit()
        provider = _oauth_provider(factory)
        loaded = await provider.load_refresh_token(_oauth_client(), plaintext)
        assert loaded is not None

        async with factory() as reset_session:
            reset_user = await _lock_user(reset_session, user_id)

            async def exchange_refresh() -> str:
                try:
                    await provider.exchange_refresh_token(_oauth_client(), loaded, [])
                except ValueError:
                    return "rejected"
                return "issued"

            task = asyncio.create_task(exchange_refresh())
            try:
                await _assert_task_waits(task)
                await mutate_builtin_password_in_session(
                    reset_session,
                    user=reset_user,
                    mode=CredentialMutationMode.RESET,
                    new_password=NEW_PASSWORD,
                )
                await reset_session.commit()
                assert await asyncio.wait_for(task, timeout=5) == "rejected"
            finally:
                if reset_session.in_transaction():
                    await reset_session.rollback()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

        async with factory() as session:
            active = await session.scalar(
                select(func.count()).select_from(OAuthRefreshToken).where(OAuthRefreshToken.revoked_at.is_(None))
            )
            assert active == 0

    await run_in_isolated_schema(config, exercise)
