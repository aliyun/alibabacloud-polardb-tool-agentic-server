from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Iterable, Mapping
from datetime import UTC, datetime, timedelta
import asyncio
from contextlib import aclosing
import json
import logging
from typing import Any, Callable, cast

from sqlalchemy import delete, exists, literal, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from server.enterprise_identity.providers import acl_membership_snapshot_config, directory_sync_adapter_for
from server.models import (
    AuthProvider,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    EnterpriseIdentitySourceEvent,
    EnterpriseIdentitySourceEventStatus,
    IdentitySourceProvider,
    User,
    UserExternalIdentity,
    UserRefreshToken,
    UserRole,
    UserStatus,
)
from server.models.base import utc_now
from server.models.oauth import ExternalTokenSession, OAuthRefreshToken


_FEISHU_DIRECTORY_UPDATE_EVENTS = frozenset({
    "contact.user.created_v3",
    "contact.user.updated_v3",
    "contact.department.created_v3",
    "contact.department.updated_v3",
})
logger = logging.getLogger(__name__)


def _merge_directory_snapshots(
    *,
    groups: Iterable[Mapping[str, object]],
    memberships: Iterable[Mapping[str, object]],
    extra_groups: Iterable[Mapping[str, object]],
    extra_memberships: Iterable[Mapping[str, object]],
) -> tuple[list[Mapping[str, object]], list[Mapping[str, object]]]:
    groups_by_id: dict[str, Mapping[str, object]] = {}
    for group in (*groups, *extra_groups):
        group_id = _required_external_id(str(group.get("id") or ""), "directory group id")
        principal_type = str(group.get("principal_type") or "group")
        existing = groups_by_id.get(group_id)
        if existing is not None and existing.get("principal_type", "group") != principal_type:
            raise ValueError("directory group has conflicting principal types")
        groups_by_id[group_id] = group
    membership_records: dict[tuple[str, str, str], Mapping[str, object]] = {}
    for membership in (*memberships, *extra_memberships):
        group_id = _required_external_id(str(membership.get("group_id") or ""), "directory group id")
        member_type = _required_external_id(str(membership.get("member_type") or ""), "directory member type")
        member_id = _required_external_id(str(membership.get("member_id") or ""), "directory member id")
        membership_records[(group_id, member_type, member_id)] = membership
    return (
        [groups_by_id[group_id] for group_id in sorted(groups_by_id)],
        [membership_records[key] for key in sorted(membership_records)],
    )


def identity_provider_key(source: EnterpriseIdentitySource) -> str:
    return f"{source.provider.value}:{source.tenant_id}"


def identity_source_snapshot_is_usable(
    source: EnterpriseIdentitySource,
    *,
    now: datetime | None = None,
) -> bool:
    if source.provider == IdentitySourceProvider.DINGTALK:
        current = now or datetime.now(UTC)
        marker = source.last_synced_at
        if source.permission_error:
            return False
        if marker is None or source.status not in {EnterpriseIdentitySourceStatus.ACTIVE, EnterpriseIdentitySourceStatus.STALE}:
            return False
        if marker.tzinfo is None:
            marker = marker.replace(tzinfo=UTC)
        return marker >= current - timedelta(seconds=min(source.stale_after_seconds, 900))
    if source.status == EnterpriseIdentitySourceStatus.ACTIVE:
        return True
    if source.status != EnterpriseIdentitySourceStatus.STALE or source.last_synced_at is None:
        return False
    current = now or datetime.now(UTC)
    last_synced_at = source.last_synced_at
    if last_synced_at.tzinfo is None:
        last_synced_at = last_synced_at.replace(tzinfo=UTC)
    return bool(
        last_synced_at
        >= current
        - timedelta(
            seconds=source.stale_after_seconds,
        )
    )


