from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.config import TenantProvisioningConfig
from server.core.adapter_registry import AdapterRegistry
from server.core.multitenant_health import ProvisioningHealthWorker
from server.core.provisioning_adapter import HealthResult
from server.models import (
    Base,
    CredentialCapability,
    CredentialPurpose,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceTopology,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningBackendStatus,
    User,
)


@pytest.fixture
async def health_env(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/health.db")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        creator = User(external_id="admin", display_name="Admin")
        session.add(creator)
        await session.flush()
        backend_ids = []
        for suffix, status in (
            ("active", ProvisioningBackendStatus.ACTIVE),
            ("draining", ProvisioningBackendStatus.DRAINING),
            ("disabled", ProvisioningBackendStatus.DISABLED),
        ):
            instance = Instance(
                cluster_id=f"pc-{suffix}",
                name=suffix,
                engine=InstanceEngine.POLARDB_MYSQL,
                topology=InstanceTopology.MULTITENANT,
            )
            session.add(instance)
            await session.flush()
            credential = InstanceCredential(
                instance_id=instance.id,
                name="admin",
                purpose=CredentialPurpose.PROVISIONING_ADMIN,
                capability=CredentialCapability.ADMIN,
                username_ciphertext="u",
                password_ciphertext="p",
                created_by_user_id=creator.id,
            )
            session.add(credential)
            await session.flush()
            backend = ProvisioningBackend(
                instance_id=instance.id,
                admin_credential_id=credential.id,
                status=status,
                max_active_resources=10,
            )
            session.add(backend)
            await session.flush()
            backend_ids.append(backend.id)
        await session.commit()
    yield factory, backend_ids
    await engine.dispose()


def _worker(factory, adapter, pool, now):
    registry = AdapterRegistry()
    registry.register(
        InstanceEngine.POLARDB_MYSQL,
        InstanceTopology.MULTITENANT,
        adapter,
    )
    return ProvisioningHealthWorker(
        factory,
        TenantProvisioningConfig(),
        registry,
        pool,
        clock=lambda: now,
    )


async def test_health_worker_checks_active_and_draining_backends(health_env):
    factory, backend_ids = health_env
    adapter = AsyncMock()
    adapter.health_check.return_value = HealthResult(True)
    pool = AsyncMock()
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    worker = _worker(factory, adapter, pool, now)

    assert await worker.run_once() == 2

    assert {
        call.args[0].id for call in adapter.health_check.await_args_list
    } == set(backend_ids[:2])
    pool.close_backend.assert_awaited_once_with(backend_ids[2])
    async with factory() as session:
        for backend_id in backend_ids[:2]:
            health = await session.get(ProvisioningBackendHealth, backend_id)
            assert health.healthy is True
            assert health.checked_at.replace(tzinfo=timezone.utc) == now
        assert (
            await session.get(ProvisioningBackendHealth, backend_ids[2])
            is None
        )


async def test_health_worker_records_sanitized_failure_count(health_env):
    factory, backend_ids = health_env
    adapter = AsyncMock()
    adapter.health_check.return_value = HealthResult(False, "RuntimeError")
    worker = _worker(
        factory,
        adapter,
        AsyncMock(),
        datetime(2026, 7, 25, tzinfo=timezone.utc),
    )

    await worker.run_once()
    await worker.run_once()

    async with factory() as session:
        health = await session.get(ProvisioningBackendHealth, backend_ids[0])
        assert health.healthy is False
        assert health.consecutive_failures == 2
        assert health.error_code == "RuntimeError"


async def test_health_worker_isolates_one_backend_failure(health_env):
    factory, backend_ids = health_env
    adapter = AsyncMock()
    adapter.health_check.side_effect = [
        RuntimeError("secret must not persist"),
        HealthResult(True),
    ]
    worker = _worker(
        factory,
        adapter,
        AsyncMock(),
        datetime(2026, 7, 25, tzinfo=timezone.utc),
    )

    assert await worker.run_once() == 2

    async with factory() as session:
        health_rows = [
            await session.get(ProvisioningBackendHealth, backend_id)
            for backend_id in backend_ids[:2]
        ]
        failed = next(row for row in health_rows if not row.healthy)
        succeeded = next(row for row in health_rows if row.healthy)
        assert failed.error_code == "RuntimeError"
        assert "secret" not in failed.error_code
        assert succeeded.error_code is None


async def test_disabled_pool_close_failure_does_not_abort_other_backends(
    health_env,
):
    factory, backend_ids = health_env
    adapter = AsyncMock()
    adapter.health_check.return_value = HealthResult(True)
    pool = AsyncMock()
    pool.close_backend.side_effect = RuntimeError("secret close failure")
    worker = _worker(
        factory,
        adapter,
        pool,
        datetime(2026, 7, 25, tzinfo=timezone.utc),
    )

    assert await worker.run_once() == 2
    assert adapter.health_check.await_count == 2
    async with factory() as session:
        disabled = await session.get(
            ProvisioningBackendHealth,
            backend_ids[2],
        )
        assert disabled.healthy is False
        assert disabled.error_code == "RuntimeError"


async def test_health_loop_recovers_after_pass_level_failure(
    health_env,
    monkeypatch,
):
    log_error = Mock()
    monkeypatch.setattr(
        "server.core.multitenant_health.logger.error",
        log_error,
    )
    factory, _backend_ids = health_env
    worker = _worker(
        factory,
        AsyncMock(),
        AsyncMock(),
        datetime(2026, 7, 25, tzinfo=timezone.utc),
    )
    stop = asyncio.Event()
    attempts = 0

    async def flaky_run_once():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary metadb failure")
        stop.set()
        return 0

    async def immediate_timeout(awaitable, *, timeout):
        del timeout
        awaitable.close()
        raise TimeoutError

    worker.run_once = flaky_run_once
    monkeypatch.setattr(
        "server.core.multitenant_health.asyncio.wait_for",
        immediate_timeout,
    )

    await worker.run_forever(stop)

    assert attempts == 2
    log_error.assert_called_once_with(
        "provisioning health pass failed with %s",
        "RuntimeError",
    )
