"""Leased official Stream listener; events invalidate snapshots and request full reconciliation."""

from __future__ import annotations
import asyncio
from datetime import UTC, datetime, timedelta
import logging
import uuid
from sqlalchemy import case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from server.enterprise_identity.dingtalk_auth import verified_dingtalk_config, dingtalk_config_digest
from server.models import (
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    EnterpriseIdentitySourceEvent,
    FeishuEventSubscriptionStatus,
)

SUPPORTED_EVENTS = frozenset(
    {
        "user_add_org",
        "user_active_org",
        "user_modify_org",
        "user_leave_org",
        "org_dept_create",
        "org_dept_modify",
        "org_dept_remove",
    }
)
_NAMESPACE = uuid.UUID("5845049c-1048-4bf8-a29e-bb42859f7f23")
_SAFE_LOGGER = logging.Logger("dingtalk_stream.sanitized")
_SAFE_LOGGER.addHandler(logging.NullHandler())
_SAFE_LOGGER.propagate = False
_LOGGER = logging.getLogger(__name__)
_POLL_SECONDS = 5
_RETRY_MAX_SECONDS = 60


async def _record_failure(factory, source_id, worker_id, error):
    async with factory() as session:
        await session.execute(
            update(EnterpriseIdentitySource)
            .where(
                EnterpriseIdentitySource.id == source_id,
                EnterpriseIdentitySource.event_listener_worker_id == worker_id,
            )
            .values(
                event_listener_status=FeishuEventSubscriptionStatus.ERROR,
                event_listener_retry_count=EnterpriseIdentitySource.event_listener_retry_count + 1,
                event_unavailable_since=func.coalesce(
                    EnterpriseIdentitySource.event_unavailable_since, datetime.now(UTC)
                ),
            )
            .execution_options(synchronize_session=False)
        )
        await session.commit()
    # Never log SDK messages, ticket URLs, credentials or raw upstream bodies.
    _LOGGER.warning(
        "DingTalk Stream listener failed",
        extra={
            "identity_source_id": source_id,
            "error_type": type(error).__name__,
            "upstream_status": getattr(getattr(error, "response", None), "status_code", None),
        },
    )


def normalize_dingtalk_subjects(event_type, data):
    # The SDK has decoded the Stream data JSON. Some event versions include
    # an outer envelope; persist only validated IDs, never the diff or PII.
    if not isinstance(data, dict):
        return [None]
    body = data.get("data", data)
    if not isinstance(body, dict):
        return [None]
    raw = body.get("userId" if event_type.startswith("user_") else "deptId")
    if event_type == "user_active_org" and isinstance(body.get("userid"), str):
        raw = [body["userid"]]
    if not isinstance(raw, list) or not raw or len(raw) > 100:
        return [None]
    if any(
        isinstance(x, bool) or not isinstance(x, (str, int)) or not str(x).strip() or len(str(x)) > 255 for x in raw
    ):
        return [None]
    if any(str(x).strip() != str(x) for x in raw):
        return [None]
    if event_type.startswith("user_") and any(not isinstance(x, str) for x in raw):
        return [None]
    if not event_type.startswith("user_") and any(not str(x).isdigit() or int(x) <= 0 for x in raw):
        return [None]
    return sorted({str(x) for x in raw})


