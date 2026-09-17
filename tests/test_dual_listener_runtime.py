from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from server.db.schema import DatabaseSchemaError
from server.management.settings import ListenerSettings
from server.runtime import PasRuntime
from server.serve import serve_with_settings


async def test_runtime_owns_one_application_lifecycle() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def application_lifespan(app):
        events.append("start")
        app.state.runtime_access_policy = SimpleNamespace(mode="READY")
        yield
        events.append("stop")

    runtime = PasRuntime(application_lifespan)
    app = SimpleNamespace(state=SimpleNamespace())

    await runtime.start(app)

    assert runtime.phase.value == "READY"
    assert events == ["start"]
    with pytest.raises(RuntimeError, match="already started"):
        await runtime.start(app)

    await runtime.stop()
    await runtime.stop()

    assert runtime.phase.value == "STOPPING"
    assert events == ["start", "stop"]


@pytest.mark.parametrize(
    ("code", "phase"),
    [
        ("DATABASE_SCHEMA_OUTDATED", "SCHEMA_OUTDATED"),
        ("DATABASE_SCHEMA_TOO_NEW", "SCHEMA_TOO_NEW"),
    ],
)
async def test_runtime_reports_sanitized_schema_blockers(
    code: str,
    phase: str,
) -> None:
    @asynccontextmanager
    async def blocked_lifespan(_app):
        raise DatabaseSchemaError(code, "must-not-be-exposed")
        yield

    runtime = PasRuntime(blocked_lifespan)
    app = SimpleNamespace(state=SimpleNamespace())

    with pytest.raises(DatabaseSchemaError):
        await runtime.start(app)

    assert runtime.phase.value == phase
    assert runtime.snapshot() == {
        "phase": phase,
        "error_code": code,
    }
    assert "must-not-be-exposed" not in repr(runtime.snapshot())


async def test_runtime_can_retry_schema_failure_after_external_migration():
    attempts = 0
    @asynccontextmanager
    async def lifespan(app):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DatabaseSchemaError('DATABASE_SCHEMA_OUTDATED', 'await external migration')
        app.state.runtime_access_policy = SimpleNamespace(mode='READY')
        yield
    runtime = PasRuntime(lifespan)
    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(DatabaseSchemaError):
        await runtime.start(app)
    await runtime.start(app)
    assert runtime.phase.value == 'READY'
    assert runtime.error_code is None
    await runtime.stop()


async def test_schema_wait_serves_health_and_recovers_without_restart():
    from server.serve import StartupHealthGate
    from fastapi import FastAPI
    import httpx
    business = FastAPI()
    @business.get('/example')
    async def example():
        return {'ok': True}
    gate = StartupHealthGate(business)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate),base_url='http://test') as client:
        assert (await client.get('/livez')).status_code == 200
        assert (await client.get('/readyz')).status_code == 503
        assert (await client.get('/example')).status_code == 503
        gate.ready = True
        assert (await client.get('/example')).json() == {'ok': True}


class FakeServer:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events
        self.started = False
        self.should_exit = False

    async def serve(self) -> None:
        self.events.append(f"{self.name}:serve")
        self.started = True
        while not self.should_exit:
            await asyncio.sleep(0)
        self.events.append(f"{self.name}:exit")


