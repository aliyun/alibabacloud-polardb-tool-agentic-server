from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    EnterpriseDirectoryPrincipalType,
    ExternalUserPrincipalMembership,
)


async def resolve_external_user_principals(
    session: AsyncSession,
    identity_source_id: str,
    external_user_id: str,
    *,
    now: datetime | None = None,
) -> set[tuple[EnterpriseDirectoryPrincipalType, str]]:
    current = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(
                ExternalUserPrincipalMembership.principal_type,
                ExternalUserPrincipalMembership.principal_id,
            ).where(
                ExternalUserPrincipalMembership.identity_source_id
                == identity_source_id,
                ExternalUserPrincipalMembership.external_user_id
                == external_user_id,
                or_(
                    ExternalUserPrincipalMembership.expires_at.is_(None),
                    ExternalUserPrincipalMembership.expires_at > current,
                ),
            )
        )
    ).all()
    return {(principal_type, str(principal_id)) for principal_type, principal_id in rows}
