from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
import multiprocessing
from queue import Empty, Full
from typing import Any, Mapping
import uuid

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from server.core.crypto import decrypt
from server.enterprise_identity.service import apply_feishu_directory_event, fetch_feishu_directory_event
from server.enterprise_identity.sync import _feishu_event_lock, schedule_identity_source_sync
from server.models import (
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    EnterpriseIdentitySourceEvent,
    EnterpriseIdentitySourceEventStatus,
    FeishuEventSubscriptionStatus,
    IdentitySourceProvider,
)


logger = logging.getLogger(__name__)
_EVENT_ID_NAMESPACE = uuid.UUID("7346509e-177f-4a55-a792-aa5161fbc0e9")

SUPPORTED_FEISHU_DIRECTORY_EVENTS = frozenset({
    "contact.user.created_v3",
    "contact.user.updated_v3",
    "contact.user.deleted_v3",
    "contact.department.created_v3",
    "contact.department.updated_v3",
    "contact.department.deleted_v3",
    "contact.scope.updated_v3",
})
_LISTENER_LEASE_SECONDS = 30
_LISTENER_POLL_SECONDS = 5
_LISTENER_CONNECT_TIMEOUT_SECONDS = 20
_LISTENER_MAX_ATTEMPTS = 3
_EVENT_RETENTION = timedelta(days=7)


def normalize_feishu_event(payload: Mapping[str, object]) -> dict[str, str | None]:
    provider_event_id = payload.get("event_id")
    event_type = payload.get("event_type")
    event = payload.get("event")
    if not isinstance(provider_event_id, str) or not provider_event_id:
        raise ValueError("Feishu directory event ID is missing")
    if len(provider_event_id) > 255:
        raise ValueError("Feishu directory event ID is too long")
    if not isinstance(event_type, str) or event_type not in SUPPORTED_FEISHU_DIRECTORY_EVENTS:
        raise ValueError("Feishu directory event type is unsupported")
    if not isinstance(event, Mapping):
        raise ValueError("Feishu directory event body is invalid")

    subject_id: str | None = None
    if event_type != "contact.scope.updated_v3":
        candidates: list[Mapping[str, object]] = [event]
        for key in ("object", "user", "department"):
            value = event.get(key)
            if isinstance(value, Mapping):
                candidates.append(value)
        subject_keys = (
            ("open_department_id", "department_id", "id")
            if event_type.startswith("contact.department.")
            else ("user_id", "open_id", "id")
        )
        for candidate in candidates:
            for key in subject_keys:
                value = candidate.get(key)
                if isinstance(value, str) and value:
                    subject_id = value
                    break
            if subject_id is not None:
                break
        if subject_id is None:
            raise ValueError("Feishu directory event subject is missing")
        if len(subject_id) > 255:
            raise ValueError("Feishu directory event subject is too long")
    return {
        "provider_event_id": provider_event_id,
        "event_type": event_type,
        "subject_id": subject_id,
    }


def _event_row_id(source_id: str, provider_event_id: str) -> str:
    return str(uuid.uuid5(_EVENT_ID_NAMESPACE, f"{source_id}:{provider_event_id}"))