class FakeRuntime:
    def __init__(
        self,
        events: list[str],
        *,
        start_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.start_error = start_error
        self.stop_count = 0

    async def start(self, _app) -> None:
        self.events.append("runtime:start")
        if self.start_error is not None:
            raise self.start_error

    async def stop(self) -> None:
        self.stop_count += 1
        self.events.append("runtime:stop")


async def test_coordinator_starts_management_then_runtime_then_business() -> None:
    events: list[str] = []
    shutdown = asyncio.Event()
    runtime = FakeRuntime(events)
    servers: dict[str, FakeServer] = {}
    listener_hosts: dict[str, str] = {}

    def app_factory(name: str):
        return SimpleNamespace(state=SimpleNamespace(listener_name=name))

    def server_factory(app, host: str, _port: int) -> FakeServer:
        name = app.state.listener_name
        server = FakeServer(name, events)
        servers[name] = server
        listener_hosts[name] = host
        if name == "business":
            shutdown.set()
        return server

    await serve_with_settings(
        ListenerSettings(
            business_port=18080,
            management_port=18081,
            auth_mode="trusted-network",
            token=None,
            managed_identity=None,
        ),
        runtime=runtime,
        business_app_factory=lambda: app_factory("business"),
        management_app_factory=lambda _runtime: app_factory("management"),
        server_factory=server_factory,
        shutdown_event=shutdown,
    )

    assert events.index("management:serve") < events.index("runtime:start")
    assert events.index("runtime:start") < events.index("business:serve")
    assert runtime.stop_count == 1
    assert servers["management"].should_exit is True
    assert servers["business"].should_exit is True
    assert listener_hosts == {
        "management": "0.0.0.0",
        "business": "0.0.0.0",
    }


async def test_schema_blocker_keeps_management_and_skips_business() -> None:
    events: list[str] = []
    shutdown = asyncio.Event()
    shutdown.set()
    runtime = FakeRuntime(
        events,
        start_error=DatabaseSchemaError(
            "DATABASE_SCHEMA_OUTDATED",
            "sanitized",
        ),
    )

    def app_factory(name: str):
        return SimpleNamespace(state=SimpleNamespace(listener_name=name))

    def server_factory(app, _host: str, _port: int) -> FakeServer:
        return FakeServer(app.state.listener_name, events)

    await serve_with_settings(
        ListenerSettings(
            business_port=18080,
            management_port=18081,
            auth_mode="trusted-network",
            token=None,
            managed_identity=None,
        ),
        runtime=runtime,
        business_app_factory=lambda: app_factory("business"),
        management_app_factory=lambda _runtime: app_factory("management"),
        server_factory=server_factory,
        shutdown_event=shutdown,
    )

    assert "management:serve" in events
    assert "runtime:start" in events
    assert "business:serve" not in events


async def test_schema_blocker_without_management_preserves_failure() -> None:
    events: list[str] = []
    runtime = FakeRuntime(
        events,
        start_error=DatabaseSchemaError(
            "DATABASE_SCHEMA_OUTDATED",
            "sanitized",
        ),
    )

    with pytest.raises(DatabaseSchemaError):
        await serve_with_settings(
            ListenerSettings(
                business_port=18080,
                management_port=None,
                auth_mode=None,
                token=None,
                managed_identity=None,
            ),
            runtime=runtime,
            business_app_factory=lambda: SimpleNamespace(
                state=SimpleNamespace(listener_name="business")
            ),
            management_app_factory=lambda _runtime: pytest.fail(
                "management app must stay disabled"
            ),
            server_factory=lambda app, _host, _port: FakeServer(
                app.state.listener_name,
                events,
            ),
            shutdown_event=asyncio.Event(),
        )

    assert events == ["runtime:start"]


async def test_local_sso_dev_mode_uses_loopback_for_both_listeners() -> None:
    events: list[str] = []
    shutdown = asyncio.Event()
    runtime = FakeRuntime(events)
    listener_hosts: list[tuple[str, str]] = []

    def app_factory(name: str):
        return SimpleNamespace(state=SimpleNamespace(listener_name=name))

    def server_factory(app, host: str, _port: int) -> FakeServer:
        name = app.state.listener_name
        listener_hosts.append((name, host))
        if name == "business":
            shutdown.set()
        return FakeServer(name, events)

    await serve_with_settings(
        ListenerSettings(
            business_port=18080,
            management_port=18081,
            auth_mode="trusted-network",
            token=None,
            managed_identity=None,
            listen_host="127.0.0.1",
            local_sso_dev_mode=True,
        ),
        runtime=runtime,
        business_app_factory=lambda: app_factory("business"),
        management_app_factory=lambda _runtime: app_factory("management"),
        server_factory=server_factory,
        shutdown_event=shutdown,
    )

    assert listener_hosts == [
        ("management", "127.0.0.1"),
        ("business", "127.0.0.1"),
    ]


@pytest.mark.parametrize('recover', [True, False])
async def test_coordinator_retries_schema_or_reports_timeout(recover):
    from server.serve import StartupHealthGate

    shutdown = asyncio.Event()
    attempts = 0
    events = []
    gates = []

    @asynccontextmanager
    async def lifecycle(app):
        nonlocal attempts
        attempts += 1
        if not recover or attempts == 1:
            raise DatabaseSchemaError('DATABASE_SCHEMA_OUTDATED', 'waiting')
        app.state.runtime_access_policy = SimpleNamespace(mode='READY')
        yield
        events.append('stopped')

    runtime = PasRuntime(lifecycle)

    def server_factory(app, _host, _port):
        if isinstance(app, StartupHealthGate):
            gates.append(app)
        return FakeServer('listener', events)

    task = asyncio.create_task(serve_with_settings(
        ListenerSettings(business_port=18080, management_port=18081,
                         auth_mode='trusted-network', token=None, managed_identity=None),
        runtime=runtime,
        business_app_factory=lambda: SimpleNamespace(state=SimpleNamespace()),
        management_app_factory=lambda _: SimpleNamespace(),
        server_factory=server_factory, shutdown_event=shutdown,
        schema_retry_interval=.001, schema_wait_timeout=.02,
    ))
    try:
        async with asyncio.timeout(2):
            while runtime.phase.value != ('READY' if recover else 'FAILED'):
                await asyncio.sleep(.001)
        assert attempts >= 2
        assert len(gates) == 1
        assert gates[0].ready is recover
        assert runtime.error_code == (None if recover else 'DATABASE_SCHEMA_WAIT_TIMEOUT')
    finally:
        shutdown.set()
        await task
    assert ('stopped' in events) is recover