def _required_external_id(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > 255:
        raise ValueError(f"{label} is too long")
    return normalized


async def _disable_source_owned_users(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    *,
    external_user_id: str | None = None,
) -> None:
    """Disable only canonical, single-identity PAS accounts absent from the directory."""
    user = aliased(User)
    identity = aliased(UserExternalIdentity)
    other_identity = aliased(UserExternalIdentity)
    owned_users = (
        select(user.id)
        .join(identity, identity.user_id == user.id)
        .join(
            EnterpriseDirectoryUser,
            (EnterpriseDirectoryUser.identity_source_id == source.id)
            & (EnterpriseDirectoryUser.external_user_id == identity.external_subject),
        )
        .where(
            identity.identity_provider == identity_provider_key(source),
            EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.DISABLED,
            user.external_id == literal(f"{identity_provider_key(source)}:") + identity.external_subject,
            user.auth_provider == AuthProvider.OIDC,
            ~exists(
                select(other_identity.id).where(
                    other_identity.user_id == user.id,
                    other_identity.id != identity.id,
                )
            ),
        )
    )
    if external_user_id is not None:
        owned_users = owned_users.where(identity.external_subject == external_user_id)
    # MySQL forbids updating users while selecting it directly in a subquery.
    # DISTINCT prevents derived-table merging, so the candidate set is
    # materialized even when optimizer_switch.derived_merge is enabled.
    owned_user_ids = owned_users.distinct().subquery()
    owned_users = select(owned_user_ids.c.id)
    await session.execute(update(User).where(User.id.in_(owned_users)).values(status=UserStatus.DISABLED))
    revoked_at = utc_now()
    await session.execute(
        update(UserRefreshToken)
        .where(UserRefreshToken.user_id.in_(owned_users), UserRefreshToken.revoked_at.is_(None))
        .values(revoked_at=revoked_at)
    )
    await session.execute(
        update(OAuthRefreshToken)
        .where(OAuthRefreshToken.user_id.in_(owned_users), OAuthRefreshToken.revoked_at.is_(None))
        .values(revoked_at=revoked_at)
    )
    await session.execute(
        update(ExternalTokenSession)
        .where(ExternalTokenSession.user_id.in_(owned_users), ExternalTokenSession.revoked_at.is_(None))
        .values(revoked_at=revoked_at)
    )


class _StreamingDirectorySink:
    """Persist directory pages immediately and prune only after a full run."""

    def __init__(
        self,
        session: AsyncSession,
        source: EnterpriseIdentitySource,
        *,
        sync_worker_id: str | None = None,
    ) -> None:
        self._session = session
        self._source = source
        self._sync_worker_id = sync_worker_id
        self._lock = asyncio.Lock()
        self._started_at: datetime | None = None
        self._sync_warning: dict[str, object] | None = None

    async def begin_sync(self, started_at: str) -> None:
        try:
            marker = datetime.fromisoformat(started_at)
        except ValueError as exc:
            raise ValueError("identity source sync checkpoint is invalid") from exc
        async with self._lock:
            self._started_at = marker.replace(microsecond=0)

    async def _commit_page(self) -> None:
        if self._sync_worker_id is not None:
            fenced = await self._session.execute(
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.id == self._source.id,
                    EnterpriseIdentitySource.sync_worker_id == self._sync_worker_id,
                )
                .values(sync_worker_id=self._sync_worker_id)
                .execution_options(synchronize_session=False)
            )
            if cast(Any, fenced).rowcount != 1:
                await self._session.rollback()
                raise RuntimeError("identity source sync ownership was lost")
        await self._session.flush()
        await self._session.commit()

    async def upsert_users(self, users: Iterable[Mapping[str, object]]) -> None:
        async with self._lock:
            for record in users:
                await upsert_external_user(
                    self._session,
                    self._source,
                    external_user_id=str(record.get("id") or ""),
                    display_name=str(record.get("display_name") or record.get("id") or ""),
                    email=str(record["email"]) if record.get("email") is not None else None,
                    synchronize_status=True,
                )
            await self._commit_page()

    async def upsert_groups(self, groups: Iterable[Mapping[str, object]]) -> None:
        records: dict[str, tuple[str, EnterpriseDirectoryPrincipalType]] = {}
        for record in groups:
            group_id = _required_external_id(
                str(record.get("id") or ""),
                "directory group id",
            )
            try:
                principal_type = EnterpriseDirectoryPrincipalType(str(record.get("principal_type") or "group"))
            except ValueError as exc:
                raise ValueError("directory group principal type is invalid") from exc
            records[group_id] = (
                _required_external_id(
                    str(record.get("display_name") or group_id),
                    "directory group display name",
                ),
                principal_type,
            )
        if not records:
            return
        async with self._lock:
            existing: dict[str, EnterpriseDirectoryGroup] = {}
            group_ids = sorted(records)
            for index in range(0, len(group_ids), 500):
                rows = (
                    await self._session.execute(
                        select(EnterpriseDirectoryGroup).where(
                            EnterpriseDirectoryGroup.identity_source_id == self._source.id,
                            EnterpriseDirectoryGroup.external_group_id.in_(group_ids[index : index + 500]),
                        )
                    )
                ).scalars()
                existing.update((group.external_group_id, group) for group in rows)
            current = utc_now()
            for group_id, (display_name, principal_type) in records.items():
                group = existing.get(group_id)
                if group is None:
                    self._session.add(
                        EnterpriseDirectoryGroup(
                            identity_source_id=self._source.id,
                            external_group_id=group_id,
                            display_name=display_name,
                            principal_type=principal_type,
                            status=EnterpriseDirectoryEntryStatus.ACTIVE,
                        )
                    )
                    continue
                group.display_name = display_name
                group.principal_type = principal_type
                group.status = EnterpriseDirectoryEntryStatus.ACTIVE
                group.updated_at = current
            await self._commit_page()

    async def upsert_memberships(self, memberships: Iterable[Mapping[str, object]]) -> None:
        records = list(memberships)
        if not records:
            return
        async with self._lock:
            group_ids = {
                _required_external_id(
                    str(record.get("group_id") or ""),
                    "directory group id",
                )
                for record in records
            }
            known_group_ids: set[str] = set()
            group_id_list = sorted(group_ids)
            for index in range(0, len(group_id_list), 500):
                known_group_ids.update(
                    (
                        await self._session.execute(
                            select(EnterpriseDirectoryGroup.external_group_id).where(
                                EnterpriseDirectoryGroup.identity_source_id == self._source.id,
                                EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                                EnterpriseDirectoryGroup.external_group_id.in_(group_id_list[index : index + 500]),
                            )
                        )
                    ).scalars()
                )
            user_member_ids = {
                _required_external_id(str(record.get("member_id") or ""), "directory member id")
                for record in records
                if record.get("member_type") == EnterpriseDirectoryMembershipType.USER.value
            }
            group_member_ids = {
                _required_external_id(str(record.get("member_id") or ""), "directory member id")
                for record in records
                if record.get("member_type") == EnterpriseDirectoryMembershipType.GROUP.value
            }
            known_user_member_ids = await self._known_ids(
                EnterpriseDirectoryUser, EnterpriseDirectoryUser.external_user_id, user_member_ids
            )
            known_group_member_ids = await self._known_ids(
                EnterpriseDirectoryGroup, EnterpriseDirectoryGroup.external_group_id, group_member_ids
            )
            desired: set[tuple[str, EnterpriseDirectoryMembershipType, str]] = set()
            for record in records:
                group_id = _required_external_id(
                    str(record.get("group_id") or ""),
                    "directory group id",
                )
                if group_id not in known_group_ids:
                    continue
                try:
                    member_type = EnterpriseDirectoryMembershipType(str(record.get("member_type") or ""))
                except ValueError as exc:
                    raise ValueError("directory membership type is invalid") from exc
                member_id = _required_external_id(
                    str(record.get("member_id") or ""),
                    "directory member id",
                )
                if (
                    member_type == EnterpriseDirectoryMembershipType.USER and member_id not in known_user_member_ids
                ) or (
                    member_type == EnterpriseDirectoryMembershipType.GROUP and member_id not in known_group_member_ids
                ):
                    continue
                desired.add((group_id, member_type, member_id))

            existing: dict[
                tuple[str, EnterpriseDirectoryMembershipType, str],
                EnterpriseDirectoryMembership,
            ] = {}
            ordered = sorted(
                desired,
                key=lambda item: (item[0], item[1].value, item[2]),
            )
            for index in range(0, len(ordered), 250):
                rows = (
                    await self._session.execute(
                        select(EnterpriseDirectoryMembership).where(
                            EnterpriseDirectoryMembership.identity_source_id == self._source.id,
                            tuple_(
                                EnterpriseDirectoryMembership.external_group_id,
                                EnterpriseDirectoryMembership.member_type,
                                EnterpriseDirectoryMembership.external_member_id,
                            ).in_(ordered[index : index + 250]),
                        )
                    )
                ).scalars()
                existing.update(
                    (
                        (
                            row.external_group_id,
                            row.member_type,
                            row.external_member_id,
                        ),
                        row,
                    )
                    for row in rows
                )
            current = utc_now()
            for key in desired:
                membership = existing.get(key)
                if membership is None:
                    self._session.add(
                        EnterpriseDirectoryMembership(
                            identity_source_id=self._source.id,
                            external_group_id=key[0],
                            member_type=key[1],
                            external_member_id=key[2],
                        )
                    )
                else:
                    membership.updated_at = current
            await self._commit_page()

    async def _known_ids(self, model: Any, column: Any, ids: set[str]) -> set[str]:
        if not ids:
            return set()
        known_ids: set[str] = set()
        ordered_ids = sorted(ids)
        for index in range(0, len(ordered_ids), 500):
            known_ids.update(
                (
                    await self._session.execute(
                        select(column).where(
                            model.identity_source_id == self._source.id,
                            model.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                            column.in_(ordered_ids[index : index + 500]),
                        )
                    )
                ).scalars()
            )
        return known_ids

    async def preserve_group_memberships(self, group_ids: Iterable[str]) -> None:
        if self._started_at is None:
            raise RuntimeError("identity source sync did not start")
        preserved_ids = sorted(set(group_ids))
        if not preserved_ids:
            return
        async with self._lock:
            for index in range(0, len(preserved_ids), 500):
                await self._session.execute(
                    update(EnterpriseDirectoryMembership)
                    .where(
                        EnterpriseDirectoryMembership.identity_source_id == self._source.id,
                        EnterpriseDirectoryMembership.external_group_id.in_(preserved_ids[index : index + 500]),
                    )
                    .values(updated_at=self._started_at)
                )
            await self._commit_page()

    async def record_sync_warning(
        self,
        code: str,
        details: Mapping[str, object],
    ) -> None:
        self._sync_warning = {"code": code, **details}

    async def membership_group_ids(self, after_group_id: str | None, limit: int) -> list[str]:
        async with self._lock:
            query = (
                select(EnterpriseDirectoryGroup.external_group_id)
                .where(
                    EnterpriseDirectoryGroup.identity_source_id == self._source.id,
                    EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                )
                .order_by(EnterpriseDirectoryGroup.external_group_id)
                .limit(limit)
            )
            if after_group_id is not None:
                query = query.where(EnterpriseDirectoryGroup.external_group_id > after_group_id)
            return list((await self._session.execute(query)).scalars())

    async def complete_sync(self) -> None:
        if self._started_at is None:
            raise RuntimeError("identity source sync did not start")
        async with self._lock:
            await self._session.execute(
                update(EnterpriseDirectoryUser)
                .where(
                    EnterpriseDirectoryUser.identity_source_id == self._source.id,
                    EnterpriseDirectoryUser.updated_at < self._started_at,
                )
                .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
                # Let the database compare timestamps: SQLite/MySQL return
                # naive values for already-loaded directory rows.
                .execution_options(synchronize_session="fetch")
            )
            await _disable_source_owned_users(self._session, self._source)
            await self._session.execute(
                update(EnterpriseDirectoryGroup)
                .where(
                    EnterpriseDirectoryGroup.identity_source_id == self._source.id,
                    EnterpriseDirectoryGroup.updated_at < self._started_at,
                )
                .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
                .execution_options(synchronize_session="fetch")
            )
            while True:
                stale_ids = list(
                    (
                        await self._session.execute(
                            select(EnterpriseDirectoryMembership.id)
                            .where(
                                EnterpriseDirectoryMembership.identity_source_id == self._source.id,
                                EnterpriseDirectoryMembership.updated_at < self._started_at,
                            )
                            .order_by(EnterpriseDirectoryMembership.id)
                            .limit(1_000)
                        )
                    ).scalars()
                )
                if not stale_ids:
                    break
                await self._session.execute(
                    delete(EnterpriseDirectoryMembership)
                    .where(EnterpriseDirectoryMembership.id.in_(stale_ids))
                    .execution_options(synchronize_session=False)
                )
                await self._commit_page()
            self._source.sync_warning_json = (
                json.dumps(self._sync_warning, sort_keys=True) if self._sync_warning is not None else None
            )
            await self._commit_page()


