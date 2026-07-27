"""Periodic sweeper for expired/consumed/revoked OAuth rows."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from server.models.oauth import (
    OAuthAuthorizationCode,
    OAuthDeniedJTI,
    OAuthPendingAuth,
    OAuthRefreshToken,
)

logger = logging.getLogger(__name__)

CONSUMED_CODE_RETENTION = timedelta(hours=24)
REVOKED_REFRESH_RETENTION = timedelta(days=7)


async def sweep_expired_oauth_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, int]:
    """Delete expired/consumed/revoked rows from all OAuth tables.

    Returns a dict mapping table name to number of rows deleted.
    """
    now = datetime.now(timezone.utc)
    counts: dict[str, int] = {}

    async with session_factory() as session:
        r = await session.execute(
            delete(OAuthPendingAuth).where(OAuthPendingAuth.expires_at < now)
        )
        counts["oauth_pending_auths"] = r.rowcount  # type: ignore[attr-defined]

        r = await session.execute(
            delete(OAuthAuthorizationCode).where(
                OAuthAuthorizationCode.consumed_at.is_not(None),
                OAuthAuthorizationCode.consumed_at < now - CONSUMED_CODE_RETENTION,
            )
        )
        expired_codes = await session.execute(
            delete(OAuthAuthorizationCode).where(
                OAuthAuthorizationCode.expires_at < now,
                OAuthAuthorizationCode.consumed_at.is_(None),
            )
        )
        counts["oauth_authorization_codes"] = r.rowcount + expired_codes.rowcount  # type: ignore[attr-defined]

        r = await session.execute(
            delete(OAuthRefreshToken).where(
                OAuthRefreshToken.revoked_at.is_not(None),
                OAuthRefreshToken.revoked_at < now - REVOKED_REFRESH_RETENTION,
            )
        )
        expired_rt = await session.execute(
            delete(OAuthRefreshToken).where(
                OAuthRefreshToken.expires_at.is_not(None),
                OAuthRefreshToken.expires_at < now,
                OAuthRefreshToken.revoked_at.is_(None),
            )
        )
        counts["oauth_refresh_tokens"] = r.rowcount + expired_rt.rowcount  # type: ignore[attr-defined]

        r = await session.execute(
            delete(OAuthDeniedJTI).where(OAuthDeniedJTI.expires_at < now)
        )
        counts["oauth_denied_jtis"] = r.rowcount  # type: ignore[attr-defined]

        await session.commit()

    total = sum(counts.values())
    if total > 0:
        logger.info("OAuth cleanup swept %d rows: %s", total, counts)
    return counts
