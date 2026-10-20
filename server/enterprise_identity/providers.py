"""Provider-specific inputs to the shared enterprise directory sync flow."""

from __future__ import annotations

import os
from collections.abc import AsyncIterable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Literal, Protocol, TypedDict

from server.config import EnterpriseIdentitySyncConfig, get_config
from server.models import (
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
)


@dataclass(frozen=True)
class DirectorySyncRequest:
    kind: Literal["full", "groups"]
    interval_seconds: float


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class AclMembershipSnapshotConfig(TypedDict):
    host: str
    port: int
    username: str
    password: str
    database: str
    dialect: str


def acl_membership_snapshot_config(
    config: Mapping[str, object],
) -> AclMembershipSnapshotConfig | None:
    """Validate the optional read-only ACL database configuration."""
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
    dialect = snapshot.get("dialect", "mysql")
    if (
        not isinstance(host, str)
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not isinstance(username, str)
        or not isinstance(password, str)
        or not isinstance(database, str)
        or dialect not in {"mysql", "postgresql"}
    ):
        raise ValueError("ACL membership snapshot configuration is invalid")
    return {
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "database": database,
        "dialect": dialect,
    }


def _identity_sync_concurrency(environment_variable: str, *, default: int = 8) -> int:
    raw_value = os.environ.get(environment_variable, str(default))
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{environment_variable} must be an integer") from exc
    if not 1 <= value <= 32:
        raise ValueError(f"{environment_variable} must be between 1 and 32")
    return value


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


class DirectorySyncAdapter(Protocol):
    """Supply a provider client and optional facts to the shared sync service."""

    def make_client(
        self,
        source: EnterpriseIdentitySource,
        config: Mapping[str, object],
        *,
        client_factory: Callable[..., Any] | None,
    ) -> Any: ...

    async def extra_snapshot(
        self,
        source: EnterpriseIdentitySource,
        config: Mapping[str, object],
        *,
        acl_snapshot_client_factory: Callable[..., Any] | None,
    ) -> tuple[
        list[Mapping[str, object]],
        list[Mapping[str, object]],
        Iterable[Any] | AsyncIterable[Any],
    ]: ...

    def after_full_sync(self, source: EnterpriseIdentitySource) -> None: ...

    def due_sync(
        self,
        source: EnterpriseIdentitySource,
        now: datetime,
        settings: EnterpriseIdentitySyncConfig,
        configured_interval: float,
    ) -> DirectorySyncRequest | None: ...

    def next_due_at(
        self,
        source: EnterpriseIdentitySource,
        now: datetime,
        settings: EnterpriseIdentitySyncConfig,
        configured_interval: float,
    ) -> list[datetime]: ...


