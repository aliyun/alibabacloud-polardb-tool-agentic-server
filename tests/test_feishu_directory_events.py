from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import base64
import json
import os
from queue import Empty, Queue
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event as sql_event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.enterprise_identity.feishu_events import (
    _channel_process_main,
    _claim_feishu_listener,
    _mark_listener_connected,
    _set_listener_error,
    feishu_directory_event_loop,
    reconnect_feishu_listener_before_full,
    ingest_feishu_event,
    normalize_feishu_event,
    process_feishu_directory_events_once,
)
from server.enterprise_identity import feishu_events
from server.core.crypto import encrypt
from server.models import (
    Base,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceEvent,
    EnterpriseIdentitySourceEventStatus,
    FeishuEventSubscriptionStatus,
    IdentitySourceProvider,
)


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}", connect_args={"timeout": 0.1})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _source(factory) -> EnterpriseIdentitySource:
    async with factory() as session:
        source = EnterpriseIdentitySource.create(
            name="Feishu events",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-events",
        )
        source.config_ciphertext = "configured"
        session.add(source)
        await session.commit()
        return source


async def _configured_source(factory, monkeypatch, credentials=None) -> EnterpriseIdentitySource:
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii"))
    source = await _source(factory)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.config_ciphertext = encrypt(json.dumps(
            credentials if credentials is not None else {"app_id": "app", "app_secret": "secret"}
        ))
        await session.commit()
    return source


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "removed", "request_full"), [
    (EnterpriseIdentitySourceEventStatus.PENDING, 0, True),
    (EnterpriseIdentitySourceEventStatus.PROCESSED, 1, False),
])
async def test_expired_event_gc_respects_processing_status(
    factory, status, removed, request_full,
) -> None:
    source = await _source(factory)
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(EnterpriseIdentitySourceEvent(
            id="expired",
            identity_source_id=source.id,
            event_type="contact.user.updated_v3",
            subject_id="user-1",
            status=status,
            created_at=now - timedelta(days=8),
        ))
        session.add(EnterpriseIdentitySourceEvent(
            id="recent-pending",
            identity_source_id=source.id,
            event_type="contact.user.updated_v3",
            subject_id="user-2",
            created_at=now - timedelta(days=1),
        ))
        await session.commit()

    assert await feishu_events.prune_expired_identity_source_events(factory, now=now) == removed
    async with factory() as session:
        ids = set((await session.execute(select(EnterpriseIdentitySourceEvent.id))).scalars())
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert ids == ({"recent-pending"} if removed else {"expired", "recent-pending"})
        assert (stored.full_sync_requested_at is not None) is request_full


@pytest.mark.asyncio
async def test_expired_processed_events_are_pruned_in_continuous_batches(factory) -> None:
    source = await _source(factory)
    now = datetime.now(UTC)
    async with factory() as session:
        session.add_all([
            EnterpriseIdentitySourceEvent(
                id=f"expired-processed-{index}",
                identity_source_id=source.id,
                event_type="contact.user.updated_v3",
                subject_id=f"user-{index}",
                status=EnterpriseIdentitySourceEventStatus.PROCESSED,
                created_at=now - timedelta(days=8),
            )
            for index in range(1001)
        ])
        await session.commit()

    assert await feishu_events.prune_expired_identity_source_events(factory, now=now) == 1000
    assert await feishu_events.prune_expired_identity_source_events(factory, now=now) == 1
    async with factory() as session:
        assert await session.scalar(select(func.count(EnterpriseIdentitySourceEvent.id))) == 0