async def upsert_external_user(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    *,
    external_user_id: str,
    display_name: str,
    email: str | None,
    user: User | None = None,
    synchronize_status: bool = False,
) -> User:
    """Upsert the canonical provider identity used by both sync and OAuth/JIT."""
    external_user_id = _required_external_id(external_user_id, "external_user_id")
    name = _required_external_id(display_name or external_user_id, "display_name")
    provider_key = identity_provider_key(source)
    identity = (
        await session.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.identity_provider == provider_key,
                UserExternalIdentity.external_subject == external_user_id,
            )
        )
    ).scalar_one_or_none()
    if identity is not None:
        resolved = await session.get(User, identity.user_id)
        if resolved is None:
            raise ValueError("external identity references a missing PAS user")
        if user is not None and resolved.id != user.id:
            raise ValueError("external identity is already linked to another PAS user")
        user = resolved
    elif user is None:
        user = User(
            external_id=f"{provider_key}:{external_user_id}",
            display_name=name,
            email=email,
            auth_provider=AuthProvider.OIDC,
            role=UserRole.MEMBER,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        await session.flush()
    if identity is None:
        identity = UserExternalIdentity(
            user_id=user.id,
            identity_provider=provider_key,
            external_subject=external_user_id,
        )
        session.add(identity)

    if user.external_id == f"{provider_key}:{external_user_id}":
        user.display_name = name
        if email is not None:
            user.email = email
    if (
        synchronize_status
        and identity is not None
        and user.status != UserStatus.ACTIVE
        and user.external_id == f"{provider_key}:{external_user_id}"
        and user.auth_provider == AuthProvider.OIDC
        and not await session.scalar(
            select(
                exists().where(
                    UserExternalIdentity.user_id == user.id,
                    UserExternalIdentity.id != identity.id,
                )
            )
        )
    ):
        user.status = UserStatus.ACTIVE
    directory_user = (
        await session.execute(
            select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                EnterpriseDirectoryUser.external_user_id == external_user_id,
            )
        )
    ).scalar_one_or_none()
    if directory_user is None:
        directory_user = EnterpriseDirectoryUser(
            identity_source_id=source.id,
            external_user_id=external_user_id,
            display_name=name,
            email=email,
            status=EnterpriseDirectoryEntryStatus.ACTIVE,
        )
        session.add(directory_user)
    elif synchronize_status:
        directory_user.display_name = name
        if email is not None:
            directory_user.email = email
        directory_user.status = EnterpriseDirectoryEntryStatus.ACTIVE
        directory_user.updated_at = utc_now()
    await session.flush()
    return user


