from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, Protocol

import uvicorn
from server.db.schema import DatabaseSchemaError
from server.management.app import create_management_app
from server.management.settings import (
    ListenerSettings,
    load_listener_settings,
)
from server.runtime import PasRuntime, RuntimePhase
from starlette.responses import JSONResponse


class StartupHealthGate:
    """Keep existing business-port probes alive while external DDL completes."""

    def __init__(self, app):
        self.app = app
        self.state = app.state
        self.ready = False
        self.error_code = 'DATABASE_SCHEMA_WAITING'

    async def __call__(self, scope, receive, send):
        if self.ready:
            await self.app(scope, receive, send)
        elif scope['type'] == 'http':
            live = scope.get('path') == '/livez'
            response = JSONResponse(
                {'status': 'STARTING' if self.error_code == 'DATABASE_SCHEMA_WAITING' else 'FAILED', 'code': self.error_code},
                status_code=200 if live else 503,
                headers={} if live else {'Retry-After': '2'},
            )
            await response(scope, receive, send)
        elif scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1013})


class ServerHandle(Protocol):
    started: bool
    should_exit: bool

    async def serve(self) -> None: ...


ServerFactory = Callable[[Any, str, int], ServerHandle]


class CoordinatedUvicornServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


def _server_factory(app: Any, host: str, port: int) -> ServerHandle:
    return CoordinatedUvicornServer(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            lifespan="off",
        )
    )


async def _wait_until_started(
    server: ServerHandle,
    task: asyncio.Task[None],
) -> None:
    while not server.started:
        if task.done():
            await task
            raise RuntimeError("listener stopped before startup")
        await asyncio.sleep(0)


async def _wait_for_shutdown(
    shutdown_event: asyncio.Event,
    server_tasks: list[asyncio.Task[None]],
) -> None:
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    try:
        await asyncio.wait(
            [shutdown_task, *server_tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        shutdown_task.cancel()
        await asyncio.gather(shutdown_task, return_exceptions=True)


async def serve_with_settings(
    settings: ListenerSettings,
    *,
    runtime: Any,
    business_app_factory: Callable[[], Any],
    management_app_factory: Callable[[Any], Any],
    server_factory: ServerFactory,
    shutdown_event: asyncio.Event,
    schema_retry_interval: float = 2.0,
    schema_wait_timeout: float = 1800.0,
) -> None:
    servers: list[ServerHandle] = []
    server_tasks: list[asyncio.Task[None]] = []
    runtime_started = False
    try:
        if settings.management_port is not None:
            management_app = management_app_factory(runtime)
            management_server = server_factory(
                management_app,
                settings.listen_host,
                settings.management_port,
            )
            management_task = asyncio.create_task(
                management_server.serve()
            )
            servers.append(management_server)
            server_tasks.append(management_task)
            await _wait_until_started(
                management_server,
                management_task,
            )

        business_app = business_app_factory()
        try:
            await runtime.start(business_app)
            runtime_started = True
        except DatabaseSchemaError as error:
            if settings.management_port is None:
                raise
            recoverable = {'DATABASE_SCHEMA_OUTDATED', 'DATABASE_SCHEMA_NOT_INITIALIZED'}
            if error.code not in recoverable or shutdown_event.is_set():
                await _wait_for_shutdown(shutdown_event, server_tasks)
                return
            gate = StartupHealthGate(business_app)
            waiting_server = server_factory(gate, settings.listen_host, settings.business_port)
            waiting_task = asyncio.create_task(waiting_server.serve())
            servers.append(waiting_server)
            server_tasks.append(waiting_task)
            await _wait_until_started(waiting_server, waiting_task)
            deadline = time.monotonic() + schema_wait_timeout
            while time.monotonic() < deadline and not shutdown_event.is_set():
                if any(task.done() for task in server_tasks):
                    return
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=schema_retry_interval)
                    break
                except TimeoutError:
                    pass
                try:
                    await runtime.start(business_app)
                    runtime_started = True
                    gate.ready = True
                    break
                except DatabaseSchemaError as retry_error:
                    if retry_error.code not in recoverable:
                        gate.error_code = retry_error.code
                        break
            if not runtime_started and not shutdown_event.is_set():
                if time.monotonic() >= deadline:
                    runtime.error_code = 'DATABASE_SCHEMA_WAIT_TIMEOUT'
                    runtime.phase = RuntimePhase.FAILED
                    gate.error_code = runtime.error_code
            await _wait_for_shutdown(shutdown_event, server_tasks)
            return

        business_server = server_factory(
            business_app,
            settings.listen_host,
            settings.business_port,
        )
        business_task = asyncio.create_task(business_server.serve())
        servers.append(business_server)
        server_tasks.append(business_task)
        await _wait_until_started(business_server, business_task)
        await _wait_for_shutdown(shutdown_event, server_tasks)
    finally:
        for server in servers:
            server.should_exit = True
        if server_tasks:
            await asyncio.gather(*server_tasks, return_exceptions=True)
        if runtime_started:
            await runtime.stop()


def _signal_shutdown_event() -> asyncio.Event:
    event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_number in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_number, event.set)
    return event


async def serve(*, local_sso_dev_mode: bool = False) -> None:
    from server.app import application_lifespan, create_app

    settings = load_listener_settings(
        local_sso_dev_mode=local_sso_dev_mode,
    )
    runtime = PasRuntime(application_lifespan)

    def business_app():
        app = create_app(runtime=runtime)
        app.state.managed_mode = settings.management_port is not None
        app.state.local_sso_dev_mode = settings.local_sso_dev_mode
        return app

    await serve_with_settings(
        settings,
        runtime=runtime,
        business_app_factory=business_app,
        management_app_factory=lambda shared_runtime: (
            create_management_app(shared_runtime, settings)
        ),
        server_factory=_server_factory,
        shutdown_event=_signal_shutdown_event(),
    )