@pytest.mark.asyncio
async def test_gc_rechecks_group_sync_after_select_before_deleting_user_event(
    factory, monkeypatch
) -> None:
    source = await _source(factory)
    now = datetime.now(UTC)
    async with factory() as session:
        session.add(EnterpriseIdentitySourceEvent(
            id="expired-before-group-claim",
            identity_source_id=source.id,
            event_type="contact.user.updated_v3",
            subject_id="user-1",
            status=EnterpriseIdentitySourceEventStatus.PROCESSED,
            created_at=now - timedelta(days=8),
        ))
        await session.commit()

    selected = asyncio.Event()
    resume = asyncio.Event()
    original_execute = AsyncSession.execute
    intercepted = False

    async def interleaved_execute(self, statement, *args, **kwargs):
        nonlocal intercepted
        result = await original_execute(self, statement, *args, **kwargs)
        sql = str(statement)
        if (
            not intercepted
            and "enterprise_identity_source_events.id" in sql
            and "ORDER BY enterprise_identity_source_events.created_at" in sql
        ):
            intercepted = True
            selected.set()
            await resume.wait()
        return result

    monkeypatch.setattr(AsyncSession, "execute", interleaved_execute)
    pruning = asyncio.create_task(
        feishu_events.prune_expired_identity_source_events(factory, now=now)
    )
    await selected.wait()
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.sync_kind = "groups"
        stored.sync_worker_id = "group-worker"
        stored.sync_lease_until = now + timedelta(minutes=2)
        await session.commit()
    resume.set()
    assert await pruning == 1

    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert stored.full_sync_requested_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(("event_type", "needs_full"), [
    ("contact.user.updated_v3", True),
    ("contact.department.updated_v3", False),
])
async def test_expired_processed_event_during_group_sync_requests_full_only_for_user(
    factory, event_type, needs_full
) -> None:
    source = await _source(factory)
    now = datetime.now(UTC)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.sync_kind = "groups"
        stored.sync_worker_id = "group-worker"
        stored.sync_lease_until = now + timedelta(minutes=2)
        session.add(EnterpriseIdentitySourceEvent(
            id="expired-during-group",
            identity_source_id=source.id,
            event_type=event_type,
            subject_id="user-1",
            status=EnterpriseIdentitySourceEventStatus.PROCESSED,
            created_at=now - timedelta(days=8),
            updated_at=now,
        ))
        await session.commit()

    assert await feishu_events.prune_expired_identity_source_events(factory, now=now) == 1
    async with factory() as session:
        assert await session.get(EnterpriseIdentitySourceEvent, "expired-during-group") is None
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert (stored.full_sync_requested_at is not None) is needs_full


@pytest.mark.asyncio
async def test_listener_loop_recovers_after_transient_database_failure(factory, monkeypatch, caplog) -> None:
    monkeypatch.setattr("server.enterprise_identity.feishu_events._LISTENER_POLL_SECONDS", 0.01)
    calls = 0
    recovered = asyncio.Event()

    def flaky_factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary database failure")
        recovered.set()
        return factory()

    task = asyncio.create_task(feishu_directory_event_loop(flaky_factory))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert not task.done()
        assert "enterprise_identity.feishu_listener_cycle_failed" in caplog.text
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_listener_loop_preserves_live_connection_after_lease_renewal_failure(
    factory, monkeypatch
) -> None:
    monkeypatch.setattr("server.enterprise_identity.feishu_events._LISTENER_POLL_SECONDS", 0.01)
    await _configured_source(factory, monkeypatch)

    class FakeProcess:
        def __init__(self, **_kwargs):
            self.started = False
            self.terminated = False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started and not self.terminated

        def terminate(self):
            self.terminated = True

        def join(self, *, timeout):
            pass

    class FakeQueue:
        def get_nowait(self):
            raise Empty

        def close(self):
            pass

    process = FakeProcess()
    monkeypatch.setattr(
        "server.enterprise_identity.feishu_events.multiprocessing.get_context",
        lambda *_args: type("Context", (), {
            "Queue": lambda *_args, **_kwargs: FakeQueue(),
            "Process": lambda *_args, **_kwargs: process,
        })(),
    )
    calls = 0
    recovered = asyncio.Event()

    def flaky_factory():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("temporary lease renewal failure")
        if calls >= 5:
            recovered.set()
        return factory()

    task = asyncio.create_task(feishu_directory_event_loop(flaky_factory))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert not task.done()
        assert not process.terminated
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_listener_loop_restarts_after_event_consumer_failure(factory, monkeypatch) -> None:
    monkeypatch.setattr("server.enterprise_identity.feishu_events._LISTENER_POLL_SECONDS", 0.01)
    calls = 0
    recovered = asyncio.Event()

    async def flaky_consumer(_factory, *, source_ids):
        nonlocal calls
        assert source_ids == set()
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary event database failure")
        recovered.set()
        return 0

    monkeypatch.setattr(
        "server.enterprise_identity.feishu_events.process_feishu_directory_events_once",
        flaky_consumer,
    )
    task = asyncio.create_task(feishu_directory_event_loop(factory))
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_normalize_feishu_user_and_department_events() -> None:
    assert normalize_feishu_event({
        "event_id": "evt-user",
        "event_type": "contact.user.updated_v3",
        "event": {"object": {"user_id": "u1", "name": "must-not-persist"}},
    }) == {
        "provider_event_id": "evt-user",
        "event_type": "contact.user.updated_v3",
        "subject_id": "u1",
    }
    assert normalize_feishu_event({
        "event_id": "evt-department",
        "event_type": "contact.department.deleted_v3",
        "event": {"object": {"open_department_id": "d1"}},
    })["subject_id"] == "d1"


