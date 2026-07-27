from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import asyncmy  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.core.crypto import decrypt
from server.core.multitenant_ddl import MultitenantDDLAdapter
from server.core.super_connection_pool import (
    SuperConnectionPoolManager,
    validate_provisioning_credential,
)
from server.models import (
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    Instance,
    InstanceCredential,
    LeaseCleanupStep,
    LeaseProvisioningStep,
    ProvisioningBackend,
)


@dataclass(frozen=True, slots=True)
class HealthResult:
    healthy: bool
    error_code: str | None = None


class ProvisioningAdapter(Protocol):
    async def create(self, resource: DBInstanceResource) -> None: ...

    async def delete(self, resource: DBInstanceResource) -> None: ...

    async def verify(self, resource: DBInstanceResource) -> None: ...

    async def health_check(self, backend: ProvisioningBackend) -> HealthResult: ...


class InvalidResourceCredential(RuntimeError):
    pass


class InvalidResourceIdentity(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _BackendContext:
    backend: ProvisioningBackend
    instance: Instance
    credential: InstanceCredential


class PolarDBMySQLMultitenantAdapter:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        pool_manager: SuperConnectionPoolManager,
    ) -> None:
        self._session_factory = session_factory
        self._pool_manager = pool_manager

    async def _context(self, backend_id: str) -> _BackendContext:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ProvisioningBackend,
                        Instance,
                        InstanceCredential,
                    )
                    .join(
                        Instance,
                        Instance.id == ProvisioningBackend.instance_id,
                    )
                    .join(
                        InstanceCredential,
                        InstanceCredential.id
                        == ProvisioningBackend.admin_credential_id,
                    )
                    .where(ProvisioningBackend.id == backend_id)
                )
            ).one_or_none()
            if row is None:
                raise RuntimeError("Provisioning backend is unavailable")
            backend, instance, credential = row
            validate_provisioning_credential(backend, instance, credential)
            return _BackendContext(backend, instance, credential)

    @staticmethod
    def _resource_credential(
        resource: DBInstanceResource,
    ) -> tuple[str, str]:
        credentials = [
            credential
            for credential in resource.credentials
            if credential.purpose == CredentialPurpose.RESOURCE_ACCESS
        ]
        if len(credentials) != 1:
            raise InvalidResourceCredential(
                "Exactly one resource access credential is required"
            )
        credential = credentials[0]
        if (
            credential.resource_id != resource.id
            or credential.instance_id is not None
            or credential.status != CredentialStatus.ACTIVE
            or credential.capability != CredentialCapability.READWRITE
            or credential.version < 1
            or not credential.username_ciphertext
            or not credential.password_ciphertext
            or credential.database_name != resource.database_name
        ):
            raise InvalidResourceCredential(
                "Active resource access credential is required"
            )
        # No decryption is attempted until the complete row has passed validation.
        return (
            decrypt(credential.username_ciphertext),
            decrypt(credential.password_ciphertext),
        )

    async def _forward_ddl(
        self, resource: DBInstanceResource
    ) -> tuple[_BackendContext, MultitenantDDLAdapter, str]:
        context = await self._context(resource.backend_id)
        username, password = self._resource_credential(resource)
        ddl = MultitenantDDLAdapter(
            self._pool_manager,
            context.backend,
            context.instance,
            context.credential,
            username,
        )
        return context, ddl, password

    @staticmethod
    def _cleanup_account_name(resource: DBInstanceResource) -> str:
        tenant = resource.tenant_name
        database = resource.database_name
        resource_config = resource.resource_config_name
        if (
            not tenant
            or database != f"agentic@{tenant}"
            or resource_config != f"rc_{tenant}"
        ):
            raise InvalidResourceIdentity(
                "Database resource identity is inconsistent"
            )
        return database

    async def _cleanup_ddl(
        self,
        resource: DBInstanceResource,
    ) -> MultitenantDDLAdapter:
        context = await self._context(resource.backend_id)
        return MultitenantDDLAdapter(
            self._pool_manager,
            context.backend,
            context.instance,
            context.credential,
            self._cleanup_account_name(resource),
        )

    async def create(self, resource: DBInstanceResource) -> None:
        _context, ddl, password = await self._forward_ddl(resource)
        if resource.provisioning_step == LeaseProvisioningStep.PENDING:
            await ddl.create_resource_config(resource)
            resource.provisioning_step = (
                LeaseProvisioningStep.RESOURCE_CONFIG_CREATED
            )
        elif (
            resource.provisioning_step
            == LeaseProvisioningStep.RESOURCE_CONFIG_CREATED
        ):
            await ddl.create_tenant(resource)
            resource.provisioning_step = LeaseProvisioningStep.TENANT_CREATED
        elif resource.provisioning_step == LeaseProvisioningStep.TENANT_CREATED:
            await ddl.create_user(resource, password)
            resource.provisioning_step = LeaseProvisioningStep.USER_CREATED
        elif resource.provisioning_step == LeaseProvisioningStep.USER_CREATED:
            await ddl.create_database(resource)
            resource.provisioning_step = LeaseProvisioningStep.DATABASE_CREATED
        elif (
            resource.provisioning_step
            == LeaseProvisioningStep.DATABASE_CREATED
        ):
            await ddl.grant_privileges(resource)
            resource.provisioning_step = LeaseProvisioningStep.GRANTED
        elif resource.provisioning_step in {
            LeaseProvisioningStep.GRANTED,
            LeaseProvisioningStep.VERIFIED,
        }:
            return
        else:
            raise RuntimeError("Unsupported provisioning step")

    async def verify(self, resource: DBInstanceResource) -> None:
        if resource.provisioning_step == LeaseProvisioningStep.VERIFIED:
            return
        if resource.provisioning_step != LeaseProvisioningStep.GRANTED:
            raise RuntimeError("Database resource has not completed DDL")
        context, _ddl, _password = await self._forward_ddl(resource)
        username, password = self._resource_credential(resource)
        if not context.instance.host or not context.instance.port:
            raise RuntimeError("Provisioning backend endpoint is incomplete")
        if not resource.database_name:
            raise RuntimeError("Database resource name is unavailable")
        connection = await asyncmy.connect(
            host=context.instance.host,
            port=context.instance.port,
            user=username,
            password=password,
            db=resource.database_name,
            autocommit=True,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                if await cursor.fetchone() != (1,):
                    raise RuntimeError(
                        "Unexpected ordinary-account verification result"
                    )
        finally:
            await connection.ensure_closed()
        resource.provisioning_step = LeaseProvisioningStep.VERIFIED

    async def delete(self, resource: DBInstanceResource) -> None:
        ddl = await self._cleanup_ddl(resource)
        if resource.cleanup_step == LeaseCleanupStep.PENDING:
            await ddl.prepare_cleanup(resource)
            await ddl.drop_database(resource)
            resource.cleanup_step = LeaseCleanupStep.DATABASE_DROPPED
        elif resource.cleanup_step == LeaseCleanupStep.DATABASE_DROPPED:
            await ddl.drop_tenant(resource)
            resource.cleanup_step = LeaseCleanupStep.TENANT_DROPPED
        elif resource.cleanup_step == LeaseCleanupStep.TENANT_DROPPED:
            await ddl.drop_resource_config(resource)
            resource.cleanup_step = LeaseCleanupStep.RESOURCE_CONFIG_DROPPED
        elif resource.cleanup_step == LeaseCleanupStep.RESOURCE_CONFIG_DROPPED:
            if not await ddl.verify_residue_absent(resource):
                raise RuntimeError("Database objects remain after cleanup")
            resource.cleanup_step = LeaseCleanupStep.RESIDUE_VERIFIED
        elif resource.cleanup_step == LeaseCleanupStep.RESIDUE_VERIFIED:
            return
        else:
            raise RuntimeError("Unsupported cleanup step")

    async def health_check(
        self, backend: ProvisioningBackend
    ) -> HealthResult:
        try:
            context = await self._context(backend.id)
            async with self._pool_manager.acquire(
                context.backend,
                context.instance,
                context.credential,
            ) as connection:
                async with connection.cursor() as cursor:
                    await cursor.execute("SELECT 1")
                    if await cursor.fetchone() != (1,):
                        raise RuntimeError("Unexpected health query result")
        except Exception as error:
            return HealthResult(False, type(error).__name__)
        return HealthResult(True)