async def ingest_dingtalk_event(factory, source_id, worker_id, digest, *, event_id, event_type, corp_id, data=None):
    if event_type not in SUPPORTED_EVENTS or not isinstance(event_id, str) or not event_id or len(event_id) > 255:
        raise ValueError("INVALID_DIRECTORY_EVENT")
    if isinstance(data, dict) and data.get("eventCorpId", corp_id) != corp_id:
        raise ValueError("EVENT_SOURCE_CHANGED")
    subjects = normalize_dingtalk_subjects(event_type, data)
    async with factory() as session:
        now = datetime.now(UTC)
        # Acquire a source write fence on SQLite as well as row-lock databases.
        claimed = await session.execute(
            update(EnterpriseIdentitySource)
            .where(
                EnterpriseIdentitySource.id == source_id,
                EnterpriseIdentitySource.provider == IdentitySourceProvider.DINGTALK,
                EnterpriseIdentitySource.tenant_id == corp_id,
                EnterpriseIdentitySource.event_listener_worker_id == worker_id,
                EnterpriseIdentitySource.event_listener_lease_until > now,
                EnterpriseIdentitySource.status.in_(
                    (EnterpriseIdentitySourceStatus.ACTIVE, EnterpriseIdentitySourceStatus.STALE)
                ),
            )
            .values(updated_at=EnterpriseIdentitySource.updated_at)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise ValueError("EVENT_SOURCE_CHANGED")
        source = await session.get(EnterpriseIdentitySource, source_id)
        if dingtalk_config_digest(source, verified_dingtalk_config(source)) != digest:
            raise ValueError("EVENT_SOURCE_CHANGED")
        from server.enterprise_identity.dingtalk_incremental import invalidate_dingtalk_subjects

        row_subjects = {
            str(
                uuid.uuid5(
                    _NAMESPACE,
                    source_id + ":" + event_id + ("" if subject is None else ":" + event_type + ":" + subject),
                )
            ): subject
            for subject in subjects
        }
        existing_ids = set(
            await session.scalars(
                select(EnterpriseIdentitySourceEvent.id).where(
                    EnterpriseIdentitySourceEvent.id.in_(row_subjects),
                )
            )
        )
        new_rows = {row_id: subject for row_id, subject in row_subjects.items() if row_id not in existing_ids}
        if not new_rows:
            return False
        with session.no_autoflush:
            session.add_all(
                [
                    EnterpriseIdentitySourceEvent(
                        id=row_id, identity_source_id=source_id, event_type=event_type, subject_id=subject
                    )
                    for row_id, subject in new_rows.items()
                ]
            )
            if None in new_rows.values():
                source.last_synced_at = None
                source.status = EnterpriseIdentitySourceStatus.STALE
                source.full_sync_requested_at = now
            else:
                await invalidate_dingtalk_subjects(session, source, event_type, list(new_rows.values()))
        try:
            await session.commit()  # ACK only after inbox and revocation commit.
        except IntegrityError:
            await session.rollback()
            # Do not ACK a batch whose invalidations were rolled back.
            raise ValueError("DIRECTORY_EVENT_NOT_PERSISTED") from None
        return True


async def process_dingtalk_directory_events_once(factory, **kwargs):
    from server.enterprise_identity.directory_events import process_directory_events_once
    from server.enterprise_identity.dingtalk_incremental import (
        fetch_dingtalk_directory_event,
        apply_dingtalk_directory_event,
    )

    kwargs.setdefault("fetch_callable", fetch_dingtalk_directory_event)
    kwargs.setdefault("apply_callable", apply_dingtalk_directory_event)
    return await process_directory_events_once(factory, provider=IdentitySourceProvider.DINGTALK, **kwargs)


async def _listen(factory, source_id, worker_id, config, tenant_id, digest):
    import dingtalk_stream
    from server.enterprise_identity.dingtalk_stream_runtime import stream_once, StreamReconnect

    class Handler(dingtalk_stream.EventHandler):
        async def process(self, event):
            _LOGGER.info("DingTalk directory event received", extra={
                "identity_source_id": source_id,
                "supported": event.headers.event_type in SUPPORTED_EVENTS,
            })
            if event.headers.event_type not in SUPPORTED_EVENTS:
                return dingtalk_stream.AckMessage.STATUS_OK, "ignored"
            try:
                await ingest_dingtalk_event(
                    factory,
                    source_id,
                    worker_id,
                    digest,
                    event_id=event.headers.event_id,
                    event_type=event.headers.event_type,
                    corp_id=event.headers.event_corp_id,
                    data=event.data,
                )
                return dingtalk_stream.AckMessage.STATUS_OK, "OK"
            except ValueError:
                return 500, "reconcile required"

    handler = Handler()
    handler.logger = _SAFE_LOGGER
    client = dingtalk_stream.DingTalkStreamClient(
        dingtalk_stream.Credential(config["client_id"], config["client_secret"]),
        logger=_SAFE_LOGGER,
    )
    client.register_all_event_handler(handler)
    task = asyncio.create_task(stream_once(client, config))
    connected_once = False
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=_POLL_SECONDS)
            if task.done():
                await task
                raise ConnectionError("STREAM_STOPPED")
            async with factory() as session:
                source = await session.get(EnterpriseIdentitySource, source_id)
                if source is None or source.status == EnterpriseIdentitySourceStatus.DISABLED:
                    return
                if dingtalk_config_digest(source, verified_dingtalk_config(source)) != digest:
                    return
                result = await session.execute(
                    update(EnterpriseIdentitySource)
                    .where(
                        EnterpriseIdentitySource.id == source_id,
                        EnterpriseIdentitySource.event_listener_worker_id == worker_id,
                    )
                    .values(
                        event_listener_lease_until=datetime.now(UTC) + timedelta(seconds=30),
                        event_listener_status=FeishuEventSubscriptionStatus.ACTIVE
                        if client.websocket is not None
                        else FeishuEventSubscriptionStatus.CONNECTING,
                        # Reconnection cannot prove that no events were missed.
                        full_sync_requested_at=func.coalesce(
                            EnterpriseIdentitySource.full_sync_requested_at, datetime.now(UTC)
                        )
                        if client.websocket is not None and not connected_once
                        else EnterpriseIdentitySource.full_sync_requested_at,
                        event_listener_retry_count=0
                        if client.websocket is not None
                        else source.event_listener_retry_count,
                        event_unavailable_since=None
                        if client.websocket is not None
                        else source.event_unavailable_since,
                    )
                )
                await session.commit()
                if result.rowcount != 1:
                    return
                connected_once = connected_once or client.websocket is not None
        await task
        raise ConnectionError("STREAM_STOPPED")
    except asyncio.CancelledError:
        raise
    except StreamReconnect:
        async with factory() as session:
            await session.execute(
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.id == source_id,
                    EnterpriseIdentitySource.event_listener_worker_id == worker_id,
                )
                .values(
                    event_listener_status=FeishuEventSubscriptionStatus.CONNECTING,
                    event_unavailable_since=func.coalesce(
                        EnterpriseIdentitySource.event_unavailable_since, datetime.now(UTC)
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        return
    except Exception as error:
        await _record_failure(factory, source_id, worker_id, error)
        raise
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def dingtalk_directory_event_loop(factory):
    runtimes = {}
    worker_id = str(uuid.uuid4())
    retry_after = {}
    try:
        while True:
            try:
                await _reconcile_listeners(factory, worker_id, runtimes, retry_after)
                from server.enterprise_identity.sync import _directory_event_lock
                from server.enterprise_identity.feishu_events import prune_expired_identity_source_events

                async with _directory_event_lock:
                    await process_dingtalk_directory_events_once(factory)
                    await prune_expired_identity_source_events(factory)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _LOGGER.warning("DingTalk Stream supervisor retry", extra={"error_type": type(error).__name__})
            await asyncio.sleep(_POLL_SECONDS)
    finally:
        for task in runtimes.values():
            task.cancel()
        await asyncio.gather(*runtimes.values(), return_exceptions=True)
        async with factory() as session:
            await session.execute(
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.event_listener_worker_id == worker_id,
                )
                .values(
                    event_listener_worker_id=None,
                    event_listener_lease_until=None,
                    event_listener_status=case(
                        (
                            EnterpriseIdentitySource.event_listener_status == FeishuEventSubscriptionStatus.ERROR,
                            FeishuEventSubscriptionStatus.ERROR.value,
                        ),
                        else_=FeishuEventSubscriptionStatus.CONNECTING.value,
                    ),
                )
                .execution_options(synchronize_session=False)
            )
            await session.commit()


async def _reconcile_listeners(factory, worker_id, runtimes, retry_after):
    now = datetime.now(UTC)
    clock = asyncio.get_running_loop().time()
    async with factory() as session:
        sources = list(
            await session.scalars(
                select(EnterpriseIdentitySource).where(
                    EnterpriseIdentitySource.provider == IdentitySourceProvider.DINGTALK,
                    EnterpriseIdentitySource.status.in_(
                        (EnterpriseIdentitySourceStatus.ACTIVE, EnterpriseIdentitySourceStatus.STALE)
                    ),
                )
            )
        )
        eligible = {source.id for source in sources}
        for source_id in (set(runtimes) | set(retry_after)) - eligible:
            removed = runtimes.pop(source_id, None)
            if removed is not None:
                removed.cancel()
                await asyncio.gather(removed, return_exceptions=True)
            retry_after.pop(source_id, None)
            await session.execute(
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.id == source_id,
                    EnterpriseIdentitySource.event_listener_worker_id == worker_id,
                )
                .values(event_listener_worker_id=None, event_listener_lease_until=None)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
        for source in sources:
            existing = runtimes.get(source.id)
            if existing is not None and not existing.done():
                continue
            if existing is not None:
                outcome = (await asyncio.gather(existing, return_exceptions=True))[0]
                runtimes.pop(source.id, None)
                if isinstance(outcome, BaseException):
                    retry_after[source.id] = clock + min(
                        _RETRY_MAX_SECONDS, 2 ** min(source.event_listener_retry_count, 6)
                    )
            if clock < retry_after.get(source.id, 0):
                continue
            try:
                config = verified_dingtalk_config(source)
            except ValueError:
                continue
            claimed = await session.execute(
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.id == source.id,
                    or_(
                        EnterpriseIdentitySource.event_listener_lease_until.is_(None),
                        EnterpriseIdentitySource.event_listener_lease_until <= now,
                        EnterpriseIdentitySource.event_listener_worker_id == worker_id,
                    ),
                )
                .values(event_listener_worker_id=worker_id, event_listener_lease_until=now + timedelta(seconds=30))
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                continue
            await session.commit()
            runtimes[source.id] = asyncio.create_task(
                _listen(
                    factory,
                    source.id,
                    worker_id,
                    config,
                    source.tenant_id,
                    dingtalk_config_digest(source, config),
                )
            )