def test_normalize_feishu_event_rejects_unsupported_or_incomplete_payloads() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        normalize_feishu_event({
            "event_id": "evt-message",
            "event_type": "im.message.receive_v1",
            "event": {},
        })
    with pytest.raises(ValueError, match="subject"):
        normalize_feishu_event({
            "event_id": "evt-user",
            "event_type": "contact.user.created_v3",
            "event": {"object": {}},
        })
    with pytest.raises(ValueError, match="too long"):
        normalize_feishu_event({
            "event_id": "e" * 256,
            "event_type": "contact.user.created_v3",
            "event": {"object": {"user_id": "u1"}},
        })


def test_channel_process_reports_only_sanitized_failure_type(monkeypatch) -> None:
    from lark_channel.core.enum import LogLevel

    observed_log_levels = []

    class FakeClient:
        def __init__(self, *_args, log_level, **_kwargs):
            observed_log_levels.append(log_level)
            self._conn = None

        def start(self):
            raise RuntimeError("https://secret.example.test/?ticket=do-not-log")

    class FakeThread:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("lark_channel.ws.Client", FakeClient)
    monkeypatch.setattr("threading.Thread", FakeThread)
    messages: Queue = Queue()

    _channel_process_main("app-id", "app-secret", messages)

    assert messages.get_nowait() == {"kind": "error", "error_type": "RuntimeError"}
    assert observed_log_levels == [LogLevel.CRITICAL]


@pytest.mark.asyncio
async def test_ingest_deduplicates_event_and_activates_subscription(factory) -> None:
    source = await _source(factory)
    unavailable_since = datetime.now(UTC) - timedelta(hours=25)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert stored is not None
        stored.event_unavailable_since = unavailable_since
        await session.commit()
    event = {
        "provider_event_id": "evt-1",
        "event_type": "contact.user.created_v3",
        "subject_id": "u1",
    }

    assert await ingest_feishu_event(factory, source.id, event) is True
    assert await ingest_feishu_event(factory, source.id, event) is False

    async with factory() as session:
        assert await session.scalar(select(func.count(EnterpriseIdentitySourceEvent.id))) == 1
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert stored is not None
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.ACTIVE
        assert stored.event_unavailable_since is not None


@pytest.mark.asyncio
async def test_event_waits_for_active_full_sync_lease(factory) -> None:
    source = await _source(factory)
    await ingest_feishu_event(factory, source.id, {
        "provider_event_id": "evt-during-full-lease",
        "event_type": "contact.user.deleted_v3",
        "subject_id": "u1",
    })
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.sync_kind = "full"
        stored.sync_worker_id = "full-worker"
        stored.sync_lease_until = datetime.now(UTC) + timedelta(minutes=2)
        await session.commit()

    applied: list[str] = []

    async def fake_apply(_session, _source, *, event_type, **_kwargs):
        applied.append(event_type)
        return False

    assert await process_feishu_directory_events_once(factory, apply_callable=fake_apply) == 0
    async with factory() as session:
        event = await session.scalar(select(EnterpriseIdentitySourceEvent))
        assert event.status == EnterpriseIdentitySourceEventStatus.PENDING
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.sync_kind = None
        stored.sync_worker_id = None
        stored.sync_lease_until = None
        await session.commit()
    assert await process_feishu_directory_events_once(factory, apply_callable=fake_apply) == 1
    assert applied == ["contact.user.deleted_v3"]


