from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
import asyncio
import json
import os
from typing import Any, Callable, TypedDict, cast

from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_config
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
    IdentitySourceProvider,
    User,
    UserExternalIdentity,
    UserRole,
    UserStatus,
)
from server.models.base import utc_now


class AclMembershipSnapshotConfig(TypedDict):
    host: str
    port: int
    username: str
    password: str
    database: str


def _acl_membership_snapshot_config(
    config: Mapping[str, object],
) -> AclMembershipSnapshotConfig | None:
    snapshot = config.get("acl_membership_snapshot")
    if snapshot is None:
        return None
    if not isinstance(snapshot, Mapping):
        raise ValueError("ACL membership snapshot configuration is invalid")
    host = snapshot.get("host")
    port = snapshot.get("port")
    username = snapshot.get("username")
    password = snapshot.get("password")
    database = snapshot.get("database", "polar_rag_meta")
    if (
        not isinstance(host, str)
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not isinstance(username, str)
        or not isinstance(password, str)
        or not isinstance(database, str)
    ):
        raise ValueError("ACL membership snapshot configuration is invalid")
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "database": database,
    }


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


def _feishu_sync_concurrency(*, initial_sync: bool) -> int:
    settings = get_config().enterprise_identity_sync
    if initial_sync:
        return _identity_sync_concurrency(
            "PAS_FEISHU_INITIAL_SYNC_CONCURRENCY",
            default=settings.initial_concurrency,
        )
    return _identity_sync_concurrency(
        "PAS_FEISHU_SYNC_CONCURRENCY",
        default=settings.incremental_concurrency,
    )


def _sharepoint_sync_concurrency() -> int:
    return _identity_sync_concurrency("PAS_SHAREPOINT_SYNC_CONCURRENCY")