async def prune_expired_identity_source_events(
    session_factory: Any, *, now: datetime | None = None
) -> int:
    current = now or datetime.now(UTC)
    async with session_factory() as session:
        expired_processed = (await session.execute(
            select(EnterpriseIdentitySourceEvent.id, EnterpriseIdentitySourceEvent.identity_source_id,
                   EnterpriseIdentitySourceEvent.event_type)
            .where(
                EnterpriseIdentitySourceEvent.created_at <= current - _EVENT_RETENTION,
                EnterpriseIdentitySourceEvent.status == EnterpriseIdentitySourceEventStatus.PROCESSED,
            )
            .order_by(EnterpriseIdentitySourceEvent.created_at, EnterpriseIdentitySourceEvent.id)
            .limit(1000)
        )).all()
        user_event_source_ids = {
            source_id for _, source_id, event_type in expired_processed
            if event_type.startswith(("contact.user.", "user_"))
        }
        locked_sources = (
            list((await session.execute(
                select(EnterpriseIdentitySource)
                .where(EnterpriseIdentitySource.id.in_(user_event_source_ids))
                .with_for_update()
            )).scalars())
            if user_event_source_ids else []
        )
        needs_full = {
            source.id for source in locked_sources if source.sync_kind == "groups"
        }
        needs_full.update((await session.execute(
            select(EnterpriseIdentitySourceEvent.identity_source_id)
            .where(
                EnterpriseIdentitySourceEvent.created_at <= current - _EVENT_RETENTION,
                EnterpriseIdentitySourceEvent.status != EnterpriseIdentitySourceEventStatus.PROCESSED,
            )
            .distinct()
            .limit(1000)
        )).scalars())
        if needs_full:
            await session.execute(
                update(EnterpriseIdentitySource)
                .where(
                    EnterpriseIdentitySource.id.in_(needs_full),
                    or_(
                        EnterpriseIdentitySource.sync_kind.is_(None),
                        EnterpriseIdentitySource.sync_kind != "full",
                        EnterpriseIdentitySource.sync_worker_id.is_(None),
                        EnterpriseIdentitySource.sync_lease_until.is_(None),
                        EnterpriseIdentitySource.sync_lease_until <= current,
                    ),
                )
                .values(full_sync_requested_at=func.coalesce(
                    EnterpriseIdentitySource.full_sync_requested_at, current
                ))
            )
        if expired_processed:
            await session.execute(delete(EnterpriseIdentitySourceEvent).where(
                EnterpriseIdentitySourceEvent.id.in_([
                    event_id for event_id, _, _ in expired_processed
                ])
            ))
        await session.commit()
        return len(expired_processed)


async def ingest_feishu_event(
    session_factory: Any,
    source_id: str,
    event: Mapping[str, str | None],
) -> bool:
    provider_event_id = event.get("provider_event_id")
    event_type = event.get("event_type")
    if not isinstance(provider_event_id, str) or not isinstance(event_type, str):
        raise ValueError("normalized Feishu event is invalid")
    async with session_factory() as session:
        with session.no_autoflush:
            session.add(EnterpriseIdentitySourceEvent(
                id=_event_row_id(source_id, provider_event_id),
                identity_source_id=source_id,
                event_type=event_type,
                subject_id=event.get("subject_id"),
            ))
            await session.execute(
                update(EnterpriseIdentitySource)
                .where(EnterpriseIdentitySource.id == source_id)
                .values(
                    event_listener_status=FeishuEventSubscriptionStatus.ACTIVE,
                )
            )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
    return True


async def process_feishu_directory_events_once(
    session_factory: Any, *, source_ids: set[str] | None = None,
    apply_callable: Any = apply_feishu_directory_event,
    fetch_callable: Any = fetch_feishu_directory_event,
    schedule_callable: Any = schedule_identity_source_sync,
    now: datetime | None = None, draining_worker_id: str | None = None,
    event_cutoff: datetime | None = None,
) -> int:
    from server.enterprise_identity.directory_events import process_directory_events_once
    return await process_directory_events_once(
        session_factory, provider=IdentitySourceProvider.FEISHU, source_ids=source_ids,
        apply_callable=apply_callable, fetch_callable=fetch_callable,
        schedule_callable=schedule_callable, now=now, draining_worker_id=draining_worker_id,
        event_cutoff=event_cutoff,
    )


def _channel_process_main(
    app_id: str,
    app_secret: str,
    output_queue: Any,
) -> None:
    import _thread
    import threading
    import time

    from lark_channel.core.enum import LogLevel  # type: ignore[import-untyped]
    from lark_channel.event.dispatcher_handler import EventDispatcherHandler  # type: ignore[import-untyped]
    from lark_channel.ws import Client  # type: ignore[import-untyped]

    client_box: dict[str, Any] = {}

    def emit(message: dict[str, object]) -> None:
        try:
            output_queue.put(message, block=True, timeout=5)
        except Full as exc:
            raise RuntimeError("Feishu event queue is full") from exc

    def handle(event: Any) -> None:
        header = getattr(event, "header", None)
        emit({
            "kind": "event",
            "payload": {
                "event_id": getattr(header, "event_id", None),
                "event_type": getattr(header, "event_type", None),
                "event": getattr(event, "event", None),
            },
        })

    def monitor() -> None:
        connected = False
        while True:
            client = client_box.get("client")
            connection = getattr(client, "_conn", None) if client is not None else None
            if connection is not None and not connected:
                connected = True
                emit({"kind": "connected"})
            elif connected and connection is None:
                emit({"kind": "error", "error_type": "ConnectionClosed"})
                _thread.interrupt_main()
            time.sleep(0.5)

    try:
        builder = EventDispatcherHandler.builder("", "")
        for event_type in SUPPORTED_FEISHU_DIRECTORY_EVENTS:
            builder.register_p2_customized_event(event_type, handle)
        client = Client(
            app_id,
            app_secret,
            log_level=LogLevel.CRITICAL,
            event_handler=builder.build(),
            auto_reconnect=False,
        )
        client_box["client"] = client
        threading.Thread(target=monitor, daemon=True).start()
        client.start()
    except BaseException as exc:
        emit({"kind": "error", "error_type": type(exc).__name__})


