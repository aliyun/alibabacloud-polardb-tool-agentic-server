from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.config import reset_config
from server.core.crypto import encrypt
from server.core.super_connection_pool import (
    InvalidProvisioningCredential,
    StaleProvisioningCredential,
    SuperConnectionPoolManager,
)
from server.models import (
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    Instance,
    InstanceCredential,
    InstanceTopology,
    ProvisioningBackend,
)


class FakePool:
    def __init__(self):
        self.close = AsyncMock()
        self.wait_closed = AsyncMock()


def _instance() -> Instance:
    return Instance(
        id="instance-1",
        cluster_id="pc-1",
        name="Agentic DB",
        topology=InstanceTopology.MULTITENANT,
        host="pc.internal",
        port=3306,
    )


def _credential(
    password: str,
    *,
    version: int = 1,
    credential_id: str = "credential-1",
) -> InstanceCredential:
    return InstanceCredential(
        id=credential_id,
        instance_id="instance-1",
        name="root",
        purpose=CredentialPurpose.PROVISIONING_ADMIN,
        capability=CredentialCapability.ADMIN,
        status=CredentialStatus.ACTIVE,
        username_ciphertext=encrypt("root"),
        password_ciphertext=encrypt(password),
        version=version,
    )


def _backend(
    *,
    credential_id: str = "credential-1",
    ddl_concurrency: int = 7,
    config_revision: int = 1,
) -> ProvisioningBackend:
    backend = ProvisioningBackend(
        id="backend-1",
        instance_id="instance-1",
        admin_credential_id=credential_id,
        max_active_resources=10,
        ddl_concurrency=ddl_concurrency,
    )
    backend.config_revision = config_revision
    return backend


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    reset_config()
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    )
    yield
    reset_config()


async def test_get_reuses_pool_for_same_backend_and_credential_version(monkeypatch):
    created = FakePool()
    create_pool = AsyncMock(return_value=created)
    monkeypatch.setattr("asyncmy.create_pool", create_pool)
    manager = SuperConnectionPoolManager()
    backend, instance, credential = _backend(), _instance(), _credential("secret")

    first = await manager.get(backend, instance, credential)
    second = await manager.get(backend, instance, credential)

    assert first is second is created
    create_pool.assert_awaited_once()
    kwargs = create_pool.await_args.kwargs
    assert kwargs["maxsize"] == 7
    assert kwargs["autocommit"] is True
    assert kwargs["password"] == "secret"


async def test_credential_version_rotation_closes_old_pool(monkeypatch):
    first_pool, second_pool = FakePool(), FakePool()
    create_pool = AsyncMock(side_effect=[first_pool, second_pool])
    monkeypatch.setattr("asyncmy.create_pool", create_pool)
    manager = SuperConnectionPoolManager()
    backend, instance = _backend(), _instance()

    await manager.get(backend, instance, _credential("old", version=1))
    current = await manager.get(
        backend,
        instance,
        _credential("new", version=2),
    )

    assert current is second_pool
    first_pool.close.assert_called_once_with()
    first_pool.wait_closed.assert_awaited_once_with()


async def test_same_version_credential_replacement_closes_old_pool(monkeypatch):
    first_pool, second_pool = FakePool(), FakePool()
    create_pool = AsyncMock(side_effect=[first_pool, second_pool])
    monkeypatch.setattr("asyncmy.create_pool", create_pool)
    manager = SuperConnectionPoolManager()
    backend, instance = _backend(), _instance()

    await manager.get(
        backend,
        instance,
        _credential(
            "old",
            version=1,
            credential_id="credential-1",
        ),
    )
    backend.admin_credential_id = "credential-2"
    backend.config_revision = 2
    current = await manager.get(
        backend,
        instance,
        _credential(
            "new",
            version=1,
            credential_id="credential-2",
        ),
    )

    assert current is second_pool
    first_pool.close.assert_called_once_with()
    first_pool.wait_closed.assert_awaited_once_with()
    assert create_pool.await_count == 2
    assert create_pool.await_args.kwargs["password"] == "new"


async def test_ddl_concurrency_change_closes_old_pool(monkeypatch):
    first_pool, second_pool = FakePool(), FakePool()
    create_pool = AsyncMock(side_effect=[first_pool, second_pool])
    monkeypatch.setattr("asyncmy.create_pool", create_pool)
    manager = SuperConnectionPoolManager()
    backend, instance, credential = (
        _backend(),
        _instance(),
        _credential("secret"),
    )

    await manager.get(backend, instance, credential)
    backend.ddl_concurrency = 11
    backend.config_revision = 2
    current = await manager.get(backend, instance, credential)

    assert current is second_pool
    first_pool.close.assert_called_once_with()
    first_pool.wait_closed.assert_awaited_once_with()
    assert create_pool.await_count == 2
    assert create_pool.await_args.kwargs["maxsize"] == 11