class FeishuDirectorySyncAdapter:
    def due_sync(
        self,
        source: EnterpriseIdentitySource,
        now: datetime,
        settings: EnterpriseIdentitySyncConfig,
        configured_interval: float,
    ) -> DirectorySyncRequest | None:
        last_full = source.last_synced_at
        threshold = timedelta(seconds=settings.feishu_fallback_after_seconds)
        unavailable = source.event_unavailable_since
        if (
            last_full is None
            or source.full_sync_requested_at is not None
            or (unavailable is not None and _utc(unavailable) <= now - threshold and _utc(last_full) <= now - threshold)
        ):
            return DirectorySyncRequest("full", settings.feishu_fallback_after_seconds)
        last_group = source.last_group_synced_at
        group_retry = source.group_next_retry_at
        if (
            last_group is None
            or _utc(last_group) <= now - timedelta(seconds=settings.feishu_group_and_knowledge_scope_interval_seconds)
        ) and (group_retry is None or _utc(group_retry) <= now):
            return DirectorySyncRequest("groups", settings.feishu_group_and_knowledge_scope_interval_seconds)
        return None

    def next_due_at(
        self,
        source: EnterpriseIdentitySource,
        now: datetime,
        settings: EnterpriseIdentitySyncConfig,
        configured_interval: float,
    ) -> list[datetime]:
        if source.last_synced_at is None:
            return [now] if source.status == EnterpriseIdentitySourceStatus.ACTIVE else []
        last_full = _utc(source.last_synced_at)
        due = []
        if source.full_sync_requested_at is not None:
            due.append(now)
        group_base = _utc(source.last_group_synced_at) if source.last_group_synced_at else last_full
        group_due = group_base + timedelta(seconds=settings.feishu_group_and_knowledge_scope_interval_seconds)
        if source.group_next_retry_at is not None:
            group_due = max(group_due, _utc(source.group_next_retry_at))
        due.append(group_due)
        if source.event_unavailable_since is not None:
            due.append(
                max(last_full, _utc(source.event_unavailable_since))
                + timedelta(seconds=settings.feishu_fallback_after_seconds)
            )
        return due

    def make_client(
        self,
        source: EnterpriseIdentitySource,
        config: Mapping[str, object],
        *,
        client_factory: Callable[..., Any] | None,
    ) -> Any:
        from server.enterprise_identity.feishu import FeishuDirectoryClient

        app_id = config.get("app_id")
        app_secret = config.get("app_secret")
        if not isinstance(app_id, str) or not isinstance(app_secret, str):
            raise ValueError("identity source configuration is invalid")
        if client_factory is not None:
            return client_factory(app_id=app_id, app_secret=app_secret)
        return FeishuDirectoryClient(
            app_id=app_id,
            app_secret=app_secret,
            max_concurrency=_feishu_sync_concurrency(initial_sync=source.last_synced_at is None),
            checkpoint_key=source.id,
            last_completed_at=source.last_synced_at,
            sync_worker_id=source.sync_worker_id,
        )

    async def extra_snapshot(
        self,
        source: EnterpriseIdentitySource,
        config: Mapping[str, object],
        *,
        acl_snapshot_client_factory: Callable[..., Any] | None,
    ) -> tuple[
        list[Mapping[str, object]],
        list[Mapping[str, object]],
        Iterable[Any] | AsyncIterable[Any],
    ]:
        snapshot_config = acl_membership_snapshot_config(config)
        if snapshot_config is None:
            return [], [], ()
        if acl_snapshot_client_factory is None:
            from server.enterprise_identity.acl_snapshot import FeishuAclMembershipSnapshotClient

            acl_snapshot_client_factory = FeishuAclMembershipSnapshotClient
        snapshot = await acl_snapshot_client_factory(**snapshot_config).fetch_snapshot(tenant_id=source.tenant_id)
        return snapshot.groups, snapshot.memberships, getattr(snapshot, "knowledge_scopes", ())

    def after_full_sync(self, source: EnterpriseIdentitySource) -> None:
        source.last_group_synced_at = source.last_synced_at
        source.last_group_error = None
        source.group_next_retry_at = None


