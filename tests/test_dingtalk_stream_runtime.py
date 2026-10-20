import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import dingtalk_stream
import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.enterprise_identity import dingtalk_events as events
from server.enterprise_identity import dingtalk_stream_runtime as transport
from server.enterprise_identity.dingtalk_auth import dingtalk_config_digest, verified_dingtalk_config
from server.models import Base, EnterpriseIdentitySource, FeishuEventSubscriptionStatus
from tests.test_dingtalk_scope_security import seeded as source_fixture, ding_source


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    import base64

    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(b"x" * 32).decode())


seeded = source_fixture


@pytest.fixture
async def stream_source(seeded, tmp_path):
    _, source, *_ = await ding_source(seeded)
    # Independent connections, like production: in-memory SQLite's StaticPool
    # lets a concurrent reader rollback the listener's uncommitted write.
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "stream.db"))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        copied = EnterpriseIdentitySource(**{c.key: getattr(source, c.key) for c in source.__table__.columns})
        copied.event_listener_worker_id = "worker"
        copied.event_listener_status = FeishuEventSubscriptionStatus.CONNECTING
        session.add(copied)
        await session.commit()
    try:
        yield factory, copied, verified_dingtalk_config(copied)
    finally:
        await engine.dispose()


async def test_released_sdk_start_renew_and_cancel(stream_source, monkeypatch):
    factory, source, config = stream_source
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def connection(client, config):
        # Use the installed SDK's real constructor and handler registration.
        assert isinstance(client, dingtalk_stream.DingTalkStreamClient)
        assert client.event_handler is not None
        client.websocket = object()
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(transport, "stream_once", connection)
    monkeypatch.setattr(events, "_POLL_SECONDS", 0.01)
    task = asyncio.create_task(
        events._listen(factory, source.id, "worker", config, source.tenant_id, dingtalk_config_digest(source, config))
    )
    await asyncio.wait_for(entered.wait(), 1)
    for _ in range(100):
        if task.done():
            await task
        async with factory() as session:
            row = await session.get(EnterpriseIdentitySource, source.id)
            if (
                row.event_listener_status == FeishuEventSubscriptionStatus.ACTIVE
                and row.full_sync_requested_at is not None
            ):
                assert row.event_listener_lease_until is not None
                break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("listener never renewed its lease")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert cancelled.is_set()


async def test_failure_backoff_and_sqlite_reclaim(stream_source, monkeypatch):
    factory, source, _ = stream_source
    calls = []

    async def failure(*args):
        calls.append(1)
        raise TimeoutError("upstream ticket must not be logged")

    monkeypatch.setattr(transport, "stream_once", failure)
    runtimes, retries = {}, {}
    await events._reconcile_listeners(factory, "worker", runtimes, retries)
    await asyncio.gather(*runtimes.values(), return_exceptions=True)
    async with factory() as session:
        row = await session.get(EnterpriseIdentitySource, source.id)
        assert row.event_listener_status == FeishuEventSubscriptionStatus.ERROR
        assert row.event_listener_retry_count == 1
        assert row.event_unavailable_since is not None
    await events._reconcile_listeners(factory, "worker", runtimes, retries)
    assert not runtimes and len(calls) == 1
    retries[source.id] = 0
    # Second claim loads SQLite's naive datetime: must not compare it in Python.
    await events._reconcile_listeners(factory, "worker", runtimes, retries)
    await asyncio.gather(*runtimes.values(), return_exceptions=True)
    assert len(calls) == 2


async def test_supervisor_recovers_after_database_failure(monkeypatch):
    calls = []

    async def reconcile(*args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary database failure")
        raise asyncio.CancelledError

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        execute = AsyncMock()
        commit = AsyncMock()

    monkeypatch.setattr(events, "_reconcile_listeners", reconcile)
    monkeypatch.setattr(events, "_POLL_SECONDS", 0.001)
    with pytest.raises(asyncio.CancelledError):
        await events.dingtalk_directory_event_loop(Session)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"endpoint": "wss://evil.example", "ticket": "secret"},
        {"endpoint": "wss://u:p@stream.dingtalk.com", "ticket": "secret"},
    ],
)
async def test_transport_rejects_invalid_endpoint(monkeypatch, payload):
    class Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return httpx.Response(200, json=payload, request=httpx.Request("POST", "https://example.test"))

    monkeypatch.setattr(transport.httpx, "AsyncClient", lambda **kwargs: Http())
    client = SimpleNamespace(pre_start=lambda: None, OPEN_CONNECTION_API="unused", get_host_ip=lambda: "127.0.0.1")
    with pytest.raises(ValueError, match="INVALID_STREAM_CONNECTION"):
        await transport.stream_once(client, {"client_id": "app", "client_secret": "secret"})