@dataclass
class _ListenerRuntime:
    worker_id: str
    process: Any
    queue: Any
    started_at: datetime
    connected: bool = False


async def _claim_feishu_listener(
    session_factory: Any,
    source_id: str,
    worker_id: str,
    *,
    now: datetime,
) -> tuple[str, str] | None:
    async with session_factory() as session:
        claimed = await session.execute(
            update(EnterpriseIdentitySource)
            .where(
                EnterpriseIdentitySource.id == source_id,
                EnterpriseIdentitySource.provider == IdentitySourceProvider.FEISHU,
                EnterpriseIdentitySource.config_ciphertext.is_not(None),
                EnterpriseIdentitySource.tenant_id.is_not(None),
                EnterpriseIdentitySource.status != EnterpriseIdentitySourceStatus.DISABLED,
                EnterpriseIdentitySource.event_listener_retry_count < _LISTENER_MAX_ATTEMPTS,
                or_(
                    EnterpriseIdentitySource.event_listener_status.is_(None),
                    EnterpriseIdentitySource.event_listener_status != FeishuEventSubscriptionStatus.ERROR,
                ),
                or_(
                    EnterpriseIdentitySource.event_listener_worker_id.is_(None),
                    EnterpriseIdentitySource.event_listener_lease_until.is_(None),
                    EnterpriseIdentitySource.event_listener_lease_until <= now,
                ),
            )
            .values(
                event_listener_worker_id=worker_id,
                event_listener_lease_until=now + timedelta(seconds=_LISTENER_LEASE_SECONDS),
                event_listener_status=FeishuEventSubscriptionStatus.CONNECTING,
                event_unavailable_since=func.coalesce(
                    EnterpriseIdentitySource.event_unavailable_since, now
                ),
            )
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await session.rollback()
            return None
        source = await session.get(EnterpriseIdentitySource, source_id)
        if source is None or source.config_ciphertext is None:
            await session.rollback()
            return None
        try:
            config = json.loads(decrypt(source.config_ciphertext))
            app_id = config.get("app_id") if isinstance(config, dict) else None
            app_secret = config.get("app_secret") if isinstance(config, dict) else None
            if not isinstance(app_id, str) or not isinstance(app_secret, str):
                raise ValueError("identity source configuration is invalid")
        except Exception as exc:
            source.event_listener_retry_count += 1
            source.event_listener_status = (
                FeishuEventSubscriptionStatus.ERROR
                if source.event_listener_retry_count >= _LISTENER_MAX_ATTEMPTS
                else FeishuEventSubscriptionStatus.CONNECTING
            )
            source.event_listener_worker_id = None
            source.event_listener_lease_until = None
            await session.commit()
            logger.warning(
                "enterprise_identity.feishu_listener_configuration_failed",
                extra={"identity_source_id": source_id, "error_type": type(exc).__name__},
            )
            return None
        await session.commit()
        return app_id, app_secret


async def _set_listener_error(
    session_factory: Any,
    source_id: str,
    worker_id: str,
    error_type: str,
    *,
    was_connected: bool = False,
) -> None:
    async with session_factory() as session:
        source = await session.scalar(
            select(EnterpriseIdentitySource).where(
                EnterpriseIdentitySource.id == source_id,
                EnterpriseIdentitySource.event_listener_worker_id == worker_id,
            ).with_for_update()
        )
        if source is None:
            return
        source.event_listener_retry_count = (
            0 if was_connected else source.event_listener_retry_count + 1
        )
        source.event_listener_status = (
            FeishuEventSubscriptionStatus.ERROR
            if source.event_listener_retry_count >= _LISTENER_MAX_ATTEMPTS
            else FeishuEventSubscriptionStatus.CONNECTING
        )
        source.event_unavailable_since = source.event_unavailable_since or datetime.now(UTC)
        source.event_listener_worker_id = None
        source.event_listener_lease_until = None
        await session.commit()


async def _mark_listener_connected(session_factory: Any, source_id: str, worker_id: str) -> None:
    async with session_factory() as session:
        await session.execute(
            update(EnterpriseIdentitySource)
            .where(
                EnterpriseIdentitySource.id == source_id,
                EnterpriseIdentitySource.event_listener_worker_id == worker_id,
            )
            .values(
                event_listener_status=FeishuEventSubscriptionStatus.ACTIVE,
                event_unavailable_since=None,
                event_listener_retry_count=0,
            )
        )
        await session.commit()


async def reconnect_feishu_listener_before_full(session_factory: Any, source_id: str) -> bool:
    """Wait for a live listener before the full snapshot begins."""
    async with session_factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        if source is None:
            return False
        status = source.event_listener_status
        lease_until = source.event_listener_lease_until
        if lease_until is not None and lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=UTC)
        if (
            status == FeishuEventSubscriptionStatus.ACTIVE
            and source.event_listener_worker_id is not None
            and lease_until is not None
            and lease_until > datetime.now(UTC)
        ):
            return True
        if status == FeishuEventSubscriptionStatus.ACTIVE:
            source.event_listener_status = FeishuEventSubscriptionStatus.CONNECTING
            source.event_unavailable_since = source.event_unavailable_since or datetime.now(UTC)
        resumed = await session.execute(
            update(EnterpriseIdentitySource)
            .where(
                EnterpriseIdentitySource.id == source_id,
                EnterpriseIdentitySource.event_listener_status == FeishuEventSubscriptionStatus.ERROR,
            )
            .values(
                event_listener_status=FeishuEventSubscriptionStatus.CONNECTING,
                event_listener_retry_count=0,
            )
        )
        await session.commit()
    if status == FeishuEventSubscriptionStatus.ERROR and resumed.rowcount != 1:
        return False

    deadline = asyncio.get_running_loop().time() + (
        _LISTENER_MAX_ATTEMPTS * (_LISTENER_CONNECT_TIMEOUT_SECONDS + _LISTENER_POLL_SECONDS)
        + _LISTENER_POLL_SECONDS
    )
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(1)
        async with session_factory() as session:
            status = await session.scalar(
                select(EnterpriseIdentitySource.event_listener_status).where(
                    EnterpriseIdentitySource.id == source_id
                )
            )
        if status == FeishuEventSubscriptionStatus.ACTIVE:
            return True
        if status == FeishuEventSubscriptionStatus.ERROR:
            return False

    async with session_factory() as session:
        await session.execute(
            update(EnterpriseIdentitySource)
            .where(
                EnterpriseIdentitySource.id == source_id,
                EnterpriseIdentitySource.event_listener_status != FeishuEventSubscriptionStatus.ACTIVE,
            )
            .values(
                event_listener_status=FeishuEventSubscriptionStatus.ERROR,
                event_listener_retry_count=_LISTENER_MAX_ATTEMPTS,
                event_unavailable_since=func.coalesce(
                    EnterpriseIdentitySource.event_unavailable_since, datetime.now(UTC)
                ),
            )
        )
        await session.commit()
    return False