async def test_late_stale_generation_cannot_replace_new_pool(monkeypatch):
    old_pool, new_pool, forbidden_pool = FakePool(), FakePool(), FakePool()
    create_pool = AsyncMock(
        side_effect=[old_pool, new_pool, forbidden_pool]
    )
    monkeypatch.setattr("asyncmy.create_pool", create_pool)
    manager = SuperConnectionPoolManager()
    instance = _instance()
    old_backend = _backend(
        credential_id="credential-1",
        ddl_concurrency=7,
        config_revision=1,
    )
    old_credential = _credential(
        "old",
        credential_id="credential-1",
    )
    new_backend = _backend(
        credential_id="credential-2",
        ddl_concurrency=11,
        config_revision=2,
    )
    new_credential = _credential(
        "new",
        credential_id="credential-2",
    )
    stale_backend = _backend(
        credential_id="credential-1",
        ddl_concurrency=7,
        config_revision=1,
    )
    stale_credential = _credential(
        "old",
        credential_id="credential-1",
    )

    await manager.get(old_backend, instance, old_credential)
    await manager._lock.acquire()
    new_request = asyncio.create_task(
        manager.get(new_backend, instance, new_credential)
    )
    await asyncio.sleep(0)
    stale_request = asyncio.create_task(
        manager.get(stale_backend, instance, stale_credential)
    )
    await asyncio.sleep(0)
    manager._lock.release()

    assert await new_request is new_pool
    with pytest.raises(RuntimeError, match="revision"):
        await stale_request
    assert manager.pool_count == 1
    assert create_pool.await_count == 2
    old_pool.close.assert_called_once_with()
    new_pool.close.assert_not_called()
    assert (
        await manager.get(new_backend, instance, new_credential)
        is new_pool
    )


async def test_stale_credential_cannot_replace_rotated_pool(monkeypatch):
    first_pool, second_pool = FakePool(), FakePool()
    create_pool = AsyncMock(side_effect=[first_pool, second_pool])
    monkeypatch.setattr("asyncmy.create_pool", create_pool)
    manager = SuperConnectionPoolManager()
    backend, instance = _backend(), _instance()
    stale = _credential("old", version=1)

    await manager.get(backend, instance, stale)
    current = await manager.get(
        backend,
        instance,
        _credential("new", version=2),
    )

    with pytest.raises(StaleProvisioningCredential):
        await manager.get(backend, instance, stale)
    assert manager.pool_count == 1
    assert current is second_pool
    assert create_pool.await_count == 2


async def test_rotation_waits_for_checked_out_old_pool_to_drain(monkeypatch):
    first_pool, second_pool = FakePool(), FakePool()
    checked_in = asyncio.Event()
    first_pool.wait_closed.side_effect = checked_in.wait
    monkeypatch.setattr(
        "asyncmy.create_pool",
        AsyncMock(side_effect=[first_pool, second_pool]),
    )
    manager = SuperConnectionPoolManager()
    backend, instance = _backend(), _instance()
    await manager.get(backend, instance, _credential("old", version=1))

    rotation = asyncio.create_task(
        manager.get(
            backend,
            instance,
            _credential("new", version=2),
        )
    )
    await asyncio.sleep(0)
    assert rotation.done() is False

    checked_in.set()
    assert await rotation is second_pool


async def test_close_backend_only_drains_target_backend(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr("asyncmy.create_pool", AsyncMock(return_value=pool))
    manager = SuperConnectionPoolManager()
    await manager.get(_backend(), _instance(), _credential("secret"))

    await manager.close_backend("backend-1")

    pool.close.assert_called_once_with()
    assert manager.pool_count == 0


async def test_invalid_credential_fails_before_decrypt(monkeypatch):
    manager = SuperConnectionPoolManager()
    credential = _credential("secret")
    credential.status = CredentialStatus.REVOKED
    decrypt = AsyncMock(side_effect=AssertionError("must not decrypt"))
    monkeypatch.setattr("server.core.super_connection_pool.decrypt", decrypt)

    with pytest.raises(InvalidProvisioningCredential):
        await manager.get(_backend(), _instance(), credential)

    decrypt.assert_not_called()


async def test_get_rejects_cross_instance_credential():
    manager = SuperConnectionPoolManager()
    credential = _credential("secret")
    credential.instance_id = "instance-2"

    with pytest.raises(InvalidProvisioningCredential):
        await manager.get(_backend(), _instance(), credential)
