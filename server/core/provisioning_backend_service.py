from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.provisioning_adapter import HealthResult
from server.core.super_connection_pool import (
    SuperConnectionPoolManager,
    validate_provisioning_credential,
)
from server.models import (
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningBackendStatus,
)
from server.models.base import utc_now


class BackendNotFound(LookupError):
    pass


class BackendValidationError(ValueError):
    pass


class InvalidBackendTransition(RuntimeError):
    pass


async def bump_backend_config_revision(
    session: AsyncSession,
    backend: ProvisioningBackend,
) -> int:
    """Atomically advance the persisted pool generation."""
    await session.flush()
    await session.execute(
        update(ProvisioningBackend)
        .where(ProvisioningBackend.id == backend.id)
        .values(
            config_revision=ProvisioningBackend.config_revision + 1
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(backend, ["config_revision"])
    return backend.config_revision


def validate_backend_definition(
    backend: ProvisioningBackend,
    instance: Instance,
    credential: InstanceCredential,
) -> None:
    if (
        instance.engine != InstanceEngine.POLARDB_MYSQL
        or instance.topology != InstanceTopology.MULTITENANT
        or instance.status != InstanceStatus.ACTIVE
        or credential.purpose != CredentialPurpose.PROVISIONING_ADMIN
        or credential.capability != CredentialCapability.ADMIN
        or credential.status != CredentialStatus.ACTIVE
    ):
        raise BackendValidationError(
            "Provisioning backends require an active polardb_mysql "
            "multitenant instance and active provisioning admin credential"
        )
    try:
        validate_provisioning_credential(backend, instance, credential)
    except RuntimeError as exc:
        raise BackendValidationError(
            "Provisioning backend credential or endpoint is invalid"
        ) from exc


async def validate_backend_connectivity(
    session: AsyncSession,
    backend: ProvisioningBackend,
) -> HealthResult:
    instance = await session.get(Instance, backend.instance_id)
    credential = await session.get(
        InstanceCredential, backend.admin_credential_id
    )
    if instance is None or credential is None:
        raise BackendValidationError(
            "Provisioning backend instance or credential was not found"
        )
    validate_backend_definition(backend, instance, credential)
    manager = SuperConnectionPoolManager()
    try:
        async with manager.acquire(
            backend, instance, credential
        ) as connection:
            await connection.ping(reconnect=False)
        return HealthResult(True)
    except Exception as exc:
        return HealthResult(False, type(exc).__name__)
    finally:
        await manager.close_all()


async def _context(
    session: AsyncSession,
    *,
    instance_id: str,
    credential_id: str,
) -> tuple[Instance, InstanceCredential]:
    instance = await session.get(Instance, instance_id)
    credential = await session.get(InstanceCredential, credential_id)
    if instance is None:
        raise BackendNotFound("Instance not found")
    if credential is None:
        raise BackendNotFound("Credential not found")
    return instance, credential


async def list_backends(
    session: AsyncSession,
) -> list[ProvisioningBackend]:
    return list(
        (
            await session.execute(
                select(ProvisioningBackend).order_by(
                    ProvisioningBackend.priority.desc(),
                    ProvisioningBackend.id,
                )
            )
        )
        .scalars()
        .all()
    )


async def create_backend(
    session: AsyncSession,
    *,
    instance_id: str,
    admin_credential_id: str,
    priority: int,
    max_active_resources: int,
    resource_min_cpu: int,
    resource_max_cpu: int,
    ddl_concurrency: int,
) -> ProvisioningBackend:
    if resource_min_cpu > resource_max_cpu:
        raise BackendValidationError(
            "resource_min_cpu must not exceed resource_max_cpu"
        )
    instance, credential = await _context(
        session,
        instance_id=instance_id,
        credential_id=admin_credential_id,
    )
    backend = ProvisioningBackend(
        instance_id=instance.id,
        admin_credential_id=credential.id,
        status=ProvisioningBackendStatus.ACTIVE,
        priority=priority,
        max_active_resources=max_active_resources,
        resource_min_cpu=resource_min_cpu,
        resource_max_cpu=resource_max_cpu,
        ddl_concurrency=ddl_concurrency,
        config_revision=1,
    )
    validate_backend_definition(backend, instance, credential)
    session.add(backend)
    await session.flush()
    health_result = await validate_backend_connectivity(session, backend)
    if not health_result.healthy:
        raise BackendValidationError(
            "Provisioning backend connectivity validation failed"
        )
    session.add(
        ProvisioningBackendHealth(
            backend_id=backend.id,
            healthy=True,
            checked_at=utc_now(),
            consecutive_failures=0,
            error_code=None,
        )
    )
    await session.flush()
    return backend


async def update_backend(
    session: AsyncSession,
    backend_id: str,
    changes: Mapping[str, Any],
) -> ProvisioningBackend:
    backend = (
        await session.execute(
            select(ProvisioningBackend)
            .where(ProvisioningBackend.id == backend_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if backend is None:
        raise BackendNotFound("Provisioning backend not found")

    pool_affecting_fields = {
        "admin_credential_id",
        "ddl_concurrency",
    }
    for field in {
        "admin_credential_id",
        "priority",
        "max_active_resources",
        "resource_min_cpu",
        "resource_max_cpu",
        "ddl_concurrency",
        "status",
    }:
        if field in changes:
            setattr(backend, field, changes[field])
    if backend.resource_min_cpu > backend.resource_max_cpu:
        raise BackendValidationError(
            "resource_min_cpu must not exceed resource_max_cpu"
        )
    instance, credential = await _context(
        session,
        instance_id=backend.instance_id,
        credential_id=backend.admin_credential_id,
    )
    validate_backend_definition(backend, instance, credential)
    await session.flush()
    if pool_affecting_fields.intersection(changes):
        await bump_backend_config_revision(session, backend)
    if (
        backend.status == ProvisioningBackendStatus.ACTIVE
        or "admin_credential_id" in changes
    ):
        result = await validate_backend_connectivity(session, backend)
        if not result.healthy:
            raise BackendValidationError(
                "Provisioning backend connectivity validation failed"
            )
        health = await session.get(ProvisioningBackendHealth, backend.id)
        if health is None:
            health = ProvisioningBackendHealth(
                backend_id=backend.id,
                checked_at=utc_now(),
            )
            session.add(health)
        health.healthy = True
        health.checked_at = utc_now()
        health.consecutive_failures = 0
        health.error_code = None
    await session.flush()
    return backend


async def set_backend_status(
    session: AsyncSession,
    backend_id: str,
    status: ProvisioningBackendStatus,
) -> ProvisioningBackend:
    if status == ProvisioningBackendStatus.ACTIVE:
        return await update_backend(
            session, backend_id, {"status": status}
        )
    backend = (
        await session.execute(
            select(ProvisioningBackend)
            .where(ProvisioningBackend.id == backend_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if backend is None:
        raise BackendNotFound("Provisioning backend not found")
    if (
        status == ProvisioningBackendStatus.DRAINING
        and backend.status == ProvisioningBackendStatus.DISABLED
    ):
        raise InvalidBackendTransition(
            "Disabled backend must be activated before it can be drained"
        )
    backend.status = status
    await session.flush()
    return backend