@pytest.mark.asyncio
async def test_process_event_marks_success_and_requests_scope_reconciliation(factory) -> None:
    source = await _source(factory)
    await ingest_feishu_event(factory, source.id, {
        "provider_event_id": "evt-scope",
        "event_type": "contact.scope.updated_v3",
        "subject_id": None,
    })
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert stored is not None
        stored.event_listener_status = FeishuEventSubscriptionStatus.ERROR
        await session.commit()
    applied: list[str] = []
    scheduled: list[str] = []

    async def fake_apply(_session, _source, *, event_type, subject_id, **_kwargs):
        assert subject_id is None
        applied.append(event_type)
        return True

    async def fake_schedule(_factory, source_id, **_kwargs):
        scheduled.append(source_id)
        return True

    assert await process_feishu_directory_events_once(
        factory,
        apply_callable=fake_apply,
        schedule_callable=fake_schedule,
        now=datetime.now(UTC),
    ) == 1
    assert applied == ["contact.scope.updated_v3"]
    assert scheduled == [source.id]
    async with factory() as session:
        event = await session.scalar(select(EnterpriseIdentitySourceEvent))
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert event is not None
        assert stored is not None
        assert event.status == EnterpriseIdentitySourceEventStatus.PROCESSED
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.ACTIVE
        assert stored.event_unavailable_since is None


async def test_scope_event_waits_for_running_group_reconciliation(factory) -> None:
    source = await _source(factory)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.sync_worker_id = "group-worker"
        stored.sync_kind = "groups"
        stored.sync_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()
    await ingest_feishu_event(factory, source.id, {
        "provider_event_id": "evt-scope-during-group",
        "event_type": "contact.scope.updated_v3",
        "subject_id": None,
    })

    async def fake_apply(*_args, **_kwargs):
        return True

    async def fake_schedule(*_args, **_kwargs):
        return False

    await process_feishu_directory_events_once(
        factory, apply_callable=fake_apply, schedule_callable=fake_schedule,
    )
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert stored.full_sync_requested_at is not None


@pytest.mark.asyncio
async def test_process_event_records_bounded_retry_without_staling_snapshot(factory) -> None:
    source = await _source(factory)
    await ingest_feishu_event(factory, source.id, {
        "provider_event_id": "evt-failure",
        "event_type": "contact.user.updated_v3",
        "subject_id": "u1",
    })

    async def failing_apply(*_args, **_kwargs):
        raise RuntimeError("secret provider response")

    assert await process_feishu_directory_events_once(
        factory,
        apply_callable=failing_apply,
        fetch_callable=AsyncMock(return_value={}),
        now=datetime.now(UTC),
    ) == 1
    async with factory() as session:
        event = await session.scalar(select(EnterpriseIdentitySourceEvent))
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert event is not None and stored is not None
        assert event.status == EnterpriseIdentitySourceEventStatus.PENDING
        assert event.retry_count == 1
        assert event.next_retry_at is not None
        assert stored.status.value == "pending_binding"


@pytest.mark.asyncio
async def test_listener_lease_allows_only_one_pas_replica(factory, monkeypatch) -> None:
    source = await _configured_source(factory, monkeypatch)

    now = datetime.now(UTC)
    assert await _claim_feishu_listener(
        factory, source.id, "worker-1", now=now
    ) == ("app", "secret")
    assert await _claim_feishu_listener(
        factory, source.id, "worker-2", now=now
    ) is None
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert stored is not None
        assert stored.event_unavailable_since is not None


@pytest.mark.asyncio
async def test_listener_error_pauses_new_connection_attempts(factory, monkeypatch) -> None:
    source = await _configured_source(factory, monkeypatch)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.event_listener_status = FeishuEventSubscriptionStatus.ERROR
        await session.commit()

    assert await _claim_feishu_listener(factory, source.id, "new-worker", now=datetime.now(UTC)) is None


@pytest.mark.asyncio
async def test_connected_listener_is_healthy_without_events(factory, monkeypatch) -> None:
    source = await _configured_source(factory, monkeypatch)

    assert await _claim_feishu_listener(factory, source.id, "worker", now=datetime.now(UTC))
    await _mark_listener_connected(factory, source.id, "worker")
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.ACTIVE
        assert stored.event_unavailable_since is None