async def test_transport_dispatch_and_cancellation_close_socket(monkeypatch):
    closed = asyncio.Event()

    class Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return httpx.Response(
                200,
                json={"endpoint": "wss://stream.dingtalk.com/connect", "ticket": "secret"},
                request=httpx.Request("POST", "https://example.test"),
            )

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.set()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not getattr(self, "sent", False):
                self.sent = True
                return json.dumps({"type": "EVENT"})
            await asyncio.Event().wait()

    def connect(url, **kwargs):
        assert kwargs["open_timeout"] == 10 and kwargs["max_size"] == 1048576
        assert kwargs["logger"] is events._SAFE_LOGGER
        return Socket()

    monkeypatch.setattr(transport.httpx, "AsyncClient", lambda **kwargs: Http())
    monkeypatch.setattr(transport.websockets, "connect", connect)
    client = SimpleNamespace(
        pre_start=lambda: None,
        OPEN_CONNECTION_API="unused",
        get_host_ip=lambda: "127.0.0.1",
        logger=events._SAFE_LOGGER,
        background_task=AsyncMock(),
        websocket=None,
    )
    task = asyncio.create_task(transport.stream_once(client, {"client_id": "app", "client_secret": "secret"}))
    for _ in range(100):
        if client.background_task.await_count:
            break
        await asyncio.sleep(0.001)
    client.background_task.assert_awaited_once_with({"type": "EVENT"})
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert closed.is_set() and client.websocket is None


@pytest.mark.parametrize("error, invalidated", [("TRANSIENT_UPSTREAM", False), ("UPSTREAM_ACCESS_DENIED", True)])
async def test_sync_failure_preserves_only_bounded_trusted_snapshot(seeded, error, invalidated):
    from server.enterprise_identity.dingtalk import DingTalkIdentityError
    from server.enterprise_identity.service import sync_identity_source, identity_source_snapshot_is_usable
    from server.models import IdentitySourceProvider

    session, source, *_ = await ding_source(seeded)
    original = source.last_synced_at

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def fetch_snapshot(self):
            raise DingTalkIdentityError(error)

    with pytest.raises(DingTalkIdentityError):
        await sync_identity_source(
            session, source, provider_client_factories={IdentitySourceProvider.DINGTALK: lambda **kw: Client()}
        )
    assert (source.last_synced_at is None) == invalidated
    if not invalidated:
        assert source.last_synced_at == original
    assert identity_source_snapshot_is_usable(source) == (not invalidated)


async def test_transient_failure_does_not_restore_event_invalidated_snapshot(seeded):
    from server.enterprise_identity.dingtalk import DingTalkIdentityError
    from server.enterprise_identity.service import sync_identity_source, identity_source_snapshot_is_usable
    from server.models import IdentitySourceProvider

    session, source, *_ = await ding_source(seeded)
    source.last_synced_at = None
    await session.commit()

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def fetch_snapshot(self):
            raise DingTalkIdentityError("TRANSIENT_UPSTREAM")

    with pytest.raises(DingTalkIdentityError):
        await sync_identity_source(
            session, source, provider_client_factories={IdentitySourceProvider.DINGTALK: lambda **kw: Client()}
        )
    assert not identity_source_snapshot_is_usable(source)


async def test_tenant_conflict_is_409_and_lock_preserves_space_timestamp(seeded):
    from server.core.identity_source_space import IdentityDomainTenantConflict
    from sqlalchemy import select
    from server.core.identity_source_space import ensure_identity_domain_tenant
    from server.models import PolarRAGSpace, IdentitySourceProvider

    session, source, *_ = await ding_source(seeded)
    space = await session.scalar(select(PolarRAGSpace))
    original = space.updated_at
    await ensure_identity_domain_tenant(session, source, space)
    await session.refresh(space)
    assert space.updated_at.replace(tzinfo=None) == original.replace(tzinfo=None)
    other = EnterpriseIdentitySource.create(
        name="Other corporation", provider=IdentitySourceProvider.DINGTALK, tenant_id="other-corp"
    )
    session.add(other)
    await session.flush()
    with pytest.raises(IdentityDomainTenantConflict) as error:
        await ensure_identity_domain_tenant(session, other, space)
    assert str(error.value) == "DINGTALK_IDENTITY_DOMAIN_TENANT_CONFLICT"


async def test_transport_normal_close_requests_reconnect(monkeypatch):
    class Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return httpx.Response(
                200,
                json={"endpoint": "wss://stream.dingtalk.com/connect", "ticket": "secret"},
                request=httpx.Request("POST", "https://example.test"),
            )

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    monkeypatch.setattr(transport.httpx, "AsyncClient", lambda **kwargs: Http())
    monkeypatch.setattr(transport.websockets, "connect", lambda *args, **kwargs: Socket())
    client = SimpleNamespace(
        pre_start=lambda: None,
        OPEN_CONNECTION_API="unused",
        get_host_ip=lambda: "127.0.0.1",
        logger=events._SAFE_LOGGER,
        websocket=None,
    )
    with pytest.raises(transport.StreamReconnect):
        await transport.stream_once(client, {"client_id": "app", "client_secret": "secret"})
    assert client.websocket is None
