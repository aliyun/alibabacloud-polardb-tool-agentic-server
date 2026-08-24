from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    ACL_CONTEXT_PRINCIPAL_PROVIDERS,
    EXTERNAL_ENTERPRISE_PRINCIPAL_PROVIDERS,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    PolarRAGSpace,
    User,
    UserExternalIdentity,
)
from server.enterprise_identity.service import identity_provider_key


class IdentityContextUnavailable(PermissionError):
    pass


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def principal_assignment_is_valid_for_user(
    assignment: EnterprisePrincipalAssignment,
    user: User,
) -> bool:
    if assignment.provider not in ACL_CONTEXT_PRINCIPAL_PROVIDERS:
        return False
    if assignment.provider != "polarrag":
        return True
    return (
        assignment.principal_type == EnterprisePrincipalType.USER
        and assignment.principal_id == user.external_id
        and assignment.source == EnterprisePrincipalSource.ADMIN_MANAGED
    )


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
    if normalized_provider not in EXTERNAL_ENTERPRISE_PRINCIPAL_PROVIDERS:
        raise ValueError("provider is not allowed")
    normalized: list[dict[str, str]] = []
    for principal in principals:
        if principal.get("provider") != normalized_provider:
            raise ValueError("provider result does not match adapter")
        principal_type = principal.get("type")
        principal_id = principal.get("id")
        if principal_type not in {"user", "group", "department", "acl_group"}:
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


async def _resolve_source_context(
    session: AsyncSession,
    user_id: str,
    knowledge_space_id: str,
    current: datetime,
) -> tuple[
    User,
    PolarRAGSpace | None,
    str,
    list[tuple[EnterpriseIdentitySource, str]],
    bool,
]:
    user = await session.get(User, user_id)
    if user is None or not user.external_id:
        raise IdentityContextUnavailable("IDENTITY_CONTEXT_UNAVAILABLE")
    space = await session.get(PolarRAGSpace, knowledge_space_id)
    if space is None:
        # Existing administrator-managed mappings predate identity sources and
        # are keyed by domain. Source-backed identities always require a Space.
        return user, None, knowledge_space_id, [], False
    if not space.enabled:
        raise IdentityContextUnavailable("IDENTITY_CONTEXT_UNAVAILABLE")
    sources = list(
        (
            await session.execute(
                select(EnterpriseIdentitySource)
                .join(
                    EnterpriseIdentitySourceSpaceBinding,
                    EnterpriseIdentitySourceSpaceBinding.identity_source_id
                    == EnterpriseIdentitySource.id,
                )
                .where(
                    EnterpriseIdentitySourceSpaceBinding.knowledge_space_id
                    == knowledge_space_id,
                    EnterpriseIdentitySource.status
                    == EnterpriseIdentitySourceStatus.ACTIVE,
                )
            )
        ).scalars()
    )
    source_identities: list[tuple[EnterpriseIdentitySource, str]] = []
    for source in sources:
        if (
            source.last_synced_at is not None
            and _as_utc(source.last_synced_at)
            < current - timedelta(seconds=source.stale_after_seconds)
        ):
            continue
        external_user_id = (
            await session.execute(
                select(UserExternalIdentity.external_subject)
                .join(User, User.id == UserExternalIdentity.user_id)
                .join(
                    EnterpriseDirectoryUser,
                    EnterpriseDirectoryUser.external_user_id
                    == UserExternalIdentity.external_subject,
                )
                .where(
                    UserExternalIdentity.user_id == user_id,
                    UserExternalIdentity.identity_provider
                    == identity_provider_key(source),
                    EnterpriseDirectoryUser.identity_source_id == source.id,
                    EnterpriseDirectoryUser.status
                    == EnterpriseDirectoryEntryStatus.ACTIVE,
                )
            )
        ).scalar_one_or_none()
        if external_user_id is not None:
            source_identities.append((source, external_user_id))
    source_keys = {
        identity_provider_key(source)
        for source in (
            await session.execute(
                select(EnterpriseIdentitySource).where(
                    EnterpriseIdentitySource.tenant_id.is_not(None)
                )
            )
        ).scalars()
    }
    identity_providers = set(
        (
            await session.execute(
                select(UserExternalIdentity.identity_provider).where(
                    UserExternalIdentity.user_id == user_id
                )
            )
        ).scalars()
    )
    has_external_assignment = (
        await session.execute(
            select(EnterprisePrincipalAssignment.id).where(
                EnterprisePrincipalAssignment.pas_user_id == user_id,
                EnterprisePrincipalAssignment.provider.in_(
                    EXTERNAL_ENTERPRISE_PRINCIPAL_PROVIDERS
                ),
                EnterprisePrincipalAssignment.status
                == EnterprisePrincipalStatus.ACTIVE,
                or_(
                    EnterprisePrincipalAssignment.valid_until.is_(None),
                    EnterprisePrincipalAssignment.valid_until > current,
                ),
            )
        )
    ).first() is not None
    return (
        user,
        space,
        space.identity_domain,
        source_identities,
        bool(source_keys & identity_providers) or has_external_assignment,
    )


