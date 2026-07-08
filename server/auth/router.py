from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.builtin import authenticate_builtin
from server.auth.dependencies import get_current_user
from server.auth.jwt_manager import create_access_token
from server.config import get_config
from server.db.engine import get_session
from server.models import User
from server.models.user_refresh_token import UserRefreshToken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserInfoResponse(BaseModel):
    id: str
    external_id: str
    display_name: str
    email: str | None
    role: str
    status: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _refresh_cookie_max_age() -> int:
    return get_config().auth.jwt.refresh_token_expire_days * 86400


def _create_refresh_record(session: AsyncSession, user_id: str, family: str | None = None) -> str:
    """Create + add a UserRefreshToken, return the opaque token (caller commits)."""
    config = get_config().auth.jwt
    token = secrets.token_urlsafe(32)
    record = UserRefreshToken(
        user_id=user_id,
        token_hash=_hash_token(token),
        token_family=family or str(uuid.uuid4()),
        expires_at=datetime.now(timezone.utc).replace(microsecond=0)
        + timedelta(days=config.refresh_token_expire_days),
    )
    session.add(record)
    return token


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Login with builtin credentials (works in all auth modes for builtin users)."""
    config = get_config()
    user = await authenticate_builtin(session, body.username, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    from server.models import UserStatus
    if user.status == UserStatus.DISABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been disabled. Contact admin.",
        )

    access_token = create_access_token({"sub": user.id, "role": user.role.value})
    refresh_token = _create_refresh_record(session, user.id)
    await session.commit()

    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=not config.server.dev_mode,
        samesite="lax",
        max_age=config.auth.jwt.access_token_expire_minutes * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=not config.server.dev_mode,
        samesite="lax",
        max_age=_refresh_cookie_max_age(),
    )

    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Rotate refresh token + issue new access token. Reuse/expiry revokes family."""
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token.")
    token_hash = _hash_token(token)

    # Lookup including already-revoked rows (so reuse is detectable).
    result = await session.execute(
        select(UserRefreshToken).where(UserRefreshToken.token_hash == token_hash)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.")

    now = datetime.now(timezone.utc)

    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    # Reuse or expiry -> revoke whole family.
    if record.revoked_at is not None or expires_at < now:
        await session.execute(
            update(UserRefreshToken)
            .where(
                UserRefreshToken.token_family == record.token_family,
                UserRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()
        logger.warning("Refresh token reuse/expiry detected, family revoked: %s", record.token_family)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token no longer valid.")

    # CAS revoke the current token.
    cas = await session.execute(
        update(UserRefreshToken)
        .where(
            UserRefreshToken.id == record.id,
            UserRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    if cas.rowcount == 0:  # type: ignore[attr-defined]
        # Concurrent consumption: revoke family.
        await session.execute(
            update(UserRefreshToken)
            .where(
                UserRefreshToken.token_family == record.token_family,
                UserRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token no longer valid.")

    config = get_config()
    access_token = create_access_token({"sub": record.user_id})
    new_refresh = _create_refresh_record(session, record.user_id, family=record.token_family)
    await session.commit()

    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=not config.server.dev_mode,
        samesite="lax",
        max_age=config.auth.jwt.access_token_expire_minutes * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=new_refresh,
        httponly=True,
        secure=not config.server.dev_mode,
        samesite="lax",
        max_age=_refresh_cookie_max_age(),
    )
    return TokenResponse(access_token=access_token)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Revoke refresh token (if present) and clear both cookies."""
    config = get_config()
    token = request.cookies.get("refresh_token")
    if token:
        token_hash = _hash_token(token)
        await session.execute(
            update(UserRefreshToken)
            .where(
                UserRefreshToken.token_hash == token_hash,
                UserRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await session.commit()
    response.delete_cookie(
        "session_token",
        httponly=True,
        secure=not config.server.dev_mode,
        samesite="lax",
    )
    response.delete_cookie(
        "refresh_token",
        httponly=True,
        secure=not config.server.dev_mode,
        samesite="lax",
    )
    return {"message": "Logged out"}


@router.get("/me", response_model=UserInfoResponse)
async def get_me(user: User = Depends(get_current_user)):
    """Get current user info."""
    return UserInfoResponse(
        id=user.id,
        external_id=user.external_id,
        display_name=user.display_name,
        email=user.email,
        role=user.role.value,
        status=user.status.value,
    )


@router.get("/mode")
async def get_auth_mode():
    """Return auth mode so frontend can conditionally show password UI."""
    config = get_config()
    return {"mode": config.auth.mode}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Change own password (builtin auth mode only)."""
    from server.auth.builtin import verify_password, hash_password
    from server.models import AuthProvider

    if user.auth_provider != AuthProvider.BUILTIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password change is only available for builtin auth users.",
        )
    if not user.password_hash or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )
    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 8 characters.",
        )

    user.password_hash = hash_password(body.new_password)
    await session.commit()
    return {"message": "Password changed successfully"}
