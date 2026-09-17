"""Personal MCP credentials in the existing opaque OAuth credential store.

The reserved client namespace is never a registered OAuth client. Its credentials
are bearer access tokens, not refresh tokens, and must not enter token exchange.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import OAuthRefreshToken, User, UserStatus
from server.models.base import utc_now

TOKEN_PREFIX = "pas_personal_"
CLIENT_ID = "pas-internal-personal-access-v1"


class ActiveTokenExists(ValueError):
    pass


def normalized(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def status(row: OAuthRefreshToken, user: User | None = None) -> str:
    if user is not None and row.code_id != f"personal:epoch:{user.credential_epoch}":
        return "revoked"
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at is None or normalized(row.expires_at) <= utc_now():
        return "expired"
    return "active"


async def list_tokens(session: AsyncSession, user_id: str) -> list[OAuthRefreshToken]:
    return list(
        (
            await session.scalars(
                select(OAuthRefreshToken)
                .where(
                    OAuthRefreshToken.client_id == CLIENT_ID,
                    OAuthRefreshToken.user_id == user_id,
                )
                .order_by(OAuthRefreshToken.created_at.desc())
                .limit(20)
            )
        ).all()
    )


async def issue(
    session: AsyncSession, user_id: str, *, resource: str, expires_in_days: int = 90
) -> tuple[OAuthRefreshToken, str]:
    if not 1 <= expires_in_days <= 365:
        raise ValueError("Expiry must be between 1 and 365 days")
    user = await session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    if user is None or user.status != UserStatus.ACTIVE:
        raise LookupError("Personal account is unavailable")
    active = await session.scalar(
        select(OAuthRefreshToken.token_hash)
        .where(
            OAuthRefreshToken.client_id == CLIENT_ID,
            OAuthRefreshToken.user_id == user_id,
            OAuthRefreshToken.revoked_at.is_(None),
            OAuthRefreshToken.expires_at > utc_now(),
            OAuthRefreshToken.code_id == f"personal:epoch:{user.credential_epoch}",
        )
        .limit(1)
        .with_for_update()
    )
    if active is not None:
        raise ActiveTokenExists("Revoke the existing personal token before issuing another")
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = OAuthRefreshToken(
        token_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        client_id=CLIENT_ID,
        user_id=user_id,
        token_family=str(uuid.uuid4()),
        code_id=f"personal:epoch:{user.credential_epoch}",
        scopes="[]",
        resource=resource,
        expires_at=utc_now() + timedelta(days=expires_in_days),
    )
    session.add(row)
    await session.flush()
    return row, plaintext


async def revoke(session: AsyncSession, user_id: str, token_id: str) -> OAuthRefreshToken:
    # Match issuance/password-rotation lock order: user, then credential.
    await session.scalar(
        select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True)
    )
    row = await session.scalar(
        select(OAuthRefreshToken)
        .where(
            OAuthRefreshToken.client_id == CLIENT_ID,
            OAuthRefreshToken.user_id == user_id,
            OAuthRefreshToken.token_family == token_id,
        )
        .with_for_update()
    )
    if row is None:
        raise LookupError("Personal token not found")
    row.revoked_at = row.revoked_at or utc_now()
    await session.flush()
    return row


async def resolve(session: AsyncSession, plaintext: str) -> tuple[User, OAuthRefreshToken] | None:
    if not plaintext.startswith(TOKEN_PREFIX) or len(plaintext) > 128:
        return None
    row = await session.scalar(
        select(OAuthRefreshToken).where(
            OAuthRefreshToken.token_hash == hashlib.sha256(plaintext.encode()).hexdigest(),
            OAuthRefreshToken.client_id == CLIENT_ID,
        )
    )
    if row is None or status(row) != "active":
        return None
    user = await session.get(User, row.user_id)
    if user is None or user.status != UserStatus.ACTIVE or row.code_id != f"personal:epoch:{user.credential_epoch}":
        return None
    return user, row
