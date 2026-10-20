from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
import logging
from typing import Any
import uuid

import httpx
from sqlalchemy import or_, select, update

from server.config import get_config
from server.enterprise_identity.providers import DIRECTORY_SYNC_ADAPTERS, directory_sync_adapter_for
from server.enterprise_identity.service import reconcile_feishu_groups, sync_identity_source
from server.models import (
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    EnterpriseIdentitySourceEvent,
    EnterpriseIdentitySourceEventStatus,
    FeishuEventSubscriptionStatus,
    IdentitySourceProvider,
)


logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 503})
_RETRY_BACKOFF_INITIAL_SECONDS = 60.0
_RETRY_BACKOFF_MAX_SECONDS = 1800.0
_SYNC_LEASE_SECONDS = 120.0
_SYNC_LEASE_RENEW_SECONDS = 30.0
_inflight_sync_tasks: dict[str, asyncio.Task[None]] = {}
_directory_event_lock = asyncio.Lock()
# Compatibility alias; both providers serialize against full-sync draining.
_feishu_event_lock = _directory_event_lock


def _retry_delay_seconds(
    exc: Exception,
    retry_count: int,
    *,
    fallback_seconds: float,
) -> float:
    if not isinstance(exc, httpx.HTTPStatusError):
        return fallback_seconds
    response = exc.response
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return min(max(float(retry_after), 1.0), _RETRY_BACKOFF_MAX_SECONDS)
            except ValueError:
                pass
    if response.status_code in _RETRYABLE_STATUS_CODES:
        return float(min(
            _RETRY_BACKOFF_INITIAL_SECONDS * (2 ** max(retry_count - 1, 0)),
            _RETRY_BACKOFF_MAX_SECONDS,
        ))
    return fallback_seconds


async def _renew_identity_source_sync_lease(
    session_factory: Any,
    source_id: str,
    worker_id: str,
) -> None:
    while True:
        await asyncio.sleep(_SYNC_LEASE_RENEW_SECONDS)
        now = datetime.now(UTC)
        async with session_factory() as session:
            renewed = await session.execute(
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.id == source_id,
                    EnterpriseIdentitySource.sync_worker_id == worker_id,
                )
                .values(
                    sync_lease_until=now + timedelta(seconds=_SYNC_LEASE_SECONDS)
                )
                .execution_options(synchronize_session=False)
            )
            if renewed.rowcount != 1:
                await session.rollback()
                return
            await session.commit()


