from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.auth.builtin import hash_password, verify_password
from server.auth.credential_mutation import (
    AdminPasswordAlreadyInitialized,
    BuiltinPasswordRequired,
    CredentialMutationMode,
    OldPasswordIncorrect,
    OldPasswordNotAllowed,
    PasswordModificationNotAllowed,
    mutate_builtin_password_in_session,
)
from server.auth.password_policy import PasswordPolicyError
from server.models import (
    AuthProvider,
    Base,
    PasswordState,
    User,
    UserRefreshToken,
)
from server.models.oauth import OAuthAuthorizationCode, OAuthRefreshToken


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


@pytest.fixture
async def user(session: AsyncSession) -> User:
    builtin_user = User(
        external_id="builtin-user",
        display_name="Builtin User",
        auth_provider=AuthProvider.BUILTIN,
        password_hash=hash_password("old-password-123"),
        password_state=PasswordState.ACTIVE,
    )
    session.add(builtin_user)
    await session.commit()
    return builtin_user


def _refresh_token(user_id: str, *, token_hash: str) -> UserRefreshToken:
    return UserRefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        token_family=f"family-{token_hash}",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )


async def _active_refresh_count(session: AsyncSession, user_id: str) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(UserRefreshToken)
        .where(
            UserRefreshToken.user_id == user_id,
            UserRefreshToken.revoked_at.is_(None),
        )
    )
    return int(count or 0)


async def test_reset_updates_hash_epoch_state_and_refresh_tokens(
    session: AsyncSession,
    user: User,
) -> None:
    tokens = [
        _refresh_token(user.id, token_hash="reset-token-a"),
        _refresh_token(user.id, token_hash="reset-token-b"),
    ]
    session.add_all(tokens)
    await session.commit()

    state = await mutate_builtin_password_in_session(
        session,
        user=user,
        mode=CredentialMutationMode.RESET,
        new_password="new-password-123",
    )

    assert state is PasswordState.ACTIVE
    assert user.credential_epoch == 2
    assert verify_password("new-password-123", user.password_hash or "")
    assert await _active_refresh_count(session, user.id) == 0
    await session.refresh(tokens[0])
    await session.refresh(tokens[1])
    assert tokens[0].revoked_at is not None
    assert tokens[0].revoked_at == tokens[1].revoked_at