class SharePointDirectorySyncAdapter:
    def due_sync(
        self,
        source: EnterpriseIdentitySource,
        now: datetime,
        settings: EnterpriseIdentitySyncConfig,
        configured_interval: float,
    ) -> DirectorySyncRequest | None:
        if (
            source.last_synced_at is None
            or source.full_sync_requested_at is not None
            or source.status
            in (
                EnterpriseIdentitySourceStatus.PENDING_BINDING,
                EnterpriseIdentitySourceStatus.STALE,
            )
            or _utc(source.last_synced_at) <= now - timedelta(seconds=configured_interval)
        ):
            return DirectorySyncRequest("full", configured_interval)
        return None

    def next_due_at(
        self,
        source: EnterpriseIdentitySource,
        now: datetime,
        settings: EnterpriseIdentitySyncConfig,
        configured_interval: float,
    ) -> list[datetime]:
        if source.last_synced_at is None:
            return [now] if source.status == EnterpriseIdentitySourceStatus.ACTIVE else []
        if source.full_sync_requested_at is not None:
            return [now]
        return [_utc(source.last_synced_at) + timedelta(seconds=configured_interval)]

    def make_client(
        self,
        source: EnterpriseIdentitySource,
        config: Mapping[str, object],
        *,
        client_factory: Callable[..., Any] | None,
    ) -> Any:
        from server.enterprise_identity.sharepoint import SharePointDirectoryClient

        client_id = config.get("client_id")
        client_secret = config.get("client_secret")
        cloud = config.get("cloud", "global")
        if not isinstance(client_id, str) or not isinstance(client_secret, str) or cloud not in {"global", "china"}:
            raise ValueError("identity source configuration is invalid")
        factory = client_factory or SharePointDirectoryClient
        kwargs: dict[str, Any] = {
            "tenant_id": source.tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
            "cloud": cloud,
        }
        if client_factory is None:
            kwargs.update(
                max_concurrency=_sharepoint_sync_concurrency(),
                checkpoint_key=source.id,
                last_completed_at=source.last_synced_at,
                sync_worker_id=source.sync_worker_id,
            )
        return factory(**kwargs)

    async def extra_snapshot(
        self,
        source: EnterpriseIdentitySource,
        config: Mapping[str, object],
        *,
        acl_snapshot_client_factory: Callable[..., Any] | None,
    ) -> tuple[
        list[Mapping[str, object]],
        list[Mapping[str, object]],
        Iterable[Any] | AsyncIterable[Any],
    ]:
        return [], [], ()

    def after_full_sync(self, source: EnterpriseIdentitySource) -> None:
        return None


class DingTalkDirectorySyncAdapter(SharePointDirectorySyncAdapter):
    """Periodic atomic directory replacement plus native ETL scope import."""
    # A periodic full reconciliation bounds missed Stream events below the
    # 900-second permission snapshot TTL. Shorter administrator intervals apply;
    # longer intervals are capped at 300 seconds for this provider.
    def due_sync(self, source, now, settings, configured_interval):
        return super().due_sync(source, now, settings, min(configured_interval, 300))

    def next_due_at(self, source, now, settings, configured_interval):
        return super().next_due_at(source, now, settings, min(configured_interval, 300))

    def make_client(self, source, config, *, client_factory):
        from server.enterprise_identity.dingtalk import DingTalkDirectoryClient
        from server.enterprise_identity.dingtalk_auth import verified_dingtalk_config
        verified_dingtalk_config(source)
        factory = client_factory or DingTalkDirectoryClient
        return factory(tenant_id=source.tenant_id, client_id=config.get("client_id"),
                       client_secret=config.get("client_secret"))

    async def extra_snapshot(self, source, config, *, acl_snapshot_client_factory):
        from server.enterprise_identity.acl_snapshot import DingTalkAclMembershipSnapshotClient
        snapshot_config = acl_membership_snapshot_config(config)
        if snapshot_config is None:
            return [], [], ()
        factory = acl_snapshot_client_factory or DingTalkAclMembershipSnapshotClient
        reader = factory(**snapshot_config)
        # Do not import unsupported TAG/CONVERSATION groups under ambiguous raw IDs.
        return [], [], reader.iter_knowledge_scopes(tenant_key=source.tenant_id)


DIRECTORY_SYNC_ADAPTERS: dict[IdentitySourceProvider, DirectorySyncAdapter] = {
    IdentitySourceProvider.FEISHU: FeishuDirectorySyncAdapter(),
    IdentitySourceProvider.SHAREPOINT: SharePointDirectorySyncAdapter(),
    IdentitySourceProvider.DINGTALK: DingTalkDirectorySyncAdapter(),
}


def directory_sync_adapter_for(provider: IdentitySourceProvider) -> DirectorySyncAdapter:
    try:
        return DIRECTORY_SYNC_ADAPTERS[provider]
    except KeyError as exc:
        raise ValueError("identity source provider is not supported") from exc
