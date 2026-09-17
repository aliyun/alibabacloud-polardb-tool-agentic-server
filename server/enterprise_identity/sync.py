from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from typing import Any
import uuid

import httpx
from sqlalchemy import or_, select, update

from server.config import get_config
from server.enterprise_identity.service import sync_identity_source
from server.models import (
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
)


logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 503})
_RETRY_BACKOFF_INITIAL_SECONDS = 60.0
_RETRY_BACKOFF_MAX_SECONDS = 1800.0
_SYNC_LEASE_SECONDS = 120.0
_SYNC_LEASE_RENEW_SECONDS = 30.0
_inflight_sync_tasks: dict[str, asyncio.Task[None]] = {}


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
    sync_callable: Any = sync_identity_source,
    worker_id: str,
    interval_seconds: float,
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
            checkpoint_client = await sync_callable(
                session,
                source,
                **client_factories,
            )
        except Exception as exc:
            retry_count = source.sync_retry_count + 1
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
                    status=EnterpriseIdentitySourceStatus.STALE,
                    last_error=type(exc).__name__,
                    sync_retry_count=retry_count,
                    sync_next_retry_at=datetime.now(UTC)
                    + timedelta(seconds=retry_delay),
                    sync_worker_id=None,
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
                last_error=None,
                sync_worker_id=None,
                sync_lease_until=None,
                sync_retry_count=0,
                sync_next_retry_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if completed.rowcount != 1:
            await session.rollback()
            return
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
    sync_callable: Any = sync_identity_source,
    worker_id: str,
    interval_seconds: float,
) -> None:
    renewer = asyncio.create_task(
        _renew_identity_source_sync_lease(session_factory, source_id, worker_id)
    )
    try:
        await _run_identity_source_sync_in_background(
            session_factory,
            source_id,
            feishu_client_factory=feishu_client_factory,
            sharepoint_client_factory=sharepoint_client_factory,
            acl_snapshot_client_factory=acl_snapshot_client_factory,
            sync_callable=sync_callable,
            worker_id=worker_id,
            interval_seconds=interval_seconds,
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
    sync_callable: Any = sync_identity_source,
    interval_seconds: float | None = None,
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
                sync_lease_until=now + timedelta(seconds=_SYNC_LEASE_SECONDS),
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
            sync_callable=sync_callable,
            worker_id=worker_id,
            interval_seconds=effective_interval,
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

    task.add_done_callback(_finished)
    return True


async def sync_configured_identity_sources_once(
    session_factory: Any,
    *,
    feishu_client_factory: Any = None,
    sharepoint_client_factory: Any = None,
    acl_snapshot_client_factory: Any = None,
    interval_seconds: float | None = None,
    now: datetime | None = None,
) -> bool:
    """Run each due source once without overlapping an existing source run."""
    effective_interval = (
        interval_seconds
        if interval_seconds is not None
        else get_config().enterprise_identity_sync.interval_seconds
    )
    current = now or datetime.now(UTC)
    due_at = current - timedelta(seconds=effective_interval)
    async with session_factory() as session:
        source_ids = list(
            (
                await session.execute(
                    select(EnterpriseIdentitySource.id)
                    .where(
                        EnterpriseIdentitySource.config_ciphertext.is_not(None),
                        EnterpriseIdentitySource.tenant_id.is_not(None),
                        EnterpriseIdentitySource.provider.in_(
                            (IdentitySourceProvider.FEISHU, IdentitySourceProvider.SHAREPOINT)
                        ),
                        or_(
                            EnterpriseIdentitySource.status.in_(
                                (
                                    EnterpriseIdentitySourceStatus.PENDING_BINDING,
                                    EnterpriseIdentitySourceStatus.STALE,
                                )
                            ),
                            (
                                EnterpriseIdentitySource.status == EnterpriseIdentitySourceStatus.ACTIVE
                            )
                            & or_(
                                EnterpriseIdentitySource.last_synced_at.is_(None),
                                EnterpriseIdentitySource.last_synced_at <= due_at,
                            ),
                        ),
                        or_(
                            EnterpriseIdentitySource.sync_next_retry_at.is_(None),
                            EnterpriseIdentitySource.sync_next_retry_at <= current,
                        ),
                    )
                    .order_by(EnterpriseIdentitySource.id)
                )
            ).scalars()
        )

    tasks: list[asyncio.Task[None]] = []
    for source_id in source_ids:
        started = await schedule_identity_source_sync(
            session_factory,
            source_id,
            feishu_client_factory=feishu_client_factory,
            sharepoint_client_factory=sharepoint_client_factory,
            acl_snapshot_client_factory=acl_snapshot_client_factory,
            interval_seconds=effective_interval,
        )
        task = sync_task(source_id)
        if started and task is not None:
            tasks.append(task)
    if tasks:
        await asyncio.gather(*tasks)

    if not source_ids:
        return False
    async with session_factory() as session:
        return bool(
            await session.scalar(
                select(EnterpriseIdentitySource.id)
                .where(
                    EnterpriseIdentitySource.id.in_(source_ids),
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
    retry_lower_bound = selected_at or now
    async with session_factory() as session:
        next_retry_at: datetime | None = await session.scalar(
            select(EnterpriseIdentitySource.sync_next_retry_at)
            .where(
                EnterpriseIdentitySource.config_ciphertext.is_not(None),
                EnterpriseIdentitySource.tenant_id.is_not(None),
                EnterpriseIdentitySource.provider.in_(
                    (IdentitySourceProvider.FEISHU, IdentitySourceProvider.SHAREPOINT)
                ),
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
    if next_retry_at is None:
        return configured_interval
    if next_retry_at.tzinfo is None:
        next_retry_at = next_retry_at.replace(tzinfo=UTC)
    return min(
        configured_interval,
        max(0.0, (next_retry_at - now).total_seconds()),
    )


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
