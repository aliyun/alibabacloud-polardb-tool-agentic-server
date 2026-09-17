from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from server.auth.jwt_manager import create_access_token
from server.config import get_config
from server.models import User
from server.models.user_refresh_token import UserRefreshToken


def secure_cookie(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",", 1)[0].strip() or request.url.scheme
    return scheme == "https"


def refresh_cookie_max_age() -> int:
    return get_config().auth.jwt.refresh_token_expire_days * 86400


def create_refresh_record(
    session: AsyncSession,
    user_id: str,
    family: str | None = None,
) -> str:
    config = get_config().auth.jwt
    token = secrets.token_urlsafe(32)
    session.add(
        UserRefreshToken(
            user_id=user_id,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            token_family=family or str(uuid.uuid4()),
            expires_at=datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(days=config.refresh_token_expire_days),
        )
    )
    return token


def issue_browser_session(
    request: Request,
    response: Response,
    session: AsyncSession,
    user: User,
) -> str:
    access_token = create_access_token(
        {
            "sub": user.id,
            "role": user.role.value,
            "credential_epoch": user.credential_epoch,
        }
    )
    refresh_token = create_refresh_record(session, user.id)
    config = get_config()
    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=secure_cookie(request),
        samesite="lax",
        max_age=config.auth.jwt.access_token_expire_minutes * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=secure_cookie(request),
        samesite="lax",
        max_age=refresh_cookie_max_age(),
    )
    return access_token