async def resolve_linked_pas_user_ids(
    session: AsyncSession,
    user_id: str,
    knowledge_space_id: str,
    *,
    now: datetime | None = None,
) -> set[str]:
    """Return the PAS user and source-generated accounts for its active identities."""
    user, _space, _identity_domain, source_identities, _has_enterprise_identity = (
        await _resolve_source_context(
            session,
            user_id,
            knowledge_space_id,
            now or datetime.now(UTC),
        )
    )
    generated_external_ids = {
        f"{identity_provider_key(source)}:{external_user_id}"
        for source, external_user_id in source_identities
    }
    linked_user_ids = {user.id}
    if generated_external_ids:
        linked_user_ids.update(
            (
                await session.execute(
                    select(User.id).where(User.external_id.in_(generated_external_ids))
                )
            ).scalars()
        )
    return linked_user_ids


async def resolve_acl_context(
    session: AsyncSession,
    user_id: str,
    knowledge_space_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    (
        user,
        space,
        identity_domain,
        source_identities,
        has_enterprise_identity,
    ) = await _resolve_source_context(session, user_id, knowledge_space_id, current)
    principals: set[tuple[str, str, str]] = set()
    for source, external_user_id in source_identities:
        provider = source.provider.value
        principals.add((provider, "user", external_user_id))
        frontier = {external_user_id}
        member_type = EnterpriseDirectoryMembershipType.USER
        visited_groups: set[str] = set()
        for _depth in range(8):
            if not frontier:
                break
            group_rows = (
                await session.execute(
                    select(
                        EnterpriseDirectoryGroup.external_group_id,
                        EnterpriseDirectoryGroup.principal_type,
                    )
                    .select_from(EnterpriseDirectoryMembership)
                    .join(
                        EnterpriseDirectoryGroup,
                        (
                            EnterpriseDirectoryGroup.identity_source_id
                            == EnterpriseDirectoryMembership.identity_source_id
                        )
                        & (
                            EnterpriseDirectoryGroup.external_group_id
                            == EnterpriseDirectoryMembership.external_group_id
                        ),
                    )
                    .where(
                        EnterpriseDirectoryMembership.identity_source_id
                        == source.id,
                        EnterpriseDirectoryMembership.member_type == member_type,
                        EnterpriseDirectoryMembership.external_member_id.in_(
                            frontier
                        ),
                        EnterpriseDirectoryGroup.status
                        == EnterpriseDirectoryEntryStatus.ACTIVE,
                    )
                )
            ).all()
            groups = {
                (str(group_id), principal_type)
                for group_id, principal_type in group_rows
                if str(group_id) not in visited_groups
            }
            for group_id, principal_type in groups:
                principals.add((provider, principal_type.value, group_id))
            frontier = {group_id for group_id, _principal_type in groups}
            visited_groups.update(frontier)
            member_type = EnterpriseDirectoryMembershipType.GROUP
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
    assignments = list(rows)
    if any(
        not principal_assignment_is_valid_for_user(assignment, user)
        for assignment in assignments
    ):
        raise IdentityContextUnavailable("IDENTITY_CONTEXT_UNAVAILABLE")
    principals.update(
        {
            (
                assignment.provider,
                assignment.principal_type.value,
                assignment.principal_id,
            )
            for assignment in assignments
        }
    )
    if not principals and (space is None or has_enterprise_identity):
        raise IdentityContextUnavailable("IDENTITY_CONTEXT_UNAVAILABLE")
    principals.add(("polarrag", "user", user.external_id))
    principals.update(
        (
            "polarrag",
            "user",
            f"{identity_provider_key(source)}:{external_user_id}",
        )
        for source, external_user_id in source_identities
    )
    return {
        "identity_domain": identity_domain,
        "principals": [
            {"provider": provider, "type": principal_type, "id": principal_id}
            for provider, principal_type, principal_id in sorted(principals)
        ],
    }