@pytest.mark.asyncio
async def test_invalid_listener_config_stops_after_three_attempts(factory, monkeypatch) -> None:
    source = await _configured_source(factory, monkeypatch, {"app_id": "app"})

    for attempt in range(1, 4):
        assert await _claim_feishu_listener(
            factory, source.id, f"worker-{attempt}", now=datetime.now(UTC)
        ) is None
        async with factory() as session:
            stored = await session.get(EnterpriseIdentitySource, source.id)
            assert stored.event_listener_retry_count == attempt
            assert stored.event_listener_status == (
                FeishuEventSubscriptionStatus.ERROR
                if attempt == 3 else FeishuEventSubscriptionStatus.CONNECTING
            )
            assert stored.event_unavailable_since is not None
    assert await _claim_feishu_listener(factory, source.id, "worker-4", now=datetime.now(UTC)) is None


@pytest.mark.asyncio
async def test_listener_reconnects_twice_then_waits_for_full(factory, monkeypatch) -> None:
    source = await _configured_source(factory, monkeypatch)

    for attempt in range(1, 4):
        worker = f"worker-{attempt}"
        assert await _claim_feishu_listener(factory, source.id, worker, now=datetime.now(UTC))
        await _set_listener_error(factory, source.id, worker, "ConnectionClosed")
        async with factory() as session:
            stored = await session.get(EnterpriseIdentitySource, source.id)
            assert stored.event_listener_retry_count == attempt
            assert stored.event_listener_status == (
                FeishuEventSubscriptionStatus.ERROR
                if attempt == 3 else FeishuEventSubscriptionStatus.CONNECTING
            )
    assert await _claim_feishu_listener(factory, source.id, "worker-4", now=datetime.now(UTC)) is None


@pytest.mark.asyncio
async def test_disconnect_allows_three_new_reconnect_attempts(factory, monkeypatch) -> None:
    source = await _configured_source(factory, monkeypatch)

    assert await _claim_feishu_listener(factory, source.id, "initial", now=datetime.now(UTC))
    await _mark_listener_connected(factory, source.id, "initial")
    await _set_listener_error(factory, source.id, "initial", "ConnectionClosed", was_connected=True)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert stored.event_listener_retry_count == 0
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.CONNECTING

    for attempt in range(1, 4):
        worker = f"retry-{attempt}"
        assert await _claim_feishu_listener(factory, source.id, worker, now=datetime.now(UTC))
        await _set_listener_error(factory, source.id, worker, "ConnectionClosed")
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        assert stored.event_listener_retry_count == 3
        assert stored.event_listener_status == FeishuEventSubscriptionStatus.ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_active", [False, True], ids=["error", "stale-active"])
async def test_full_preconnect_rearms_listener_before_snapshot(factory, stale_active) -> None:
    source = await _source(factory)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.event_listener_status = (
            FeishuEventSubscriptionStatus.ACTIVE
            if stale_active else FeishuEventSubscriptionStatus.ERROR
        )
        if stale_active:
            stored.event_listener_lease_until = datetime.now(UTC) - timedelta(seconds=1)
        else:
            stored.event_listener_retry_count = 3
        await session.commit()

    async def connect():
        await asyncio.sleep(0.1)
        async with factory() as session:
            stored = await session.get(EnterpriseIdentitySource, source.id)
            assert stored.event_listener_status == FeishuEventSubscriptionStatus.CONNECTING
            assert stored.event_listener_retry_count == 0
            stored.event_listener_status = FeishuEventSubscriptionStatus.ACTIVE
            await session.commit()

    task = asyncio.create_task(connect())
    assert await reconnect_feishu_listener_before_full(factory, source.id) is True
    await task