async def feishu_directory_event_loop(session_factory: Any) -> None:
    while True:
        try:
            await _run_feishu_directory_event_loop(session_factory)
        except Exception as exc:
            logger.warning(
                "enterprise_identity.feishu_listener_loop_restarting",
                extra={"error_type": type(exc).__name__},
            )
        await asyncio.sleep(_LISTENER_POLL_SECONDS)


async def _run_feishu_directory_event_loop(session_factory: Any) -> None:
    context = multiprocessing.get_context("spawn")
    listeners: dict[str, _ListenerRuntime] = {}
    try:
        while True:
            now = datetime.now(UTC)
            try:
                async with session_factory() as session:
                    source_ids = list((await session.execute(
                        select(EnterpriseIdentitySource.id).where(
                            EnterpriseIdentitySource.provider == IdentitySourceProvider.FEISHU,
                            EnterpriseIdentitySource.config_ciphertext.is_not(None),
                            EnterpriseIdentitySource.tenant_id.is_not(None),
                            EnterpriseIdentitySource.status != EnterpriseIdentitySourceStatus.DISABLED,
                            or_(
                                EnterpriseIdentitySource.event_listener_status.is_(None),
                                EnterpriseIdentitySource.event_listener_status != FeishuEventSubscriptionStatus.ERROR,
                            ),
                        )
                    )).scalars())
            except Exception as exc:
                logger.warning(
                    "enterprise_identity.feishu_listener_cycle_failed",
                    extra={"error_type": type(exc).__name__},
                )
                await asyncio.sleep(_LISTENER_POLL_SECONDS)
                continue

            configured_source_ids = set(source_ids)
            for source_id in set(listeners) - configured_source_ids:
                removed_runtime = listeners.pop(source_id)
                if removed_runtime.process.is_alive():
                    removed_runtime.process.terminate()
                removed_runtime.process.join(timeout=2)
                removed_runtime.queue.close()

            for source_id in source_ids:
                runtime = listeners.get(source_id)
                if runtime is None:
                    worker_id = str(uuid.uuid4())
                    try:
                        credentials = await _claim_feishu_listener(
                            session_factory, source_id, worker_id, now=now
                        )
                    except Exception as exc:
                        logger.warning(
                            "enterprise_identity.feishu_listener_configuration_failed",
                            extra={"identity_source_id": source_id, "error_type": type(exc).__name__},
                        )
                        continue
                    if credentials is None:
                        continue
                    output_queue = context.Queue(maxsize=1000)
                    process = context.Process(
                        target=_channel_process_main,
                        args=(*credentials, output_queue),
                        daemon=True,
                    )
                    process.start()
                    runtime = _ListenerRuntime(
                        worker_id=worker_id, process=process, queue=output_queue, started_at=now
                    )
                    listeners[source_id] = runtime

                if not runtime.process.is_alive():
                    await _set_listener_error(
                        session_factory, source_id, runtime.worker_id, "ListenerProcessExited",
                        was_connected=runtime.connected,
                    )
                    runtime.process.join(timeout=1)
                    runtime.queue.close()
                    listeners.pop(source_id, None)
                    continue

                try:
                    async with session_factory() as session:
                        renewed = await session.execute(
                            update(EnterpriseIdentitySource)
                            .where(
                                EnterpriseIdentitySource.id == source_id,
                                EnterpriseIdentitySource.event_listener_worker_id == runtime.worker_id,
                            )
                            .values(event_listener_lease_until=now + timedelta(seconds=_LISTENER_LEASE_SECONDS))
                        )
                        if renewed.rowcount != 1:
                            await session.rollback()
                            runtime.process.terminate()
                            runtime.process.join(timeout=2)
                            runtime.queue.close()
                            listeners.pop(source_id, None)
                            continue
                        await session.commit()
                except Exception as exc:
                    logger.warning(
                        "enterprise_identity.feishu_listener_cycle_failed",
                        extra={"identity_source_id": source_id, "error_type": type(exc).__name__},
                    )
                    continue

                while True:
                    try:
                        message = runtime.queue.get_nowait()
                    except Empty:
                        break
                    if message.get("kind") == "connected":
                        await _mark_listener_connected(session_factory, source_id, runtime.worker_id)
                        runtime.connected = True
                    elif message.get("kind") == "event":
                        try:
                            event = normalize_feishu_event(message.get("payload", {}))
                            await ingest_feishu_event(session_factory, source_id, event)
                        except Exception as exc:
                            await _set_listener_error(
                                session_factory, source_id, runtime.worker_id, type(exc).__name__,
                                was_connected=runtime.connected,
                            )
                    elif message.get("kind") == "error":
                        error_type = message.get("error_type")
                        await _set_listener_error(
                            session_factory,
                            source_id,
                            runtime.worker_id,
                            error_type if isinstance(error_type, str) else "ListenerError",
                            was_connected=runtime.connected,
                        )

                if not runtime.connected and (now - runtime.started_at).total_seconds() >= _LISTENER_CONNECT_TIMEOUT_SECONDS:
                    await _set_listener_error(
                        session_factory, source_id, runtime.worker_id, "ConnectionTimeout"
                    )
                    runtime.process.terminate()
                    runtime.process.join(timeout=2)
                    runtime.queue.close()
                    listeners.pop(source_id, None)

            local_source_ids = set(listeners)
            if not _feishu_event_lock.locked():
                async with _feishu_event_lock:
                    await process_feishu_directory_events_once(
                        session_factory,
                        source_ids=local_source_ids,
                    )
                    await prune_expired_identity_source_events(session_factory, now=now)
            await asyncio.sleep(_LISTENER_POLL_SECONDS)
    finally:
        for runtime in listeners.values():
            if runtime.process.is_alive():
                runtime.process.terminate()
            runtime.process.join(timeout=2)
            runtime.queue.close()