async def _run_identity_source_sync_in_background(
    session_factory: Any,
    source_id: str,
    *,
    feishu_client_factory: Any = None,
    sharepoint_client_factory: Any = None,
    acl_snapshot_client_factory: Any = None,
    provider_client_factories: Mapping[IdentitySourceProvider, Any] | None = None,
    sync_callable: Any = sync_identity_source,
    worker_id: str,
    interval_seconds: float,
    sync_kind: str = "full",
    discard_pre_snapshot_events: bool = False,
    event_cutoff: datetime | None = None,
) -> None:
    async with session_factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        if (
            source is None
            or source.status == EnterpriseIdentitySourceStatus.DISABLED
            or source.sync_worker_id != worker_id
        ):
            return
        try:
            client_factories: dict[str, Any] = {}
            if feishu_client_factory is not None:
                client_factories["feishu_client_factory"] = feishu_client_factory
            if sharepoint_client_factory is not None:
                client_factories["sharepoint_client_factory"] = sharepoint_client_factory
            if acl_snapshot_client_factory is not None:
                client_factories["acl_snapshot_client_factory"] = acl_snapshot_client_factory
            if provider_client_factories is not None:
                client_factories["provider_client_factories"] = provider_client_factories
            checkpoint_client = await sync_callable(
                session,
                source,
                **client_factories,
            )
        except Exception as exc:
            retry_count = source.sync_retry_count + 1 if sync_kind == "full" else 1
            retry_delay = _retry_delay_seconds(
                exc,
                retry_count,
                fallback_seconds=interval_seconds,
            )
            await session.rollback()
            failed = await session.execute(
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.id == source_id,
                    EnterpriseIdentitySource.sync_worker_id == worker_id,
                )
                .values(
                    **({"status": EnterpriseIdentitySourceStatus.STALE, "last_error": type(exc).__name__}
                       if sync_kind == "full" else {"last_group_error": type(exc).__name__}),
                    **({"sync_retry_count": retry_count,
                        "sync_next_retry_at": datetime.now(UTC) + timedelta(seconds=retry_delay),
                        "full_sync_requested_at": datetime.now(UTC)}
                       if sync_kind == "full" else {
                           "group_next_retry_at": datetime.now(UTC) + timedelta(seconds=retry_delay)}),
                    sync_worker_id=None,
                    sync_kind=None,
                    sync_lease_until=None,
                )
                .execution_options(synchronize_session=False)
            )
            if failed.rowcount != 1:
                await session.rollback()
                return
            await session.commit()
            logger.warning(
                "enterprise_identity.source.background_sync_failed",
                extra={
                    "identity_source_id": source_id,
                    "error_type": type(exc).__name__,
                    "retry_after_seconds": retry_delay,
                },
            )
            return
        completed = await session.execute(
            update(EnterpriseIdentitySource)
            .where(
                EnterpriseIdentitySource.id == source_id,
                EnterpriseIdentitySource.sync_worker_id == worker_id,
            )
            .values(
                **({"last_error": None} if sync_kind == "full" else {"last_group_error": None}),
                **({"sync_retry_count": 0, "sync_next_retry_at": None}
                   if sync_kind == "full" else {"group_next_retry_at": None}),
                sync_worker_id=None,
                sync_kind=None,
                sync_lease_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        if completed.rowcount != 1:
            await session.rollback()
            return
        if discard_pre_snapshot_events and event_cutoff is not None:
            await session.execute(
                update(EnterpriseIdentitySourceEvent)
                .where(
                    EnterpriseIdentitySourceEvent.identity_source_id == source_id,
                    EnterpriseIdentitySourceEvent.status != EnterpriseIdentitySourceEventStatus.PROCESSED,
                    EnterpriseIdentitySourceEvent.created_at <= event_cutoff,
                )
                .values(
                    status=EnterpriseIdentitySourceEventStatus.PROCESSED,
                    next_retry_at=None,
                )
            )
        await session.commit()
        if checkpoint_client is not None:
            await checkpoint_client.clear_checkpoint()


async def _sync_identity_source_in_background(
    session_factory: Any,
    source_id: str,
    *,
    feishu_client_factory: Any = None,
    sharepoint_client_factory: Any = None,
    acl_snapshot_client_factory: Any = None,
    provider_client_factories: Mapping[IdentitySourceProvider, Any] | None = None,
    sync_callable: Any = sync_identity_source,
    worker_id: str,
    interval_seconds: float,
    event_cutoff: datetime,
    sync_kind: str = "full",
) -> None:
    renewer = asyncio.create_task(
        _renew_identity_source_sync_lease(session_factory, source_id, worker_id)
    )
    try:
        async with session_factory() as session:
            source_state = await session.execute(
                select(
                    EnterpriseIdentitySource.provider,
                    EnterpriseIdentitySource.event_listener_status,
                ).where(
                    EnterpriseIdentitySource.id == source_id
                )
            )
            provider, listener_status = source_state.one()
        recovering_subscription = (
            provider == IdentitySourceProvider.FEISHU
            and listener_status == FeishuEventSubscriptionStatus.ERROR
        )

        async def run_full_sync() -> None:
            if provider == IdentitySourceProvider.FEISHU and sync_kind == "full" and (
                recovering_subscription
                or (
                    sync_callable is sync_identity_source
                    and feishu_client_factory is None
                    and not provider_client_factories
                )
            ):
                from server.enterprise_identity.feishu_events import reconnect_feishu_listener_before_full

                await reconnect_feishu_listener_before_full(session_factory, source_id)
            await _run_identity_source_sync_in_background(
                session_factory,
                source_id,
                feishu_client_factory=feishu_client_factory,
                sharepoint_client_factory=sharepoint_client_factory,
                acl_snapshot_client_factory=acl_snapshot_client_factory,
                provider_client_factories=provider_client_factories,
                sync_callable=sync_callable,
                worker_id=worker_id,
                interval_seconds=interval_seconds,
                sync_kind=sync_kind,
                discard_pre_snapshot_events=sync_kind == "full",
                event_cutoff=event_cutoff,
            )

        if provider == IdentitySourceProvider.FEISHU:
            if recovering_subscription and sync_kind == "full":
                await run_full_sync()
                return
            from server.enterprise_identity.feishu_events import process_feishu_directory_events_once

            while True:
                async with _feishu_event_lock:
                    async with session_factory() as session:
                        pending = await session.scalar(
                            select(EnterpriseIdentitySourceEvent.id).where(
                                EnterpriseIdentitySourceEvent.identity_source_id == source_id,
                                EnterpriseIdentitySourceEvent.status == EnterpriseIdentitySourceEventStatus.PENDING,
                                EnterpriseIdentitySourceEvent.created_at <= event_cutoff,
                            ).limit(1)
                        )
                    if pending is None:
                        break
                    await process_feishu_directory_events_once(
                        session_factory,
                        source_ids={source_id},
                        draining_worker_id=worker_id,
                        event_cutoff=event_cutoff,
                    )
                await asyncio.sleep(1)
            if sync_kind in {"full", "groups"}:
                await run_full_sync()
        else:
            await run_full_sync()
    except Exception as exc:
        async with session_factory() as session:
            await session.execute(
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.id == source_id,
                    EnterpriseIdentitySource.sync_worker_id == worker_id,
                )
                .values(
                    **({"status": EnterpriseIdentitySourceStatus.STALE, "last_error": type(exc).__name__}
                       if sync_kind == "full" else {"last_group_error": type(exc).__name__}),
                    sync_worker_id=None,
                    sync_kind=None,
                    sync_lease_until=None,
                    **({"sync_next_retry_at": datetime.now(UTC) + timedelta(seconds=interval_seconds)}
                       if sync_kind == "full" else {
                           "group_next_retry_at": datetime.now(UTC) + timedelta(seconds=interval_seconds)}),
                    **({"full_sync_requested_at": datetime.now(UTC)} if sync_kind == "full" else {}),
                )
            )
            await session.commit()
        logger.warning(
            "enterprise_identity.source.event_drain_failed",
            extra={"identity_source_id": source_id, "error_type": type(exc).__name__},
        )
    finally:
        renewer.cancel()
        await asyncio.gather(renewer, return_exceptions=True)


def sync_task(source_id: str) -> asyncio.Task[None] | None:
    task = _inflight_sync_tasks.get(source_id)
    return task if task is not None and not task.done() else None


async def schedule_identity_source_sync(
    session_factory: Any,
    source_id: str,
    *,
    background_tasks: set[asyncio.Task[None]] | None = None,
    feishu_client_factory: Any = None,
    sharepoint_client_factory: Any = None,
    acl_snapshot_client_factory: Any = None,
    provider_client_factories: Mapping[IdentitySourceProvider, Any] | None = None,
    sync_callable: Any = sync_identity_source,
    interval_seconds: float | None = None,
    sync_kind: str = "full",
) -> bool:
    if sync_task(source_id) is not None:
        return False
    effective_interval = (
        interval_seconds
        if interval_seconds is not None
        else get_config().enterprise_identity_sync.interval_seconds
    )
    worker_id = str(uuid.uuid4())
    now = datetime.now(UTC)
    async with session_factory() as session:
        claimed = await session.execute(
            update(EnterpriseIdentitySource)
            .where(
                EnterpriseIdentitySource.id == source_id,
                EnterpriseIdentitySource.status
                != EnterpriseIdentitySourceStatus.DISABLED,
                or_(
                    EnterpriseIdentitySource.sync_worker_id.is_(None),
                    EnterpriseIdentitySource.sync_lease_until.is_(None),
                    EnterpriseIdentitySource.sync_lease_until <= now,
                ),
            )
            .values(
                sync_worker_id=worker_id,
                sync_kind=sync_kind,
                sync_lease_until=now + timedelta(seconds=_SYNC_LEASE_SECONDS),
                **({"full_sync_requested_at": None} if sync_kind == "full" else {}),
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            return False
        await session.commit()
    task = asyncio.create_task(
        _sync_identity_source_in_background(
            session_factory,
            source_id,
            feishu_client_factory=feishu_client_factory,
            sharepoint_client_factory=sharepoint_client_factory,
            acl_snapshot_client_factory=acl_snapshot_client_factory,
            provider_client_factories=provider_client_factories,
            sync_callable=sync_callable,
            worker_id=worker_id,
            interval_seconds=effective_interval,
            event_cutoff=now,
            sync_kind=sync_kind,
        )
    )
    _inflight_sync_tasks[source_id] = task
    if background_tasks is not None:
        background_tasks.add(task)

    def _finished(completed: asyncio.Task[None]) -> None:
        if _inflight_sync_tasks.get(source_id) is completed:
            _inflight_sync_tasks.pop(source_id, None)
        if background_tasks is not None:
            background_tasks.discard(completed)
        if sync_kind == "groups":
            async def start_requested_full_sync() -> None:
                async with session_factory() as session:
                    requested = await session.scalar(
                        select(EnterpriseIdentitySource.full_sync_requested_at).where(
                            EnterpriseIdentitySource.id == source_id,
                        )
                    )
                if requested is not None:
                    await schedule_identity_source_sync(session_factory, source_id)

            follow_up = asyncio.create_task(start_requested_full_sync())
            if background_tasks is not None:
                background_tasks.add(follow_up)
            def _follow_up_finished(completed_follow_up: asyncio.Task[None]) -> None:
                if background_tasks is not None:
                    background_tasks.discard(completed_follow_up)
                if not completed_follow_up.cancelled() and completed_follow_up.exception() is not None:
                    logger.error(
                        "enterprise_identity.source.queued_full_sync_failed",
                        extra={"identity_source_id": source_id,
                               "error_type": type(completed_follow_up.exception()).__name__},
                    )

            follow_up.add_done_callback(_follow_up_finished)

    task.add_done_callback(_finished)
    return True


async def repair_excessive_group_retry_delays(session_factory: Any, now: datetime) -> int:
    """Bound legacy group retries without altering directory lifecycle or leases."""
    interval = get_config().enterprise_identity_sync.feishu_group_and_knowledge_scope_interval_seconds
    maximum = max(interval, _RETRY_BACKOFF_MAX_SECONDS)
    async with session_factory() as session:
        result = await session.execute(update(EnterpriseIdentitySource).where(
            EnterpriseIdentitySource.provider == IdentitySourceProvider.FEISHU,
            EnterpriseIdentitySource.status.in_((EnterpriseIdentitySourceStatus.ACTIVE, EnterpriseIdentitySourceStatus.STALE)),
            EnterpriseIdentitySource.last_group_error.is_not(None),
            EnterpriseIdentitySource.group_next_retry_at > now + timedelta(seconds=maximum),
            or_(EnterpriseIdentitySource.sync_lease_until.is_(None), EnterpriseIdentitySource.sync_lease_until <= now),
        ).values(group_next_retry_at=now + timedelta(seconds=interval)))
        await session.commit()
        return result.rowcount


async def sync_configured_identity_sources_once(
    session_factory: Any,
    *,
    feishu_client_factory: Any = None,
    sharepoint_client_factory: Any = None,
    acl_snapshot_client_factory: Any = None,
    provider_client_factories: Mapping[IdentitySourceProvider, Any] | None = None,
    interval_seconds: float | None = None,
    now: datetime | None = None,
) -> bool:
    """Run each due source once without overlapping an existing source run."""
    effective_interval = (
        interval_seconds
        if interval_seconds is not None
        else get_config().enterprise_identity_sync.interval_seconds
    )
    settings = get_config().enterprise_identity_sync
    current = now or datetime.now(UTC)
    await repair_excessive_group_retry_delays(session_factory, current)
    async with session_factory() as session:
        source_rows = list((await session.execute(
            select(EnterpriseIdentitySource).where(
                EnterpriseIdentitySource.config_ciphertext.is_not(None),
                EnterpriseIdentitySource.tenant_id.is_not(None),
                EnterpriseIdentitySource.provider.in_(tuple(DIRECTORY_SYNC_ADAPTERS)),
                EnterpriseIdentitySource.status.in_((
                    EnterpriseIdentitySourceStatus.PENDING_BINDING,
                    EnterpriseIdentitySourceStatus.STALE,
                    EnterpriseIdentitySourceStatus.ACTIVE,
                )),
                or_(
                    EnterpriseIdentitySource.sync_next_retry_at.is_(None),
                    EnterpriseIdentitySource.sync_next_retry_at <= current,
                ),
            ).order_by(EnterpriseIdentitySource.id)
        )).scalars())

    tasks: list[asyncio.Task[None]] = []
    due_source_ids: list[str] = []
    for source in source_rows:
        request = directory_sync_adapter_for(source.provider).due_sync(
            source, current, settings, effective_interval
        )
        if request is None:
            continue
        due_source_ids.append(source.id)
        started = await schedule_identity_source_sync(
            session_factory,
            source.id,
            feishu_client_factory=feishu_client_factory,
            sharepoint_client_factory=sharepoint_client_factory,
            acl_snapshot_client_factory=acl_snapshot_client_factory,
            provider_client_factories=provider_client_factories,
            interval_seconds=request.interval_seconds,
            sync_callable=sync_identity_source if request.kind == "full" else reconcile_feishu_groups,
            sync_kind=request.kind,
        )
        if not started and request.kind == "full":
            async with session_factory() as session:
                await session.execute(
                    update(EnterpriseIdentitySource)
                    .where(
                        EnterpriseIdentitySource.id == source.id,
                        EnterpriseIdentitySource.sync_kind == "groups",
                        EnterpriseIdentitySource.full_sync_requested_at.is_(None),
                    )
                    .values(full_sync_requested_at=current)
                )
                await session.commit()
        task = sync_task(source.id)
        if started and task is not None:
            tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks)

    if not due_source_ids:
        return False
    async with session_factory() as session:
        return bool(
            await session.scalar(
                select(EnterpriseIdentitySource.id)
                .where(
                    EnterpriseIdentitySource.id.in_(due_source_ids),
                    EnterpriseIdentitySource.status
                    == EnterpriseIdentitySourceStatus.STALE,
                    or_(
                        EnterpriseIdentitySource.sync_next_retry_at.is_(None),
                        EnterpriseIdentitySource.sync_next_retry_at <= datetime.now(UTC),
                    ),
                )
                .limit(1)
            )
        )


async def _next_identity_source_sync_delay(
    session_factory: Any,
    configured_interval: float,
    *,
    selected_at: datetime | None = None,
) -> float:
    now = datetime.now(UTC)
    settings = get_config().enterprise_identity_sync
    retry_lower_bound = selected_at or now
    async with session_factory() as session:
        next_retry_at: datetime | None = await session.scalar(
            select(EnterpriseIdentitySource.sync_next_retry_at)
            .where(
                EnterpriseIdentitySource.config_ciphertext.is_not(None),
                EnterpriseIdentitySource.tenant_id.is_not(None),
                EnterpriseIdentitySource.provider.in_(tuple(DIRECTORY_SYNC_ADAPTERS)),
                EnterpriseIdentitySource.status.in_(
                    (
                        EnterpriseIdentitySourceStatus.PENDING_BINDING,
                        EnterpriseIdentitySourceStatus.ACTIVE,
                        EnterpriseIdentitySourceStatus.STALE,
                    )
                ),
                EnterpriseIdentitySource.sync_next_retry_at.is_not(None),
                EnterpriseIdentitySource.sync_next_retry_at > retry_lower_bound,
            )
            .order_by(EnterpriseIdentitySource.sync_next_retry_at)
            .limit(1)
        )
        active_sources = (
            await session.execute(
                select(EnterpriseIdentitySource).where(
                    EnterpriseIdentitySource.config_ciphertext.is_not(None),
                    EnterpriseIdentitySource.tenant_id.is_not(None),
                    EnterpriseIdentitySource.provider.in_(tuple(DIRECTORY_SYNC_ADAPTERS)),
                    EnterpriseIdentitySource.status.in_(
                        (EnterpriseIdentitySourceStatus.ACTIVE, EnterpriseIdentitySourceStatus.STALE)
                    ),
                )
            )
        ).scalars().all()

    delays = [min(configured_interval, settings.feishu_check_interval_seconds)]
    if next_retry_at is not None:
        if next_retry_at.tzinfo is None:
            next_retry_at = next_retry_at.replace(tzinfo=UTC)
        delays.append(max(0.0, (next_retry_at - now).total_seconds()))
    for source in active_sources:
        for due_at in directory_sync_adapter_for(source.provider).next_due_at(
            source, now, settings, configured_interval
        ):
            if source.sync_next_retry_at is not None:
                retry_at = source.sync_next_retry_at
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                due_at = max(due_at, retry_at)
            delays.append(max(0.0, (due_at - now).total_seconds()))
    return min(delays)


async def identity_source_sync_loop(
    session_factory: Any,
    *,
    interval_seconds: float | None = None,
) -> None:
    logger.info("enterprise_identity.source.scheduler_started")
    while True:
        configured_interval = (
            interval_seconds
            if interval_seconds is not None
            else get_config().enterprise_identity_sync.interval_seconds
        )
        try:
            selected_at = datetime.now(UTC)
            retry_needed = await sync_configured_identity_sources_once(
                session_factory,
                interval_seconds=configured_interval,
                now=selected_at,
            )
            sleep_seconds = (
                5.0
                if retry_needed
                else await _next_identity_source_sync_delay(
                    session_factory,
                    configured_interval,
                    selected_at=selected_at,
                )
            )
        except Exception:
            logger.exception("enterprise_identity.source.scheduler_failed")
            sleep_seconds = 5.0
        await asyncio.sleep(sleep_seconds)


async def request_feishu_full_sync_on_startup(session_factory: Any) -> None:
    """Reconcile once after startup to cover changes missed while PAS was offline."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        await session.execute(
            update(EnterpriseIdentitySource)
            .where(
                EnterpriseIdentitySource.provider == IdentitySourceProvider.FEISHU,
                EnterpriseIdentitySource.status != EnterpriseIdentitySourceStatus.DISABLED,
                EnterpriseIdentitySource.config_ciphertext.is_not(None),
                EnterpriseIdentitySource.tenant_id.is_not(None),
                EnterpriseIdentitySource.full_sync_requested_at.is_(None),
                or_(
                    EnterpriseIdentitySource.sync_kind.is_(None),
                    EnterpriseIdentitySource.sync_kind != "full",
                    EnterpriseIdentitySource.sync_worker_id.is_(None),
                    EnterpriseIdentitySource.sync_lease_until.is_(None),
                    EnterpriseIdentitySource.sync_lease_until <= now,
                ),
                or_(
                    EnterpriseIdentitySource.event_listener_status.is_(None),
                    EnterpriseIdentitySource.event_listener_status != FeishuEventSubscriptionStatus.ERROR,
                ),
            )
            .values(full_sync_requested_at=now)
        )
        await session.commit()
