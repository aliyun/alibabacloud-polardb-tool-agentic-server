from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.builtin import hash_password, verify_password
from server.auth.password_policy import validate_password
from server.models import (
    AuthProvider,
    OAuthAuthorizationCode,
    OAuthRefreshToken,
    PasswordState,
    User,
    UserRefreshToken,
)


class CredentialMutationMode(StrEnum):
    INITIALIZE = "INITIALIZE"
    MODIFY = "MODIFY"
    RESET = "RESET"


class CredentialMutationError(ValueError):
    """Base class for password-safe credential mutation errors."""


class BuiltinPasswordRequired(CredentialMutationError):
    def __init__(self) -> None:
        super().__init__("Password mutation requires a builtin user")


class AdminPasswordAlreadyInitialized(CredentialMutationError):
    def __init__(self) -> None:
        super().__init__("Administrator password is already initialized")


class PasswordModificationNotAllowed(CredentialMutationError):
    def __init__(self) -> None:
        super().__init__("Password modification requires active credentials")


class OldPasswordIncorrect(CredentialMutationError):
    def __init__(self) -> None:
        super().__init__("Current password is incorrect")


class OldPasswordNotAllowed(CredentialMutationError):
    def __init__(self) -> None:
        super().__init__("Current password is not accepted for password reset")


async def mutate_builtin_password_in_session(
    session: AsyncSession,
    *,
    user: User,
    mode: CredentialMutationMode,
    new_password: str,
    old_password: str | None = None,
) -> PasswordState:
    """Mutate builtin credentials inside the transaction owned by the caller."""
    await session.refresh(user, with_for_update=True)

    if user.auth_provider != AuthProvider.BUILTIN:
        raise BuiltinPasswordRequired

    validate_password(new_password)

    password_state = user.effective_password_state
    if mode is CredentialMutationMode.INITIALIZE:
        if password_state is not PasswordState.RESET_REQUIRED:
            raise AdminPasswordAlreadyInitialized
    elif mode is CredentialMutationMode.MODIFY:
        if password_state is not PasswordState.ACTIVE:
            raise PasswordModificationNotAllowed
        if old_password is None or user.password_hash is None or not verify_password(old_password, user.password_hash):
            raise OldPasswordIncorrect
    elif mode is CredentialMutationMode.RESET:
        if old_password is not None:
            raise OldPasswordNotAllowed

    user.password_hash = hash_password(new_password)
    user.password_state = PasswordState.ACTIVE
    user.credential_epoch += 1
    revoked_at = datetime.now(timezone.utc)
    await session.execute(
        update(UserRefreshToken)
        .where(
            UserRefreshToken.user_id == user.id,
            UserRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )
    await session.execute(
        update(OAuthRefreshToken)
        .where(
            OAuthRefreshToken.user_id == user.id,
            OAuthRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=revoked_at)
    )
    await session.execute(
        update(OAuthAuthorizationCode)
        .where(
            OAuthAuthorizationCode.user_id == user.id,
            OAuthAuthorizationCode.consumed_at.is_(None),
        )
        .values(consumed_at=revoked_at)
    )
    return PasswordState.ACTIVE
