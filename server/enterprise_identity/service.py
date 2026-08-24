from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
import json
from typing import Any, Callable, TypedDict

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

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
        group_id = _required_external_id(
            str(membership.get("group_id") or ""), "directory group id"
        )
        member_type = _required_external_id(
            str(membership.get("member_type") or ""), "directory member type"
        )
        member_id = _required_external_id(
            str(membership.get("member_id") or ""), "directory member id"
        )
        membership_records[(group_id, member_type, member_id)] = membership
    return (
        [groups_by_id[group_id] for group_id in sorted(groups_by_id)],
        [membership_records[key] for key in sorted(membership_records)],
    )


def identity_provider_key(source: EnterpriseIdentitySource) -> str:
    return f"{source.provider.value}:{source.tenant_id}"


def _required_external_id(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    if len(normalized) > 255:
        raise ValueError(f"{label} is too long")
    return normalized


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
        directory_user.email = email
        directory_user.status = EnterpriseDirectoryEntryStatus.ACTIVE
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
            principal_type=EnterpriseDirectoryPrincipalType(
                str(record.get("principal_type") or "group")
            ),
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
        delete(EnterpriseDirectoryMembership).where(
            EnterpriseDirectoryMembership.identity_source_id == source.id
        )
    )
    for record in memberships:
        external_group_id = _required_external_id(str(record.get("group_id") or ""), "directory group id")
        if external_group_id not in group_ids:
            raise ValueError("directory membership references an unknown group")
        try:
            member_type = EnterpriseDirectoryMembershipType(str(record.get("member_type") or ""))
        except ValueError as exc:
            raise ValueError("directory membership type is invalid") from exc
        external_member_id = _required_external_id(
            str(record.get("member_id") or ""), "directory member id"
        )
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
) -> None:
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
        if source.provider == IdentitySourceProvider.FEISHU:
            from server.enterprise_identity.feishu import FeishuDirectoryClient

            app_id = config.get("app_id")
            app_secret = config.get("app_secret")
            if not isinstance(app_id, str) or not isinstance(app_secret, str):
                raise ValueError("identity source configuration is invalid")
            client_factory = feishu_client_factory or FeishuDirectoryClient
            async with client_factory(app_id=app_id, app_secret=app_secret) as client:
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
            if (
                not isinstance(client_id, str)
                or not isinstance(client_secret, str)
                or cloud not in {"global", "china"}
            ):
                raise ValueError("identity source configuration is invalid")
            client_factory = sharepoint_client_factory or SharePointDirectoryClient
            async with client_factory(
                tenant_id=source.tenant_id,
                client_id=client_id,
                client_secret=client_secret,
                cloud=cloud,
            ) as client:
                directory_snapshot = await client.fetch_snapshot()
        else:
            raise ValueError("identity source provider is not supported")
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
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        source.last_error = None
        await session.flush()
    except Exception as exc:
        source.status = EnterpriseIdentitySourceStatus.STALE
        source.last_error = type(exc).__name__
        await session.flush()
        raise
