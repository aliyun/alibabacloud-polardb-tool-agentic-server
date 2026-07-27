from __future__ import annotations

import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Any

import asyncmy  # type: ignore[import-untyped]

from server.core.crypto import decrypt
from server.models import (
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    Instance,
    InstanceCredential,
    ProvisioningBackend,
)


class InvalidProvisioningCredential(RuntimeError):
    pass


class StaleProvisioningCredential(InvalidProvisioningCredential):
    pass


class StaleProvisioningBackend(InvalidProvisioningCredential):
    pass


def validate_provisioning_credential(
    backend: ProvisioningBackend,
    instance: Instance,
    credential: InstanceCredential,
) -> None:
    if (
        backend.instance_id != instance.id
        or backend.admin_credential_id != credential.id
        or credential.instance_id != instance.id
        or credential.resource_id is not None
        or credential.purpose != CredentialPurpose.PROVISIONING_ADMIN
        or credential.capability != CredentialCapability.ADMIN
        or credential.status != CredentialStatus.ACTIVE
        or credential.version < 1
        or backend.config_revision < 1
        or not credential.username_ciphertext
        or not credential.password_ciphertext
    ):
        raise InvalidProvisioningCredential(
            "Active provisioning admin credential is required"
        )
    if not instance.host or not instance.port:
        raise InvalidProvisioningCredential(
            "Provisioning backend endpoint is incomplete"
        )


class SuperConnectionPoolManager:
    def __init__(self) -> None:
        self._pools: dict[
            tuple[str, int, str, int, str, int, int], Any
        ] = {}
        self._current_versions: dict[tuple[str, str], int] = {}
        self._current_revisions: dict[str, int] = {}
        self._current_structures: dict[
            str, tuple[str, str, int, int]
        ] = {}
        self._lock = asyncio.Lock()

    @property
    def pool_count(self) -> int:
        return len(self._pools)

    async def _close_pool(self, pool: Any) -> None:
        close_result = pool.close()
        if inspect.isawaitable(close_result):
            await close_result
        await pool.wait_closed()

    async def get(
        self,
        backend: ProvisioningBackend,
        instance: Instance,
        credential: InstanceCredential,
    ):
        # Validation deliberately precedes decryption. Revoked, mismatched, or
        # malformed credential rows must fail closed without touching secrets.
        validate_provisioning_credential(backend, instance, credential)
        assert instance.host is not None
        assert instance.port is not None
        assert credential.username_ciphertext is not None
        assert credential.password_ciphertext is not None
        host = instance.host
        port = instance.port
        ddl_concurrency = backend.ddl_concurrency
        config_revision = backend.config_revision
        credential_id = credential.id
        credential_version = credential.version
        username_ciphertext = credential.username_ciphertext
        password_ciphertext = credential.password_ciphertext
        key = (
            backend.id,
            config_revision,
            credential_id,
            credential_version,
            host,
            port,
            ddl_concurrency,
        )
        structure = (
            credential_id,
            host,
            port,
            ddl_concurrency,
        )
        version_key = (backend.id, credential_id)
        async with self._lock:
            current_revision = self._current_revisions.get(backend.id)
            if (
                current_revision is not None
                and config_revision < current_revision
            ):
                raise StaleProvisioningBackend(
                    "Provisioning backend configuration revision is stale"
                )
            current_structure = self._current_structures.get(backend.id)
            if (
                current_revision == config_revision
                and current_structure is not None
                and structure != current_structure
            ):
                raise StaleProvisioningBackend(
                    "Provisioning backend generation does not match revision"
                )
            current_version = self._current_versions.get(version_key)
            if (
                current_version is not None
                and credential_version < current_version
            ):
                raise StaleProvisioningCredential(
                    "Provisioning credential version is stale"
                )
            existing = self._pools.get(key)
            if existing is not None:
                return existing

            if (
                current_revision is None
                or config_revision > current_revision
            ):
                # Advance the watermark before closing or creating pools. If
                # either operation fails, late requests carrying an older
                # persisted backend snapshot remain fenced.
                self._current_revisions[backend.id] = config_revision
                self._current_structures[backend.id] = structure

            stale_keys = [
                pool_key
                for pool_key in self._pools
                if pool_key[0] == backend.id and pool_key != key
            ]
            for stale_key in stale_keys:
                await self._close_pool(self._pools.pop(stale_key))

            pool = await asyncmy.create_pool(
                host=host,
                port=port,
                user=decrypt(username_ciphertext),
                password=decrypt(password_ciphertext),
                minsize=1,
                maxsize=ddl_concurrency,
                autocommit=True,
            )
            self._pools[key] = pool
            self._current_versions[version_key] = credential_version
            return pool

    @asynccontextmanager
    async def acquire(
        self,
        backend: ProvisioningBackend,
        instance: Instance,
        credential: InstanceCredential,
    ):
        pool = await self.get(backend, instance, credential)
        async with pool.acquire() as connection:
            await connection.ping(reconnect=False)
            yield connection

    async def close_backend(self, backend_id: str) -> None:
        async with self._lock:
            keys = [key for key in self._pools if key[0] == backend_id]
            for key in keys:
                await self._close_pool(self._pools.pop(key))

    async def close_all(self) -> None:
        async with self._lock:
            pools = list(self._pools.values())
            self._pools.clear()
            for pool in pools:
                await self._close_pool(pool)
