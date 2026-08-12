from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from server.app import provisioning_runtime_lifespan
from server.config import TenantProvisioningConfig
from server.db.schema import DatabaseSchemaError, check_database_schema


def _runtime():
    return SimpleNamespace(
        dispatcher=SimpleNamespace(run_forever=AsyncMock()),
        health=SimpleNamespace(
            run_once=AsyncMock(return_value=0),
            run_forever=AsyncMock(),
        ),
        dedicated=SimpleNamespace(run_forever=AsyncMock()),
        dedicated_client_available=True,
        pool_manager=SimpleNamespace(close_all=AsyncMock()),
    )


class _BlockingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.run_requested = asyncio.Event()

    def request_run(self) -> None:
        self.run_requested.set()

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        self.started.set()
        try:
            await stop_event.wait()
        finally:
            self.stopped.set()


async def test_feature_flag_prevents_dedicated_worker_activation(
    monkeypatch,
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(
        "server.app._build_provisioning_runtime",
        AsyncMock(return_value=runtime),
    )
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))

    async with provisioning_runtime_lifespan(
        app,
        AsyncMock(),
        TenantProvisioningConfig(dedicated_pool_enabled=False),
    ):
        await asyncio.sleep(0)

    runtime.dedicated.run_forever.assert_not_called()
    assert app.state.dedicated_pool_worker_enabled is False


async def test_feature_flag_enables_dedicated_worker_activation(
    monkeypatch,
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(
        "server.app._build_provisioning_runtime",
        AsyncMock(return_value=runtime),
    )
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))

    async with provisioning_runtime_lifespan(
        app,
        AsyncMock(),
        TenantProvisioningConfig(dedicated_pool_enabled=True),
    ):
        await asyncio.sleep(0)
        runtime.dedicated.run_forever.assert_called_once()
        assert app.state.dedicated_pool_worker_enabled is True

    assert app.state.dedicated_pool_worker_enabled is False


async def test_missing_cloud_client_keeps_configured_worker_stopped(
    monkeypatch,
) -> None:
    runtime = _runtime()
    runtime.dedicated_client_available = False
    monkeypatch.setattr(
        "server.app._build_provisioning_runtime",
        AsyncMock(return_value=runtime),
    )
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))

    async with provisioning_runtime_lifespan(
        app,
        AsyncMock(),
        TenantProvisioningConfig(dedicated_pool_enabled=True),
    ):
        await asyncio.sleep(0)

    runtime.dedicated.run_forever.assert_not_called()
    assert app.state.dedicated_pool_worker_enabled is False


async def test_runtime_activation_starts_dedicated_worker_without_restart(
    monkeypatch,
) -> None:
    runtime = _runtime()
    worker = _BlockingWorker()
    runtime.dedicated = worker
    monkeypatch.setattr(
        "server.app._build_provisioning_runtime",
        AsyncMock(return_value=runtime),
    )
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))
    config = TenantProvisioningConfig(dedicated_pool_enabled=False)

    async with provisioning_runtime_lifespan(
        app,
        AsyncMock(),
        config,
    ):
        assert worker.started.is_set() is False
        config.dedicated_pool_enabled = True
        app.state.dedicated_pool_worker_supervisor.request_reconcile()
        await asyncio.wait_for(worker.started.wait(), timeout=1)
        assert app.state.dedicated_pool_worker_enabled is True

    assert worker.stopped.is_set() is True


async def test_supervisor_forwards_manual_worker_wakeup(monkeypatch) -> None:
    runtime = _runtime()
    worker = _BlockingWorker()
    runtime.dedicated = worker
    monkeypatch.setattr(
        "server.app._build_provisioning_runtime",
        AsyncMock(return_value=runtime),
    )
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))

    async with provisioning_runtime_lifespan(
        app,
        AsyncMock(),
        TenantProvisioningConfig(dedicated_pool_enabled=True),
    ):
        await asyncio.wait_for(worker.started.wait(), timeout=1)

        app.state.dedicated_pool_worker_supervisor.request_run()

        await asyncio.wait_for(worker.run_requested.wait(), timeout=1)