async def upsert_directory_group(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    *,
    external_group_id: str,
    display_name: str,
    principal_type: EnterpriseDirectoryPrincipalType = EnterpriseDirectoryPrincipalType.GROUP,
) -> EnterpriseDirectoryGroup:
    external_group_id = _required_external_id(external_group_id, "external_group_id")
    name = _required_external_id(display_name or external_group_id, "display_name")
    group = (
        await session.execute(
            select(EnterpriseDirectoryGroup).where(
                EnterpriseDirectoryGroup.identity_source_id == source.id,
                EnterpriseDirectoryGroup.external_group_id == external_group_id,
            )
        )
    ).scalar_one_or_none()
    if group is None:
        group = EnterpriseDirectoryGroup(
            identity_source_id=source.id,
            external_group_id=external_group_id,
            display_name=name,
            principal_type=principal_type,
            status=EnterpriseDirectoryEntryStatus.ACTIVE,
        )
        session.add(group)
    else:
        group.display_name = name
        group.principal_type = principal_type
        group.status = EnterpriseDirectoryEntryStatus.ACTIVE
        group.updated_at = utc_now()
    await session.flush()
    return group


async def upsert_directory_membership(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    *,
    external_group_id: str,
    member_type: EnterpriseDirectoryMembershipType,
    external_member_id: str,
) -> EnterpriseDirectoryMembership:
    external_group_id = _required_external_id(external_group_id, "external_group_id")
    external_member_id = _required_external_id(external_member_id, "external_member_id")
    membership = (
        await session.execute(
            select(EnterpriseDirectoryMembership).where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                EnterpriseDirectoryMembership.external_group_id == external_group_id,
                EnterpriseDirectoryMembership.member_type == member_type,
                EnterpriseDirectoryMembership.external_member_id == external_member_id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        membership = EnterpriseDirectoryMembership(
            identity_source_id=source.id,
            external_group_id=external_group_id,
            member_type=member_type,
            external_member_id=external_member_id,
        )
        session.add(membership)
        await session.flush()
    else:
        membership.updated_at = utc_now()
        await session.flush()
    return membership


async def replace_directory_snapshot(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    *,
    users: Iterable[Mapping[str, object]],
    groups: Iterable[Mapping[str, object]],
    memberships: Iterable[Mapping[str, object]],
    now: datetime | None = None,
) -> None:
    """Replace one source's directory facts in the caller's transaction.

    PAS users remain durable account records so a disabled source can be
    diagnosed or explicitly re-enabled. ACL construction reads the directory
    status and deletes removed memberships in the same transaction, which
    makes revocation effective at the next request.
    """
    current = now or datetime.now(UTC)
    user_ids: set[str] = set()
    for record in users:
        external_user_id = _required_external_id(str(record.get("id") or ""), "directory user id")
        await upsert_external_user(
            session,
            source,
            external_user_id=external_user_id,
            display_name=str(record.get("display_name") or external_user_id),
            email=str(record["email"]) if record.get("email") is not None else None,
            synchronize_status=True,
        )
        user_ids.add(external_user_id)
    group_ids: set[str] = set()
    for record in groups:
        external_group_id = _required_external_id(str(record.get("id") or ""), "directory group id")
        await upsert_directory_group(
            session,
            source,
            external_group_id=external_group_id,
            display_name=str(record.get("display_name") or external_group_id),
            principal_type=EnterpriseDirectoryPrincipalType(str(record.get("principal_type") or "group")),
        )
        group_ids.add(external_group_id)

    if user_ids:
        await session.execute(
            update(EnterpriseDirectoryUser)
            .where(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                EnterpriseDirectoryUser.external_user_id.not_in(user_ids),
            )
            .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
        )
    else:
        await session.execute(
            update(EnterpriseDirectoryUser)
            .where(EnterpriseDirectoryUser.identity_source_id == source.id)
            .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
        )
    await _disable_source_owned_users(session, source)
    if group_ids:
        await session.execute(
            update(EnterpriseDirectoryGroup)
            .where(
                EnterpriseDirectoryGroup.identity_source_id == source.id,
                EnterpriseDirectoryGroup.external_group_id.not_in(group_ids),
            )
            .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
        )
    else:
        await session.execute(
            update(EnterpriseDirectoryGroup)
            .where(EnterpriseDirectoryGroup.identity_source_id == source.id)
            .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
        )

    await session.execute(
        delete(EnterpriseDirectoryMembership).where(EnterpriseDirectoryMembership.identity_source_id == source.id)
    )
    for record in memberships:
        external_group_id = _required_external_id(str(record.get("group_id") or ""), "directory group id")
        if external_group_id not in group_ids:
            raise ValueError("directory membership references an unknown group")
        try:
            member_type = EnterpriseDirectoryMembershipType(str(record.get("member_type") or ""))
        except ValueError as exc:
            raise ValueError("directory membership type is invalid") from exc
        external_member_id = _required_external_id(str(record.get("member_id") or ""), "directory member id")
        await upsert_directory_membership(
            session,
            source,
            external_group_id=external_group_id,
            member_type=member_type,
            external_member_id=external_member_id,
        )
    source.last_synced_at = current
    source.last_error = None
    await session.flush()


async def _external_scope_items(scopes: Iterable[Any] | AsyncIterable[Any]) -> AsyncIterator[Any]:
    if isinstance(scopes, AsyncIterable):
        iterator = scopes.__aiter__()
        try:
            async for scope in iterator:
                yield scope
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()
    else:
        for scope in scopes:
            yield scope


async def import_external_knowledge_scopes(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    external_knowledge_scopes: Iterable[Any] | AsyncIterable[Any],
    *, record_identity_warning: bool = True,
) -> dict[str, Any]:
    """Import scope streams with per-scope isolation and generation fencing."""
    from server.core.external_knowledge_scope_sync import (
        ExternalKnowledgeScopeStaleSnapshotError,
        ExternalKnowledgeScopeSyncError,
        ImportedGrant,
        ImportedSnapshot,
        discard_external_knowledge_scope_snapshot,
        import_external_knowledge_scope_snapshot,
    )

    scope_errors: dict[str, int] = {}
    processed = 0
    async with aclosing(_external_scope_items(external_knowledge_scopes)) as scopes:
        async for external_scope in scopes:
            processed += 1
            imported_snapshot = ImportedSnapshot(
                snapshot_id=external_scope.snapshot_id,
                tenant_key=external_scope.tenant_key,
                source_scope_type=external_scope.source_scope_type,
                source_scope_id=external_scope.source_scope_id,
                source_scope_name=external_scope.source_scope_name,
                space_id=external_scope.space_id,
                kb_id=external_scope.kb_id,
                generation=external_scope.generation,
                fingerprint=external_scope.fingerprint,
                status=external_scope.status,
                source_updated_at=external_scope.source_updated_at,
                grants=tuple(
                    ImportedGrant(
                        subject_type=grant.subject_type,
                        subject_id=grant.subject_id,
                    )
                    for grant in external_scope.grants
                ),
            )
            try:
                async with session.begin_nested():
                    await import_external_knowledge_scope_snapshot(
                        session,
                        identity_source_id=source.id,
                        snapshot=imported_snapshot,
                    )
            except ExternalKnowledgeScopeStaleSnapshotError as exc:
                error_code = str(exc)
                scope_errors[error_code] = scope_errors.get(error_code, 0) + 1
                logger.warning(
                    "enterprise_identity.external_knowledge_scope_stale",
                    extra={
                        "identity_source_id": source.id,
                        "snapshot_id": external_scope.snapshot_id,
                        "error": error_code,
                    },
                )
            except ExternalKnowledgeScopeSyncError as exc:
                await discard_external_knowledge_scope_snapshot(
                    session,
                    identity_source_id=source.id,
                    snapshot_id=external_scope.snapshot_id,
                )
                error_code = str(exc)
                scope_errors[error_code] = scope_errors.get(error_code, 0) + 1
                logger.warning(
                    "enterprise_identity.external_knowledge_scope_skipped",
                    extra={
                        "identity_source_id": source.id,
                        "snapshot_id": external_scope.snapshot_id,
                        "error": error_code,
                    },
                )
    if scope_errors and record_identity_warning:
        warning: dict[str, Any] = {}
        if source.sync_warning_json:
            try:
                decoded = json.loads(source.sync_warning_json)
                if isinstance(decoded, dict):
                    warning.update(decoded)
            except ValueError:
                pass
        warning["external_knowledge_scopes"] = {
            "code": "SCOPES_PARTIAL",
            "errors": scope_errors,
            "skipped_count": sum(scope_errors.values()),
        }
        source.sync_warning_json = json.dumps(warning, sort_keys=True)

    return {"snapshots_processed": processed, "snapshots_skipped": sum(scope_errors.values()), "errors": scope_errors}


async def reconcile_feishu_groups(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    *,
    feishu_client_factory: Callable[..., Any] | None = None,
    acl_snapshot_client_factory: Callable[..., Any] | None = None,
) -> None:
    """Refresh Feishu contact groups without scanning users or departments."""
    from server.core.crypto import decrypt
    from server.enterprise_identity.feishu import FeishuDirectoryClient

    if source.provider != IdentitySourceProvider.FEISHU or not source.config_ciphertext:
        raise ValueError("Feishu identity source configuration is missing")
    config = json.loads(decrypt(source.config_ciphertext))
    app_id = config.get("app_id") if isinstance(config, dict) else None
    app_secret = config.get("app_secret") if isinstance(config, dict) else None
    if not isinstance(app_id, str) or not isinstance(app_secret, str):
        raise ValueError("Feishu identity source configuration is invalid")
    factory = feishu_client_factory or FeishuDirectoryClient
    fetched_at = datetime.now(UTC)
    async with factory(app_id=app_id, app_secret=app_secret) as client:
        snapshot = await client.fetch_group_snapshot()
    extra_groups, extra_memberships, external_knowledge_scopes = await directory_sync_adapter_for(
        source.provider
    ).extra_snapshot(
        source,
        config,
        acl_snapshot_client_factory=acl_snapshot_client_factory,
    )

    from server.enterprise_identity.sync import _feishu_event_lock

    async with _feishu_event_lock:
        await _apply_feishu_group_snapshot(
            session,
            source,
            snapshot,
            fetched_at=fetched_at,
            extra_groups=extra_groups,
            extra_memberships=extra_memberships,
        )
        await import_external_knowledge_scopes(session, source, external_knowledge_scopes)
        await session.commit()


async def _apply_feishu_group_snapshot(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    snapshot: Any,
    *,
    fetched_at: datetime,
    extra_groups: Iterable[Mapping[str, object]] = (),
    extra_memberships: Iterable[Mapping[str, object]] = (),
) -> None:
    owner = (
        await session.execute(
            select(
                EnterpriseIdentitySource.sync_worker_id,
                EnterpriseIdentitySource.sync_kind,
                EnterpriseIdentitySource.full_sync_requested_at,
            )
            .where(EnterpriseIdentitySource.id == source.id)
            .with_for_update()
        )
    ).one()
    if source.sync_worker_id is not None and owner[:2] != (source.sync_worker_id, "groups"):
        raise RuntimeError("Feishu group reconciliation lease was lost")
    if owner[2] is not None:
        raise RuntimeError("Full reconciliation requested during Feishu group reconciliation")
    changed_user_ids = set(
        (
            await session.execute(
                select(EnterpriseIdentitySourceEvent.subject_id).where(
                    EnterpriseIdentitySourceEvent.identity_source_id == source.id,
                    EnterpriseIdentitySourceEvent.status == EnterpriseIdentitySourceEventStatus.PROCESSED,
                    EnterpriseIdentitySourceEvent.event_type.like("contact.user.%"),
                    EnterpriseIdentitySourceEvent.updated_at >= fetched_at,
                )
            )
        ).scalars()
    )
    preserved_memberships = (
        list(
            (
                await session.execute(
                    select(
                        EnterpriseDirectoryMembership.external_group_id,
                        EnterpriseDirectoryMembership.external_member_id,
                    ).where(
                        EnterpriseDirectoryMembership.identity_source_id == source.id,
                        EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.USER,
                        EnterpriseDirectoryMembership.external_member_id.in_(changed_user_ids),
                    )
                )
            ).all()
        )
        if changed_user_ids
        else []
    )
    etl_groups = [
        group for group in extra_groups if group.get("principal_type") == EnterpriseDirectoryPrincipalType.GROUP.value
    ]
    group_ids = {str(group["id"]) for group in (*snapshot.groups, *etl_groups)}
    existing_group_ids = set(
        (
            await session.execute(
                select(EnterpriseDirectoryGroup.external_group_id).where(
                    EnterpriseDirectoryGroup.identity_source_id == source.id,
                    EnterpriseDirectoryGroup.principal_type == EnterpriseDirectoryPrincipalType.GROUP,
                )
            )
        ).scalars()
    )
    for group in (*etl_groups, *snapshot.groups):
        await upsert_directory_group(
            session,
            source,
            external_group_id=str(group["id"]),
            display_name=str(group["display_name"]),
            principal_type=EnterpriseDirectoryPrincipalType.GROUP,
        )
    removed_group_ids = existing_group_ids - group_ids
    if removed_group_ids:
        await session.execute(
            update(EnterpriseDirectoryGroup)
            .where(
                EnterpriseDirectoryGroup.identity_source_id == source.id,
                EnterpriseDirectoryGroup.external_group_id.in_(removed_group_ids),
            )
            .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
        )
    if existing_group_ids | group_ids:
        await session.execute(
            delete(EnterpriseDirectoryMembership).where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                EnterpriseDirectoryMembership.external_group_id.in_(existing_group_ids | group_ids),
            )
        )
    etl_memberships = [member for member in extra_memberships if str(member["group_id"]) in group_ids]
    user_ids = {str(member["member_id"]) for member in (*snapshot.memberships, *etl_memberships)} | changed_user_ids
    known_user_ids = (
        set(
            (
                await session.execute(
                    select(EnterpriseDirectoryUser.external_user_id).where(
                        EnterpriseDirectoryUser.identity_source_id == source.id,
                        EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                        EnterpriseDirectoryUser.external_user_id.in_(user_ids),
                    )
                )
            ).scalars()
        )
        if user_ids
        else set()
    )
    desired_memberships = {
        (str(member["group_id"]), str(member["member_id"]))
        for member in (*snapshot.memberships, *etl_memberships)
        if member["member_id"] in known_user_ids and member["member_id"] not in changed_user_ids
    }
    for group_id, user_id in preserved_memberships:
        if group_id in group_ids and user_id in known_user_ids:
            desired_memberships.add((group_id, user_id))
    for group_id, user_id in desired_memberships:
        session.add(
            EnterpriseDirectoryMembership(
                identity_source_id=source.id,
                external_group_id=group_id,
                member_type=EnterpriseDirectoryMembershipType.USER,
                external_member_id=user_id,
            )
        )
    source.last_group_synced_at = datetime.now(UTC)
    source.last_group_error = None
    source.group_next_retry_at = None
    await session.flush()


async def fetch_feishu_directory_event(
    source: EnterpriseIdentitySource,
    *,
    event_type: str,
    subject_id: str | None,
    client_factory: Callable[..., Any] | None = None,
    acl_snapshot_client_factory: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Fetch event facts without acquiring database locks."""
    if source.provider != IdentitySourceProvider.FEISHU:
        raise ValueError("Feishu directory event source is invalid")
    if event_type not in _FEISHU_DIRECTORY_UPDATE_EVENTS:
        return {}
    if not subject_id:
        raise ValueError("Feishu directory event subject is missing")
    from server.core.crypto import decrypt
    from server.enterprise_identity.feishu import FeishuDirectoryClient

    if not source.config_ciphertext:
        raise ValueError("identity source configuration is missing")
    config = json.loads(decrypt(source.config_ciphertext))
    app_id = config.get("app_id") if isinstance(config, dict) else None
    app_secret = config.get("app_secret") if isinstance(config, dict) else None
    if not isinstance(app_id, str) or not isinstance(app_secret, str):
        raise ValueError("identity source configuration is invalid")
    factory = client_factory or FeishuDirectoryClient
    async with factory(app_id=app_id, app_secret=app_secret) as client:
        if event_type.startswith("contact.user."):
            user = await client.fetch_user(subject_id)
            memberships = await client.fetch_user_memberships(subject_id)
            groups: list[dict[str, Any]] = []
            snapshot_config = acl_membership_snapshot_config(config)
            if snapshot_config is not None:
                from server.enterprise_identity.acl_snapshot import FeishuAclMembershipSnapshotClient

                acl_factory = acl_snapshot_client_factory or FeishuAclMembershipSnapshotClient
                acl_snapshot = await acl_factory(**snapshot_config).fetch_user_snapshot(
                    tenant_id=source.tenant_id, user_id=subject_id,
                )
                groups = acl_snapshot.groups
                memberships.extend(acl_snapshot.memberships)
            return {"user": user, "memberships": memberships, "groups": groups}
        department = await client.fetch_department(subject_id)
        users = await client.fetch_department_users(subject_id)
        return {"department": department, "users": users}


async def apply_feishu_directory_event(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    *,
    event_type: str,
    subject_id: str | None,
    client_factory: Callable[..., Any] | None = None,
    acl_snapshot_client_factory: Callable[..., Any] | None = None,
    data: dict[str, Any] | None = None,
) -> bool:
    """Apply one normalized Feishu event and return whether a full sync is needed."""
    if data is None:
        data = await fetch_feishu_directory_event(
            source, event_type=event_type, subject_id=subject_id,
            client_factory=client_factory, acl_snapshot_client_factory=acl_snapshot_client_factory,
        )
    return await apply_directory_event_facts(
        session, source, provider=IdentitySourceProvider.FEISHU,
        event_type=event_type, subject_id=subject_id, data=data,
    )


async def apply_directory_event_facts(session, source, *, provider, event_type, subject_id, data):
    """Apply authoritative provider facts, scoped to one source and subject."""
    if event_type == "contact.scope.updated_v3":
        return True
    if not subject_id:
        raise ValueError("Feishu directory event subject is missing")
    if source.provider != provider:
        raise ValueError("Directory event source is invalid")

    if event_type == "contact.user.deleted_v3":
        await session.execute(
            update(EnterpriseDirectoryUser)
            .where(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                EnterpriseDirectoryUser.external_user_id == subject_id,
            )
            .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
        )
        await _disable_source_owned_users(session, source, external_user_id=subject_id)
        await session.execute(
            delete(EnterpriseDirectoryMembership).where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.USER,
                EnterpriseDirectoryMembership.external_member_id == subject_id,
            )
        )
        await session.flush()
        return False

    if event_type == "contact.department.deleted_v3":
        await session.execute(
            update(EnterpriseDirectoryGroup)
            .where(
                EnterpriseDirectoryGroup.identity_source_id == source.id,
                EnterpriseDirectoryGroup.external_group_id == subject_id,
            )
            .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
        )
        await session.execute(
            delete(EnterpriseDirectoryMembership).where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                or_(
                    EnterpriseDirectoryMembership.external_group_id == subject_id,
                    (EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.GROUP)
                    & (EnterpriseDirectoryMembership.external_member_id == subject_id),
                ),
            )
        )
        await session.flush()
        return False

    if event_type not in _FEISHU_DIRECTORY_UPDATE_EVENTS:
        raise ValueError("Feishu directory event type is unsupported")

    if event_type.startswith("contact.user."):
        user = data["user"]
        memberships = data["memberships"]
        for group in data["groups"]:
            await upsert_directory_group(
                session, source,
                external_group_id=str(group["id"]),
                display_name=str(group["display_name"]),
                principal_type=EnterpriseDirectoryPrincipalType(str(group["principal_type"])),
            )
        await upsert_external_user(
            session,
            source,
            external_user_id=str(user.get("id") or ""),
            display_name=str(user.get("display_name") or subject_id),
            email=str(user["email"]) if user.get("email") is not None else None,
            synchronize_status=True,
        )
        await session.execute(
            delete(EnterpriseDirectoryMembership).where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.USER,
                EnterpriseDirectoryMembership.external_member_id == subject_id,
            )
        )
        for membership in memberships:
            await upsert_directory_membership(
                session,
                source,
                external_group_id=str(membership.get("group_id") or ""),
                member_type=EnterpriseDirectoryMembershipType.USER,
                external_member_id=subject_id,
            )
    else:
        department = data["department"]
        users = data["users"]
        await upsert_directory_group(
            session,
            source,
            external_group_id=str(department.get("id") or ""),
            display_name=str(department.get("display_name") or subject_id),
            principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
        )
        await session.execute(
            delete(EnterpriseDirectoryMembership).where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.GROUP,
                EnterpriseDirectoryMembership.external_member_id == subject_id,
            )
        )
        parent_id = department.get("parent_id")
        if isinstance(parent_id, str) and parent_id:
            await upsert_directory_membership(
                session,
                source,
                external_group_id=parent_id,
                member_type=EnterpriseDirectoryMembershipType.GROUP,
                external_member_id=subject_id,
            )
        await session.execute(
            delete(EnterpriseDirectoryMembership).where(
                EnterpriseDirectoryMembership.identity_source_id == source.id,
                EnterpriseDirectoryMembership.external_group_id == subject_id,
                EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.USER,
            )
        )
        for user in users:
            await upsert_external_user(
                session, source,
                external_user_id=str(user["id"]),
                display_name=str(user["display_name"]),
                email=str(user["email"]) if user["email"] is not None else None,
                synchronize_status=True,
            )
            await upsert_directory_membership(
                session, source,
                external_group_id=subject_id,
                member_type=EnterpriseDirectoryMembershipType.USER,
                external_member_id=str(user["id"]),
            )
    await session.flush()
    return False


async def sync_identity_source(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    *,
    feishu_client_factory: Callable[..., Any] | None = None,
    sharepoint_client_factory: Callable[..., Any] | None = None,
    acl_snapshot_client_factory: Callable[..., Any] | None = None,
    provider_client_factories: Mapping[IdentitySourceProvider, Callable[..., Any]] | None = None,
) -> Any | None:
    """Synchronize an enabled source without exposing provider credentials."""
    started_at = datetime.now(UTC)
    is_dingtalk = source.provider == IdentitySourceProvider.DINGTALK
    try:
        from server.core.crypto import decrypt

        if not source.config_ciphertext:
            raise ValueError("identity source configuration is missing")
        config = json.loads(decrypt(source.config_ciphertext))
        if not isinstance(config, dict):
            raise ValueError("identity source configuration is invalid")
        if source.tenant_id is None:
            raise ValueError("identity source tenant is missing")
        adapter = directory_sync_adapter_for(source.provider)
        client_factories = {
            IdentitySourceProvider.FEISHU: feishu_client_factory,
            IdentitySourceProvider.SHAREPOINT: sharepoint_client_factory,
        }
        if provider_client_factories is not None:
            client_factories.update(provider_client_factories)
        override_factory = client_factories.get(source.provider)
        client = adapter.make_client(source, config, client_factory=override_factory)
        checkpoint_client: Any | None = None
        directory_snapshot: Any | None = None
        if override_factory is None and source.provider != IdentitySourceProvider.DINGTALK:
            checkpoint_client = client
            async with checkpoint_client:
                await checkpoint_client.sync_to_sink(
                    _StreamingDirectorySink(session, source, sync_worker_id=source.sync_worker_id)
                )
        else:
            async with client:
                directory_snapshot = await client.fetch_snapshot()
        extra_groups, extra_memberships, external_knowledge_scopes = await adapter.extra_snapshot(
            source,
            config,
            acl_snapshot_client_factory=acl_snapshot_client_factory,
        )
        if source.provider == IdentitySourceProvider.DINGTALK:
            from server.enterprise_identity.dingtalk_auth import dingtalk_config_digest
            initial_digest = dingtalk_config_digest(source, config)
            await session.refresh(source, with_for_update=True)
            live_config = json.loads(decrypt(source.config_ciphertext))
            if source.status == EnterpriseIdentitySourceStatus.DISABLED or initial_digest != dingtalk_config_digest(source, live_config):
                raise ValueError("DINGTALK_SOURCE_CHANGED")
            pending_event = await session.scalar(select(EnterpriseIdentitySourceEvent.id).where(
                EnterpriseIdentitySourceEvent.identity_source_id == source.id,
                EnterpriseIdentitySourceEvent.created_at >= started_at,
                EnterpriseIdentitySourceEvent.status == EnterpriseIdentitySourceEventStatus.PENDING).limit(1))
            if pending_event:
                raise ValueError("DINGTALK_DIRECTORY_CHANGED_DURING_SYNC")
        if directory_snapshot is not None:
            groups, memberships = _merge_directory_snapshots(
                groups=directory_snapshot.groups,
                memberships=directory_snapshot.memberships,
                extra_groups=extra_groups,
                extra_memberships=extra_memberships,
            )
            await replace_directory_snapshot(
                session,
                source,
                users=directory_snapshot.users,
                groups=groups,
                memberships=memberships,
            )
            source.sync_warning_json = None
        elif extra_groups or extra_memberships:
            sink = _StreamingDirectorySink(
                session,
                source,
                sync_worker_id=source.sync_worker_id,
            )
            await sink.begin_sync(datetime.now(UTC).isoformat())
            await sink.upsert_groups(extra_groups)
            await sink.upsert_memberships(extra_memberships)
        if checkpoint_client is not None or source.provider == IdentitySourceProvider.DINGTALK:
            source.last_synced_at = datetime.now(UTC)
        adapter.after_full_sync(source)
        if source.provider == IdentitySourceProvider.DINGTALK:
            source.status = EnterpriseIdentitySourceStatus.ACTIVE
            source.permission_error = None
            source.permission_observed_at = datetime.now(UTC)
        if external_knowledge_scopes:
            imported = await import_external_knowledge_scopes(session, source, external_knowledge_scopes)
            if source.provider == IdentitySourceProvider.DINGTALK:
                if imported["snapshots_skipped"]:
                    raise ValueError("DINGTALK_PERMISSION_SNAPSHOT_REJECTED")
                source.permission_observed_at = datetime.now(UTC)
                source.permission_error = None
        if source.provider == IdentitySourceProvider.DINGTALK:
            await session.execute(update(EnterpriseIdentitySourceEvent).where(
                EnterpriseIdentitySourceEvent.identity_source_id == source.id,
                EnterpriseIdentitySourceEvent.created_at < started_at,
                EnterpriseIdentitySourceEvent.status != EnterpriseIdentitySourceEventStatus.PROCESSED).values(
                    status=EnterpriseIdentitySourceEventStatus.PROCESSED))
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_error = None
        await session.flush()
        return checkpoint_client
    except Exception as exc:
        source.status = EnterpriseIdentitySourceStatus.STALE
        source.last_error = type(exc).__name__
        from server.enterprise_identity.dingtalk import DingTalkIdentityError

        # A transport failure does not prove a directory change. Keep only the
        # existing bounded snapshot; never restore a marker invalidated by an event.
        transient = isinstance(exc, DingTalkIdentityError) and str(exc) == "TRANSIENT_UPSTREAM"
        if is_dingtalk and not transient:
            source.last_synced_at = None
            source.permission_error = "RECONCILIATION_FAILED"
        await session.flush()
        raise