def _identity_sync_concurrency(
    environment_variable: str,
    *,
    default: int = 8,
) -> int:
    raw_value = os.environ.get(environment_variable, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{environment_variable} must be an integer") from exc
    if not 1 <= value <= 32:
        raise ValueError(f"{environment_variable} must be between 1 and 32")
    return value


def _required_external_id(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > 255:
        raise ValueError(f"{label} is too long")
    return normalized


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

    async def membership_user_ids(self, after_user_id: str | None, limit: int) -> list[str]:
        async with self._lock:
            query = (
                select(EnterpriseDirectoryUser.external_user_id)
                .where(
                    EnterpriseDirectoryUser.identity_source_id == self._source.id,
                    EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                )
                .order_by(EnterpriseDirectoryUser.external_user_id)
                .limit(limit)
            )
            if after_user_id is not None:
                query = query.where(EnterpriseDirectoryUser.external_user_id > after_user_id)
            return list((await self._session.execute(query)).scalars())

    async def preserve_user_memberships(self, user_ids: Iterable[str]) -> None:
        if self._started_at is None:
            raise RuntimeError("identity source sync did not start")
        preserved_ids = sorted(set(user_ids))
        if not preserved_ids:
            return
        async with self._lock:
            for index in range(0, len(preserved_ids), 500):
                await self._session.execute(
                    update(EnterpriseDirectoryMembership)
                    .where(
                        EnterpriseDirectoryMembership.identity_source_id == self._source.id,
                        EnterpriseDirectoryMembership.member_type == EnterpriseDirectoryMembershipType.USER,
                        EnterpriseDirectoryMembership.external_member_id.in_(preserved_ids[index : index + 500]),
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
            )
            await self._session.execute(
                update(EnterpriseDirectoryGroup)
                .where(
                    EnterpriseDirectoryGroup.identity_source_id == self._source.id,
                    EnterpriseDirectoryGroup.updated_at < self._started_at,
                )
                .values(status=EnterpriseDirectoryEntryStatus.DISABLED)
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
    else:
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


async def sync_identity_source(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    *,
    feishu_client_factory: Callable[..., Any] | None = None,
    sharepoint_client_factory: Callable[..., Any] | None = None,
    acl_snapshot_client_factory: Callable[..., Any] | None = None,
) -> Any | None:
    """Synchronize an enabled source without exposing provider credentials."""
    try:
        from server.core.crypto import decrypt

        if not source.config_ciphertext:
            raise ValueError("identity source configuration is missing")
        config = json.loads(decrypt(source.config_ciphertext))
        if not isinstance(config, dict):
            raise ValueError("identity source configuration is invalid")
        if source.tenant_id is None:
            raise ValueError("identity source tenant is missing")
        extra_groups: list[Mapping[str, object]] = []
        extra_memberships: list[Mapping[str, object]] = []
        checkpoint_client: Any | None = None
        directory_snapshot: Any | None = None
        if source.provider == IdentitySourceProvider.FEISHU:
            from server.enterprise_identity.feishu import FeishuDirectoryClient

            app_id = config.get("app_id")
            app_secret = config.get("app_secret")
            if not isinstance(app_id, str) or not isinstance(app_secret, str):
                raise ValueError("identity source configuration is invalid")
            if feishu_client_factory is None:
                checkpoint_client = FeishuDirectoryClient(
                    app_id=app_id,
                    app_secret=app_secret,
                    max_concurrency=_feishu_sync_concurrency(
                        initial_sync=source.last_synced_at is None,
                    ),
                    checkpoint_key=source.id,
                )
                async with checkpoint_client:
                    await checkpoint_client.sync_to_sink(
                        _StreamingDirectorySink(
                            session,
                            source,
                            sync_worker_id=source.sync_worker_id,
                        )
                    )
            else:
                async with feishu_client_factory(app_id=app_id, app_secret=app_secret) as client:
                    directory_snapshot = await client.fetch_snapshot()
            snapshot_config = _acl_membership_snapshot_config(config)
            if snapshot_config is not None:
                if acl_snapshot_client_factory is None:
                    from server.enterprise_identity.acl_snapshot import FeishuAclMembershipSnapshotClient

                    acl_snapshot_client_factory = FeishuAclMembershipSnapshotClient
                snapshot_client_factory: Callable[..., Any] = acl_snapshot_client_factory
                acl_snapshot = await snapshot_client_factory(**snapshot_config).fetch_snapshot(
                    tenant_id=source.tenant_id
                )
                extra_groups = acl_snapshot.groups
                extra_memberships = acl_snapshot.memberships
        elif source.provider == IdentitySourceProvider.SHAREPOINT:
            from server.enterprise_identity.sharepoint import SharePointDirectoryClient

            client_id = config.get("client_id")
            client_secret = config.get("client_secret")
            cloud = config.get("cloud", "global")
            if not isinstance(client_id, str) or not isinstance(client_secret, str) or cloud not in {"global", "china"}:
                raise ValueError("identity source configuration is invalid")
            if sharepoint_client_factory is None:
                checkpoint_client = SharePointDirectoryClient(
                    tenant_id=source.tenant_id,
                    client_id=client_id,
                    client_secret=client_secret,
                    cloud=cloud,
                    max_concurrency=_sharepoint_sync_concurrency(),
                    checkpoint_key=source.id,
                )
                async with checkpoint_client:
                    await checkpoint_client.sync_to_sink(
                        _StreamingDirectorySink(
                            session,
                            source,
                            sync_worker_id=source.sync_worker_id,
                        )
                    )
            else:
                async with sharepoint_client_factory(
                    tenant_id=source.tenant_id,
                    client_id=client_id,
                    client_secret=client_secret,
                    cloud=cloud,
                ) as client:
                    directory_snapshot = await client.fetch_snapshot()
        else:
            raise ValueError("identity source provider is not supported")
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
        if checkpoint_client is not None:
            source.last_synced_at = datetime.now(UTC)
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_error = None
        await session.flush()
        return checkpoint_client
    except Exception as exc:
        source.status = EnterpriseIdentitySourceStatus.STALE
        source.last_error = type(exc).__name__
        await session.flush()
        raise