async def test_runtime_deactivation_stops_dedicated_worker_without_restart(
    monkeypatch,
) -> None:
    runtime = _runtime()
    worker = _BlockingWorker()
    runtime.dedicated = worker
    monkeypatch.setattr(
        "server.app._build_provisioning_runtime",
        AsyncMock(return_value=runtime),
    )
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))
    config = TenantProvisioningConfig(dedicated_pool_enabled=True)

    async with provisioning_runtime_lifespan(
        app,
        AsyncMock(),
        config,
    ):
        await asyncio.wait_for(worker.started.wait(), timeout=1)
        config.dedicated_pool_enabled = False
        app.state.dedicated_pool_worker_supervisor.request_reconcile()
        await asyncio.wait_for(worker.stopped.wait(), timeout=1)
        assert app.state.dedicated_pool_worker_enabled is False


async def test_credential_activation_rebuilds_and_starts_worker_without_restart(
    monkeypatch,
) -> None:
    runtime = _runtime()
    runtime.dedicated_client_available = False
    replacement = _BlockingWorker()
    rebuild = AsyncMock(return_value=(replacement, True))
    monkeypatch.setattr(
        "server.app._build_provisioning_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        "server.app._build_dedicated_worker_runtime",
        rebuild,
        raising=False,
    )
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))
    config = TenantProvisioningConfig(dedicated_pool_enabled=True)

    async with provisioning_runtime_lifespan(
        app,
        AsyncMock(),
        config,
    ):
        assert app.state.dedicated_pool_worker_enabled is False
        app.state.dedicated_pool_worker_supervisor.request_reconcile(
            rebuild=True,
        )
        await asyncio.wait_for(replacement.started.wait(), timeout=1)
        assert rebuild.await_count == 1
        assert app.state.dedicated_pool_worker_enabled is True

    assert replacement.stopped.is_set() is True


async def test_supervisor_survives_transient_client_rebuild_failure(
    monkeypatch,
) -> None:
    runtime = _runtime()
    runtime.dedicated_client_available = False
    replacement = _BlockingWorker()
    first_attempt = asyncio.Event()
    rebuild_count = 0

    async def rebuild(_session_factory, _config):
        nonlocal rebuild_count
        rebuild_count += 1
        if rebuild_count == 1:
            first_attempt.set()
            raise RuntimeError("temporary credential provider failure")
        return replacement, True

    monkeypatch.setattr(
        "server.app._build_provisioning_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        "server.app._build_dedicated_worker_runtime",
        rebuild,
        raising=False,
    )
    app = SimpleNamespace(state=SimpleNamespace(background_tasks=set()))
    config = TenantProvisioningConfig(dedicated_pool_enabled=True)

    async with provisioning_runtime_lifespan(
        app,
        AsyncMock(),
        config,
    ):
        app.state.dedicated_pool_worker_supervisor.request_reconcile(
            rebuild=True,
        )
        await asyncio.wait_for(first_attempt.wait(), timeout=1)
        app.state.dedicated_pool_worker_supervisor.request_reconcile(
            rebuild=True,
        )
        await asyncio.wait_for(replacement.started.wait(), timeout=1)
        assert rebuild_count == 2
        assert app.state.dedicated_pool_worker_enabled is True

    assert replacement.stopped.is_set() is True


async def test_old_binary_contract_rejects_future_schema_before_startup(
    tmp_path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'future.db'}"
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE alembic_version "
                    "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO alembic_version (version_num) "
                    "VALUES ('future_dedicated_schema')"
                )
            )
    finally:
        await engine.dispose()

    with pytest.raises(DatabaseSchemaError) as captured:
        await check_database_schema(database_url)

    assert captured.value.code == "DATABASE_SCHEMA_TOO_NEW"
