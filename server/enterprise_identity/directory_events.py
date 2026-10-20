"""Provider-fenced directory inbox processing shared by Feishu and DingTalk."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import logging
from typing import Any
from sqlalchemy import or_, select, update
from server.enterprise_identity.sync import schedule_identity_source_sync
from server.models import (
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    EnterpriseIdentitySourceEvent,
    EnterpriseIdentitySourceEventStatus,
    FeishuEventSubscriptionStatus,
    IdentitySourceProvider,
)


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


logger = logging.getLogger(__name__)
_EVENT_RETRY_LIMIT = 5
_EVENT_RETRY_MAX_SECONDS = 1800


async def process_directory_events_once(
    session_factory: Any,
    *,
    provider: IdentitySourceProvider,
    source_ids: set[str] | None = None,
    apply_callable: Any,
    fetch_callable: Any,
    schedule_callable: Any = schedule_identity_source_sync,
    now: datetime | None = None,
    draining_worker_id: str | None = None,
    event_cutoff: datetime | None = None,
) -> int:
    current = now or datetime.now(UTC)
    async with session_factory() as session:
        query = (
            select(EnterpriseIdentitySourceEvent.id)
            .join(
                EnterpriseIdentitySource,
                EnterpriseIdentitySource.id == EnterpriseIdentitySourceEvent.identity_source_id,
            )
            .where(
                EnterpriseIdentitySource.provider == provider,
                EnterpriseIdentitySourceEvent.status == EnterpriseIdentitySourceEventStatus.PENDING,
                or_(
                    EnterpriseIdentitySourceEvent.next_retry_at.is_(None),
                    EnterpriseIdentitySourceEvent.next_retry_at <= current,
                ),
            )
            .order_by(EnterpriseIdentitySourceEvent.created_at, EnterpriseIdentitySourceEvent.id)
            .limit(100)
        )
        if source_ids is not None:
            if not source_ids:
                return 0
            query = query.where(EnterpriseIdentitySourceEvent.identity_source_id.in_(source_ids))
        if event_cutoff is not None:
            query = query.where(EnterpriseIdentitySourceEvent.created_at <= event_cutoff)
        event_ids = list((await session.execute(query)).scalars())

    processed = 0
    for event_id in event_ids:
        request_full_sync = False
        source_id: str | None = None
        async with session_factory() as session:
            event = await session.get(EnterpriseIdentitySourceEvent, event_id)
            if event is None or event.status != EnterpriseIdentitySourceEventStatus.PENDING:
                continue
            source = await session.scalar(
                select(EnterpriseIdentitySource).where(EnterpriseIdentitySource.id == event.identity_source_id)
            )
            if (
                source is None
                or source.provider != provider
                or source.status == EnterpriseIdentitySourceStatus.DISABLED
            ):
                event.status = EnterpriseIdentitySourceEventStatus.FAILED
                await session.commit()
                processed += 1
                continue
            if provider == IdentitySourceProvider.DINGTALK and (
                source.status not in (EnterpriseIdentitySourceStatus.ACTIVE, EnterpriseIdentitySourceStatus.STALE)
                or source.last_synced_at is None
            ):
                # Initial/configuration/scope rebuild must establish a complete
                # baseline before any queued subject notification can grant access.
                continue
            lease_until = source.sync_lease_until
            if lease_until is not None and lease_until.tzinfo is None:
                lease_until = lease_until.replace(tzinfo=UTC)
            if (
                source.sync_kind == "full"
                and lease_until is not None
                and lease_until > current
                and not (
                    draining_worker_id is not None
                    and draining_worker_id == source.sync_worker_id
                    and event_cutoff is not None
                    and _utc(event.created_at) <= _utc(event_cutoff)
                )
            ):
                continue
            source_id = source.id
            source_lock = (
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.id == source_id,
                    EnterpriseIdentitySource.status != EnterpriseIdentitySourceStatus.DISABLED,
                    EnterpriseIdentitySource.config_ciphertext == source.config_ciphertext,
                    EnterpriseIdentitySource.tenant_id == source.tenant_id,
                    EnterpriseIdentitySource.last_synced_at == source.last_synced_at,
                    EnterpriseIdentitySource.last_group_synced_at == source.last_group_synced_at,
                    EnterpriseIdentitySource.sync_worker_id == source.sync_worker_id,
                )
                .values(updated_at=EnterpriseIdentitySource.updated_at)
                .execution_options(synchronize_session=False)
            )
            event_type, subject_id = event.event_type, event.subject_id
            retry_count = event.retry_count
            fetched_after = datetime.now(UTC)
            try:
                data = await fetch_callable(source, event_type=event_type, subject_id=subject_id)
                # End the read snapshot before acquiring locks for the local apply.
                await session.rollback()
                locked = await session.execute(source_lock)
                if locked.rowcount != 1:
                    continue
                source = await session.get(EnterpriseIdentitySource, source_id)
                event = await session.get(EnterpriseIdentitySourceEvent, event_id, with_for_update=True)
                if (
                    event is None
                    or event.status != EnterpriseIdentitySourceEventStatus.PENDING
                    or event.retry_count != retry_count
                ):
                    continue
                if provider == IdentitySourceProvider.DINGTALK and await session.scalar(
                    select(EnterpriseIdentitySourceEvent.id)
                    .where(
                        EnterpriseIdentitySourceEvent.identity_source_id == source_id,
                        EnterpriseIdentitySourceEvent.created_at >= fetched_after,
                        EnterpriseIdentitySourceEvent.id != event_id,
                    )
                    .limit(1)
                ):
                    # A newer notification may have invalidated facts during IO.
                    # Re-fetch rather than resurrecting the previous memberships.
                    continue
                request_full_sync = await apply_callable(
                    session,
                    source,
                    event_type=event.event_type,
                    subject_id=event.subject_id,
                    data=data,
                )
            except Exception as exc:
                await session.rollback()
                locked = await session.execute(source_lock)
                if locked.rowcount != 1:
                    continue
                source = await session.get(EnterpriseIdentitySource, source_id)
                event = await session.get(EnterpriseIdentitySourceEvent, event_id, with_for_update=True)
                if (
                    event is None
                    or event.status != EnterpriseIdentitySourceEventStatus.PENDING
                    or event.retry_count != retry_count
                ):
                    continue
                event.retry_count += 1
                if provider == IdentitySourceProvider.FEISHU:
                    source.event_listener_status = FeishuEventSubscriptionStatus.ERROR
                    source.event_unavailable_since = source.event_unavailable_since or current
                else:
                    source.last_error = "DINGTALK_DIRECTORY_EVENT_FAILED"
                if event.retry_count >= _EVENT_RETRY_LIMIT:
                    event.status = EnterpriseIdentitySourceEventStatus.FAILED
                    if provider == IdentitySourceProvider.DINGTALK:
                        source.full_sync_requested_at = current
                else:
                    retry_seconds = min(60 * (2 ** (event.retry_count - 1)), _EVENT_RETRY_MAX_SECONDS)
                    event.next_retry_at = current + timedelta(seconds=retry_seconds)
                await session.commit()
                logger.warning(
                    "enterprise_identity.directory_event_failed",
                    extra={
                        "identity_source_id": source_id,
                        "event_type": event.event_type,
                        "error_type": type(exc).__name__,
                    },
                )
                processed += 1
                continue
            event.status = EnterpriseIdentitySourceEventStatus.PROCESSED
            event.next_retry_at = None
            if provider == IdentitySourceProvider.FEISHU:
                source.event_listener_status = FeishuEventSubscriptionStatus.ACTIVE
                source.event_unavailable_since = None
            if request_full_sync and (provider == IdentitySourceProvider.DINGTALK or source.sync_kind == "groups"):
                source.full_sync_requested_at = current
            await session.commit()
            processed += 1
        if request_full_sync and source_id is not None:
            await schedule_callable(session_factory, source_id)
    return processed
