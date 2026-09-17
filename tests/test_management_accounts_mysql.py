from __future__ import annotations

import os
import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request
from starlette.responses import Response

from server.auth.builtin import hash_password
from server.auth.credential_mutation import (
    CredentialMutationMode,
    mutate_builtin_password_in_session,
)
from server.auth.router import (
    LoginRequest,
    _create_refresh_record,
    login,
    refresh,
)
from server.configuration.repository import ConfigRepository
from server.configuration.service import ConfigService
from server.core.config_crypto import ConfigCrypto
from server.management.accounts import ManagedAccountError, ManagedAccountService
from server.management.identity import ManagedIdentityBinder
from server.management.settings import ManagedIdentitySettings
from server.management.types import ManagedTarget
from server.models import (
    AuthProvider,
    Base,
    PasswordState,
    User,
    UserRefreshToken,
    UserRole,
    UserStatus,
)
from tests._helpers import init_test_jwt_keys


def _mysql_url() -> str:
    value = os.environ.get("PAS_TEST_MYSQL_URL", "").strip()
    if not value:
        pytest.skip("PAS_TEST_MYSQL_URL is not configured")
    if os.environ.get("PAS_TEST_MYSQL_DATABASE_OK") != "1":
        pytest.skip("PAS_TEST_MYSQL_DATABASE_OK=1 is required for the disposable test database")
    parsed = make_url(value)
    if parsed.drivername != "mysql+asyncmy" or not parsed.database:
        pytest.fail("PAS_TEST_MYSQL_URL must use mysql+asyncmy and name a disposable database")
    return value


async def test_mysql_case_insensitive_admin_match_is_rejected() -> None:
    engine = create_async_engine(_mysql_url())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            collation = await connection.scalar(text("SELECT @@collation_database"))
        assert isinstance(collation, str)
        assert collation.lower().endswith("_ci")

        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = ConfigRepository(factory)
        await repository.ensure_setup_status()
        await repository.bind_managed_identity(
            instance_id="pmcp-mysql",
            generation=1,
        )
        async with factory() as session:
            mixed_case = User(
                external_id="Admin",
                display_name="Mixed Case Administrator",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=hash_password("mysql-test-password"),
                password_state=PasswordState.ACTIVE,
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
            session.add(mixed_case)
            await session.commit()
            database_match = await session.scalar(select(User).where(User.external_id == "admin"))
            assert database_match is not None
            assert database_match.external_id == "Admin"

        service = ConfigService(
            repository,
            ConfigCrypto(b"01234567890123456789012345678901"),
        )
        account_service = ManagedAccountService(
            service,
            ManagedIdentityBinder(
                repository,
                ManagedIdentitySettings("pmcp-mysql", 1),
            ),
        )

        with pytest.raises(ManagedAccountError) as captured:
            await account_service.describe_admin(
                ManagedTarget(
                    instance_id="pmcp-mysql",
                    generation=1,
                )
            )

        assert captured.value.code == "ACCOUNT_NOT_FOUND"
    finally:
        await engine.dispose()


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


async def _lock_mysql_user(session, external_id: str) -> User:
    user = await session.scalar(select(User).where(User.external_id == external_id).with_for_update())
    assert user is not None
    return user


async def _create_mysql_race_user(factory, external_id: str) -> str:
    async with factory() as session:
        user = User(
            external_id=external_id,
            display_name="MySQL Race User",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("mysql-old-password"),
            password_state=PasswordState.ACTIVE,
        )
        session.add(user)
        await session.commit()
        return user.id


async def test_mysql_reset_serializes_with_builtin_login() -> None:
    init_test_jwt_keys()
    engine = create_async_engine(_mysql_url())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        external_id = "mysql-race-login"
        user_id = await _create_mysql_race_user(factory, external_id)

        async with factory() as reset_session, factory() as login_session:
            reset_user = await _lock_mysql_user(reset_session, external_id)

            async def attempt_login() -> int:
                try:
                    await login(
                        LoginRequest(
                            username=external_id,
                            password="mysql-old-password",
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
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
                await mutate_builtin_password_in_session(
                    reset_session,
                    user=reset_user,
                    mode=CredentialMutationMode.RESET,
                    new_password="mysql-new-password",
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
                select(UserRefreshToken).where(
                    UserRefreshToken.user_id == user_id,
                    UserRefreshToken.revoked_at.is_(None),
                )
            )
            assert active is None
    finally:
        await engine.dispose()


async def test_mysql_reset_serializes_with_rest_refresh() -> None:
    init_test_jwt_keys()
    engine = create_async_engine(_mysql_url())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        external_id = "mysql-race-refresh"
        user_id = await _create_mysql_race_user(factory, external_id)
        async with factory() as setup_session:
            refresh_token = _create_refresh_record(setup_session, user_id)
            await setup_session.commit()

        async with factory() as reset_session, factory() as refresh_session:
            reset_user = await _lock_mysql_user(reset_session, external_id)

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
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.25)
                await mutate_builtin_password_in_session(
                    reset_session,
                    user=reset_user,
                    mode=CredentialMutationMode.RESET,
                    new_password="mysql-new-password",
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
                select(UserRefreshToken).where(
                    UserRefreshToken.user_id == user_id,
                    UserRefreshToken.revoked_at.is_(None),
                )
            )
            assert active is None
    finally:
        await engine.dispose()