async def test_file_sqlite_sync_commit_failure_then_listener_cancellation(
    tmp_path, monkeypatch,
):
    import sqlite3
    import threading

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import event, text
    from sqlalchemy.exc import OperationalError

    from server.app import create_app
    from server.auth.jwt_manager import create_access_token, reset_keys
    from server.config import reset_config
    from server.configuration.bootstrap import initialize_configuration
    from server.configuration.repository import ConfigRepository
    from server.core.config_crypto import ConfigCrypto
    from server.db.engine import get_engine, reset_engine
    from server.enterprise_identity.sync import _run_identity_source_sync_in_background
    from server.features import knowledge
    from server.models import Agent, AuthProvider, User, UserRole
    from tests._helpers import init_test_jwt_keys
    from tests._knowledge_helpers import enable_knowledge_routes

    path = tmp_path / "listener.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{path}?timeout=0.02")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(b"x" * 32).decode())
    reset_config()
    reset_engine()
    reset_keys()
    init_test_jwt_keys()
    engine = get_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    source = await _source(factory)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.sync_worker_id = "test-sync-worker"
        admin = User(external_id="test-admin", display_name="Admin", auth_provider=AuthProvider.BUILTIN,
                     role=UserRole.ADMIN)
        agent = Agent(name="Committed agent")
        session.add_all([admin, agent])
        await session.commit()
    repository = ConfigRepository(factory)
    await initialize_configuration(repository, ConfigCrypto(b"x" * 32))
    document = await repository.get_module("knowledge")
    enabled = document.model_copy(update={"effective": document.effective.model_copy(
        update={"config": {"enabled": True}}
    )})
    await repository.compare_and_set_module("knowledge", expected_revision=document.revision, document=enabled)
    runtime = knowledge.KnowledgeRuntime(knowledge.KnowledgeState(repository), managed=False)
    monkeypatch.setattr(knowledge, "_runtime", runtime)
    await runtime.start()

    reader = sqlite3.connect(path, timeout=0.02)
    entered, release = threading.Event(), threading.Event()
    listener_task = None

    async def completed_snapshot(*args, **kwargs):
        return None

    try:
        # Exercise the real sync completion UPDATE and its failed COMMIT.
        reader.execute("BEGIN")
        reader.execute("SELECT id FROM enterprise_identity_sources").fetchall()
        with pytest.raises(OperationalError, match="database is locked"):
            await _run_identity_source_sync_in_background(
                factory, source.id, worker_id="test-sync-worker", interval_seconds=60,
                sync_callable=completed_snapshot,
            )
        reader.rollback()

        def pause_select():
            if not entered.is_set():
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("listener SELECT was not released")
            return 0

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def pause_listener(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("SELECT enterprise_identity_sources.id ") and "event_listener_status IS NULL" in statement:
                connection.connection.dbapi_connection.run_async(
                    lambda driver: driver.set_progress_handler(pause_select, 1)
                )

        listener_task = asyncio.create_task(feishu_directory_event_loop(factory))
        assert await asyncio.to_thread(entered.wait, 5)
        listener_task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as retained_error:
            await listener_task
        assert retained_error.value.__traceback__ is not None

        # Both checks execute writes; neither can pass with the zombie lock.
        reader.execute("UPDATE agents SET name='Committed agent' WHERE id=?", (agent.id,))
        reader.commit()
        await runtime.heartbeat()
        assert await runtime.available()
        async with factory() as session:
            stored = await session.get(EnterpriseIdentitySource, source.id)
            assert stored.sync_worker_id == "test-sync-worker"
            assert stored.name == "Feishu events"
            assert await session.scalar(text("PRAGMA integrity_check")) == "ok"
        app = enable_knowledge_routes(create_app())
        app.state.knowledge_runtime = runtime
        headers = {"Authorization": "Bearer " + create_access_token({
            "sub": admin.id, "role": "admin", "credential_epoch": admin.credential_epoch,
        })}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            response = await http.get(f"/api/agents/{agent.id}", headers=headers)
            bindings = await http.get(f"/api/agents/{agent.id}/polarrag-bindings", headers=headers)
        assert bindings.status_code == 200, bindings.text
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "Committed agent"
    finally:
        release.set()
        if listener_task is not None and not listener_task.done():
            listener_task.cancel()
            await asyncio.gather(listener_task, return_exceptions=True)
        reader.close()
        await engine.dispose()
        reset_keys()
        reset_engine()
        reset_config()


async def test_file_sqlite_listener_error_then_busy_sync_commit_and_pool_overflow(
    tmp_path, monkeypatch, caplog,
):
    import sqlite3

    from sqlalchemy import event, or_, text
    from sqlalchemy.exc import OperationalError

    from server.config import reset_config
    from server.db.engine import get_engine, reset_engine
    from server.enterprise_identity.sync import _run_identity_source_sync_in_background
    from server.models import EnterpriseIdentitySourceStatus

    path = tmp_path / "listener-error.db"
    monkeypatch.setenv("PAS_DATABASE_URL", f"sqlite+aiosqlite:///{path}?timeout=0.02")
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(b"x" * 32).decode())
    reset_config()
    reset_engine()
    engine = get_engine()
    native_connections = []

    @event.listens_for(engine.sync_engine, "connect")
    def record_native(connection, _record):
        native_connections.append(connection.driver_connection._conn)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    source = await _source(factory)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.sync_worker_id = "test-sync-worker"
        await session.commit()
        # Warm the same SQLite prepared statement used by the listener.
        await session.execute(select(EnterpriseIdentitySource.id).where(
            EnterpriseIdentitySource.provider == IdentitySourceProvider.FEISHU,
            EnterpriseIdentitySource.config_ciphertext.is_not(None),
            EnterpriseIdentitySource.tenant_id.is_not(None),
            EnterpriseIdentitySource.status != EnterpriseIdentitySourceStatus.DISABLED,
            or_(EnterpriseIdentitySource.event_listener_status.is_(None),
                EnterpriseIdentitySource.event_listener_status != FeishuEventSubscriptionStatus.ERROR),
        ))

    reader = sqlite3.connect(path, timeout=0.02)
    listener_task = None

    async def completed_snapshot(*args, **kwargs):
        return None

    try:
        reader.execute("BEGIN EXCLUSIVE")
        listener_task = asyncio.create_task(feishu_directory_event_loop(factory))
        for _ in range(100):
            if any(record.message == "enterprise_identity.feishu_listener_cycle_failed" for record in caplog.records):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("listener did not report its real SQLite lock error")
        # Its retry sleep retains the actual SELECT exception and traceback.
        assert not listener_task.done()
        reader.rollback()
        reader.execute("BEGIN")
        reader.execute("SELECT id FROM enterprise_identity_sources").fetchall()

        async def fill_idle_pool():
            # Return five other connections first, so the active sync connection
            # is closed as overflow when it returns to the default size-5 pool.
            connections = [await engine.connect() for _ in range(5)]
            for connection in connections:
                await connection.close()

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def fill_pool_before_completion(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE enterprise_identity_sources SET"):
                connection.connection.dbapi_connection.run_async(lambda _driver: fill_idle_pool())

        with pytest.raises(OperationalError, match="database is locked"):
            await _run_identity_source_sync_in_background(
                factory, source.id, worker_id="test-sync-worker", interval_seconds=60,
                sync_callable=completed_snapshot,
            )
        reader.rollback()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            native_connections[0].execute("SELECT 1")
        reader.execute("UPDATE enterprise_identity_sources SET name='Still committed' WHERE id=?", (source.id,))
        reader.commit()
        async with factory() as session:
            stored = await session.get(EnterpriseIdentitySource, source.id)
            assert stored.sync_worker_id == "test-sync-worker"
            assert stored.name == "Still committed"
            assert await session.scalar(text("PRAGMA integrity_check")) == "ok"
    finally:
        if listener_task is not None:
            listener_task.cancel()
            await asyncio.gather(listener_task, return_exceptions=True)
        reader.close()
        await engine.dispose()
        reset_engine()
        reset_config()


@pytest.mark.parametrize("change", ["renew", "full", "completed", "config", "disabled", "cancel", "failure", "config_failure", "disabled_failure"])
async def test_event_fetch_does_not_lock_source_and_rechecks_before_apply(factory, monkeypatch, change):
    from server.models import EnterpriseDirectoryUser, EnterpriseIdentitySourceStatus
    from server.enterprise_identity.feishu import FeishuDirectoryClient

    source = await _configured_source(factory, monkeypatch)
    await ingest_feishu_event(factory, source.id, {
        "provider_event_id": "event-fetch", "event_type": "contact.user.updated_v3", "subject_id": "user-1",
    })
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    @sql_event.listens_for(factory.kw["bind"].sync_engine, "before_cursor_execute")
    def record_writes(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith(("UPDATE", "INSERT", "DELETE")) and asyncio.current_task().get_name() == "event-apply":
            writes.append(statement)

    async def fetch_user(_client, user_id):
        entered.set()
        await release.wait()
        if change in {"failure", "config_failure", "disabled_failure"}:
            raise RuntimeError("upstream unavailable")
        return {"id": user_id, "display_name": "Fetched", "email": None}

    async def memberships(_client, user_id):
        return []

    monkeypatch.setattr(FeishuDirectoryClient, "fetch_user", fetch_user)
    monkeypatch.setattr(FeishuDirectoryClient, "fetch_user_memberships", memberships)
    task = asyncio.create_task(process_feishu_directory_events_once(factory), name="event-apply")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not writes
        async with factory() as other:
            values = {"event_listener_worker_id": "renewer"}
            if change == "full":
                values.update(sync_kind="full", sync_worker_id="full-worker",
                              sync_lease_until=datetime.now(UTC) + timedelta(minutes=2))
            elif change == "completed":
                values.update(last_synced_at=datetime.now(UTC))
                await other.execute(update(EnterpriseIdentitySourceEvent).values(
                    status=EnterpriseIdentitySourceEventStatus.PROCESSED,
                ))
            elif change in {"config", "config_failure"}:
                values.update(config_ciphertext=encrypt(json.dumps({"app_id": "new", "app_secret": "new"})))
            elif change in {"disabled", "disabled_failure"}:
                values.update(status=EnterpriseIdentitySourceStatus.DISABLED)
            await other.execute(update(EnterpriseIdentitySource).where(
                EnterpriseIdentitySource.id == source.id,
            ).values(**values))
            await other.commit()
        if change == "cancel":
            task.cancel()
        release.set()
        if change == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task == (1 if change in {"renew", "failure"} else 0)
        async with factory() as check:
            assert await check.scalar(select(func.count(EnterpriseDirectoryUser.id))) == (1 if change == "renew" else 0)
            saved = await check.scalar(select(EnterpriseIdentitySourceEvent))
            assert saved.status == (
                EnterpriseIdentitySourceEventStatus.PROCESSED if change in {"renew", "completed"}
                else EnterpriseIdentitySourceEventStatus.PENDING
            )
            stored = await check.get(EnterpriseIdentitySource, source.id)
            assert stored.event_listener_worker_id == "renewer"
            assert saved.retry_count == (1 if change == "failure" else 0)
        if change == "renew":
            assert writes[0].startswith("UPDATE enterprise_identity_sources")
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("late_failure", [False, True])
async def test_event_retry_fences_response_from_previous_attempt(factory, late_failure):
    from server.models import EnterpriseDirectoryUser

    source = await _source(factory)
    async with factory() as session:
        stored = await session.get(EnterpriseIdentitySource, source.id)
        stored.sync_kind = "groups"
        stored.sync_worker_id = "group-worker"
        stored.sync_lease_until = datetime.now(UTC) + timedelta(minutes=2)
        await session.commit()
    for event_id in ("older", "newer"):
        await ingest_feishu_event(factory, source.id, {
            "provider_event_id": event_id, "event_type": "contact.user.updated_v3", "subject_id": "user-1",
        })
    entered, release = asyncio.Event(), asyncio.Event()

    async def old_fetch(_source, **kwargs):
        entered.set()
        await release.wait()
        if late_failure:
            raise RuntimeError("old attempt failed")
        return {"user": {"id": "user-1", "display_name": "Old", "email": None}, "memberships": [], "groups": []}

    # A listener and a group-sync drainer may run on separate PAS replicas.
    task = asyncio.create_task(process_feishu_directory_events_once(factory, fetch_callable=old_fetch))
    calls = 0

    async def new_fetch(_source, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("retry older event")
        return {"user": {"id": "user-1", "display_name": "New", "email": None}, "memberships": [], "groups": []}

    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await process_feishu_directory_events_once(
            factory, fetch_callable=new_fetch, draining_worker_id="group-worker", event_cutoff=datetime.now(UTC),
        ) == 2
        release.set()
        assert await task == 0
        async with factory() as check:
            user = await check.scalar(select(EnterpriseDirectoryUser))
            assert user.display_name == "New"
            events = (await check.scalars(select(EnterpriseIdentitySourceEvent).order_by(
                EnterpriseIdentitySourceEvent.created_at, EnterpriseIdentitySourceEvent.id,
            ))).all()
            assert events[0].retry_count == 1
            assert events[0].status == EnterpriseIdentitySourceEventStatus.PENDING
            assert events[1].status == EnterpriseIdentitySourceEventStatus.PROCESSED
            stored = await check.get(EnterpriseIdentitySource, source.id)
            assert stored.event_listener_status == FeishuEventSubscriptionStatus.ACTIVE
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
