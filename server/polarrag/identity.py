from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    ENTERPRISE_PRINCIPAL_PROVIDERS,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalStatus,
)


class IdentityContextUnavailable(PermissionError):
    pass


class EnterpriseIdentityProvider(Protocol):
    @property
    def provider(self) -> str: ...

    async def authenticate(self, assertion: dict[str, Any]) -> None: ...

    async def resolve_user_principal(
        self,
        external_user_id: str,
    ) -> dict[str, str]: ...

    async def resolve_group_principals(
        self,
        external_user_id: str,
    ) -> list[dict[str, str]]: ...

    async def healthcheck(self) -> bool: ...


def validate_provider_results(
    provider: str,
    principals: list[dict[str, str]],
) -> list[dict[str, str]]:
    normalized_provider = provider.strip().lower()
    if normalized_provider not in ENTERPRISE_PRINCIPAL_PROVIDERS:
        raise ValueError("provider is not allowed")
    normalized: list[dict[str, str]] = []
    for principal in principals:
        if principal.get("provider") != normalized_provider:
            raise ValueError("provider result does not match adapter")
        principal_type = principal.get("type")
        principal_id = principal.get("id")
        if principal_type not in {"user", "group"}:
            raise ValueError("principal type is invalid")
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise ValueError("principal id is invalid")
        normalized.append(
            {
                "provider": normalized_provider,
                "type": principal_type,
                "id": principal_id.strip(),
            }
        )
    return normalized


async def resolve_acl_context(
    session: AsyncSession,
    user_id: str,
    identity_domain: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    rows = (
        await session.execute(
            select(EnterprisePrincipalAssignment).where(
                EnterprisePrincipalAssignment.pas_user_id == user_id,
                EnterprisePrincipalAssignment.identity_domain == identity_domain,
                EnterprisePrincipalAssignment.status == EnterprisePrincipalStatus.ACTIVE,
                or_(
                    EnterprisePrincipalAssignment.valid_until.is_(None),
                    EnterprisePrincipalAssignment.valid_until > current,
                ),
            )
        )
    ).scalars()
    principals = sorted(
        {
            (
                assignment.provider,
                assignment.principal_type.value,
                assignment.principal_id,
            )
            for assignment in rows
        }
    )
    if not principals:
        raise IdentityContextUnavailable("IDENTITY_CONTEXT_UNAVAILABLE")
    return {
        "identity_domain": identity_domain,
        "principals": [
            {"provider": provider, "type": principal_type, "id": principal_id}
            for provider, principal_type, principal_id in principals
        ],
    }