async def test_reset_revokes_oauth_refresh_and_outstanding_codes(
    session: AsyncSession,
    user: User,
) -> None:
    code = OAuthAuthorizationCode(
        code_hash="a" * 64,
        client_id="managed-client",
        user_id=user.id,
        redirect_uri="http://localhost/callback",
        redirect_uri_provided_explicitly=True,
        code_challenge="challenge",
        code_challenge_method="S256",
        resource="http://localhost:18760/mcp",
        scopes="[]",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    oauth_refresh = OAuthRefreshToken(
        token_hash="b" * 64,
        client_id="managed-client",
        user_id=user.id,
        code_id=code.code_hash,
        token_family="oauth-family",
        scopes="[]",
        resource="http://localhost:18760/mcp",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    session.add_all([code, oauth_refresh])
    await session.commit()

    await mutate_builtin_password_in_session(
        session,
        user=user,
        mode=CredentialMutationMode.RESET,
        new_password="replacement-password-123",
    )

    await session.refresh(code)
    await session.refresh(oauth_refresh)
    assert code.consumed_at is not None
    assert oauth_refresh.revoked_at is not None
    assert code.consumed_at == oauth_refresh.revoked_at


async def test_modify_verifies_old_password_and_rotates_credentials(
    session: AsyncSession,
    user: User,
) -> None:
    session.add(_refresh_token(user.id, token_hash="modify-token"))
    await session.commit()

    await mutate_builtin_password_in_session(
        session,
        user=user,
        mode=CredentialMutationMode.MODIFY,
        old_password="old-password-123",
        new_password="modified-password-123",
    )

    assert user.credential_epoch == 2
    assert verify_password("modified-password-123", user.password_hash or "")
    assert await _active_refresh_count(session, user.id) == 0


async def test_modify_rejects_incorrect_old_password_without_changes(
    session: AsyncSession,
    user: User,
) -> None:
    old_hash = user.password_hash
    session.add(_refresh_token(user.id, token_hash="incorrect-token"))
    await session.commit()

    with pytest.raises(OldPasswordIncorrect) as captured:
        await mutate_builtin_password_in_session(
            session,
            user=user,
            mode=CredentialMutationMode.MODIFY,
            old_password="incorrect-password",
            new_password="modified-password-123",
        )

    assert "incorrect-password" not in repr(captured.value)
    assert user.password_hash == old_hash
    assert user.credential_epoch == 1
    assert await _active_refresh_count(session, user.id) == 1


async def test_initialize_is_allowed_only_for_reset_required_user(
    session: AsyncSession,
    user: User,
) -> None:
    user.password_state = PasswordState.RESET_REQUIRED
    await session.commit()

    await mutate_builtin_password_in_session(
        session,
        user=user,
        mode=CredentialMutationMode.INITIALIZE,
        new_password="initialized-password-123",
    )

    assert user.password_state == PasswordState.ACTIVE
    assert user.credential_epoch == 2
    with pytest.raises(AdminPasswordAlreadyInitialized):
        await mutate_builtin_password_in_session(
            session,
            user=user,
            mode=CredentialMutationMode.INITIALIZE,
            new_password="another-password-123",
        )


async def test_modify_requires_active_password_state(
    session: AsyncSession,
    user: User,
) -> None:
    user.password_state = PasswordState.RESET_REQUIRED
    await session.commit()

    with pytest.raises(PasswordModificationNotAllowed):
        await mutate_builtin_password_in_session(
            session,
            user=user,
            mode=CredentialMutationMode.MODIFY,
            old_password="old-password-123",
            new_password="modified-password-123",
        )

    assert user.credential_epoch == 1


async def test_invalid_password_policy_changes_nothing(
    session: AsyncSession,
    user: User,
) -> None:
    old_hash = user.password_hash
    session.add(_refresh_token(user.id, token_hash="policy-token"))
    await session.commit()

    with pytest.raises(PasswordPolicyError) as captured:
        await mutate_builtin_password_in_session(
            session,
            user=user,
            mode=CredentialMutationMode.RESET,
            new_password="short",
        )

    assert "short" not in repr(captured.value)
    assert user.password_hash == old_hash
    assert user.credential_epoch == 1
    assert await _active_refresh_count(session, user.id) == 1


async def test_oidc_user_is_rejected_without_changes(session: AsyncSession) -> None:
    oidc_user = User(
        external_id="oidc-user",
        display_name="OIDC User",
        auth_provider=AuthProvider.OIDC,
    )
    session.add(oidc_user)
    await session.commit()

    with pytest.raises(BuiltinPasswordRequired):
        await mutate_builtin_password_in_session(
            session,
            user=oidc_user,
            mode=CredentialMutationMode.RESET,
            new_password="new-password-123",
        )

    assert oidc_user.password_hash is None
    assert oidc_user.credential_epoch == 1


async def test_reset_rejects_supplied_old_password_without_changes(
    session: AsyncSession,
    user: User,
) -> None:
    old_hash = user.password_hash

    with pytest.raises(OldPasswordNotAllowed):
        await mutate_builtin_password_in_session(
            session,
            user=user,
            mode=CredentialMutationMode.RESET,
            old_password="old-password-123",
            new_password="new-password-123",
        )

    assert user.password_hash == old_hash
    assert user.credential_epoch == 1


async def test_rollback_preserves_hash_epoch_and_refresh_token_rows(
    session: AsyncSession,
    user: User,
) -> None:
    old_hash = user.password_hash
    token = _refresh_token(user.id, token_hash="rollback-token")
    session.add(token)
    await session.commit()

    with pytest.raises(RuntimeError, match="rollback mutation"):
        async with session.begin():
            await mutate_builtin_password_in_session(
                session,
                user=user,
                mode=CredentialMutationMode.RESET,
                new_password="new-password-123",
            )
            raise RuntimeError("rollback mutation")

    await session.refresh(user)
    await session.refresh(token)
    assert user.password_hash == old_hash
    assert user.password_state == PasswordState.ACTIVE
    assert user.credential_epoch == 1
    assert token.revoked_at is None


async def test_concurrent_reset_rejects_stale_inflight_self_change() -> None:
    from tests._postgres_capacity_harness import (
        HarnessDisabled,
        load_harness_config,
        run_in_isolated_schema,
    )

    try:
        config = load_harness_config()
    except HarnessDisabled as error:
        pytest.skip(str(error))

    async def exercise(factory, _schema_name: str) -> None:
        async with factory() as session:
            target = User(
                external_id="concurrent-user",
                display_name="Concurrent User",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=hash_password("old-password-123"),
                password_state=PasswordState.ACTIVE,
            )
            session.add(target)
            await session.commit()
            user_id = target.id

        async with factory() as reset_session, factory() as change_session:
            reset_user = await reset_session.get(User, user_id)
            stale_change_user = await change_session.get(User, user_id)
            assert reset_user is not None
            assert stale_change_user is not None

            await mutate_builtin_password_in_session(
                reset_session,
                user=reset_user,
                mode=CredentialMutationMode.RESET,
                new_password="reset-password-123",
            )

            async def change_stale_password() -> OldPasswordIncorrect | None:
                try:
                    await mutate_builtin_password_in_session(
                        change_session,
                        user=stale_change_user,
                        mode=CredentialMutationMode.MODIFY,
                        old_password="old-password-123",
                        new_password="changed-password-123",
                    )
                    await change_session.commit()
                except OldPasswordIncorrect as error:
                    await change_session.rollback()
                    return error
                return None

            change_task = asyncio.create_task(change_stale_password())
            try:
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(change_task),
                        timeout=0.25,
                    )
                await reset_session.commit()
                change_error = await asyncio.wait_for(change_task, timeout=5)
            finally:
                if reset_session.in_transaction():
                    await reset_session.rollback()
                if change_session.in_transaction():
                    await change_session.rollback()
                if not change_task.done():
                    change_task.cancel()
                    await asyncio.gather(change_task, return_exceptions=True)

            assert isinstance(change_error, OldPasswordIncorrect)

        async with factory() as session:
            target = await session.get(User, user_id)
            assert target is not None
            assert target.credential_epoch == 2
            assert verify_password(
                "reset-password-123",
                target.password_hash or "",
            )
            assert not verify_password(
                "changed-password-123",
                target.password_hash or "",
            )

    await run_in_isolated_schema(config, exercise)
