from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core import admin_binding_service
from server.core.provisioning_backend_repository import (
    backend_is_fresh_and_healthy,
)
from server.core.provisioning_backend_service import (
    BackendValidationError,
    validate_backend_definition,
)
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentProvisioningBinding,
    BindingCapability,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    Permission,
    ProvisioningBackend,
    ProvisioningBackendStatus,
)


class AgentInstanceAccessCapability(str, Enum):
    DB_INSTANCE_LIST = "db_instance:list"
    DB_INSTANCE_DESCRIBE = "db_instance:describe"
    DB_INSTANCE_CREDENTIALS_READ = "db_instance:credentials:read"
    SQL_READ = "sql:read"
    SQL_WRITE = "sql:write"
    DB_INSTANCE_CREATE = "db_instance:create"


class CreateAvailability(str, Enum):
    AVAILABLE = "available"
    BACKEND_REQUIRED = "backend_required"
    BACKEND_INACTIVE = "backend_inactive"
    BACKEND_UNHEALTHY = "backend_unhealthy"
    INSTANCE_INELIGIBLE = "instance_ineligible"


class AgentInstanceAccessError(RuntimeError):
    code = "AGENT_INSTANCE_ACCESS_ERROR"
    status_code = 422


class DirectCredentialRequired(AgentInstanceAccessError):
    code = "DIRECT_CREDENTIAL_REQUIRED"


class InstanceNotMultitenant(AgentInstanceAccessError):
    code = "INSTANCE_NOT_MULTITENANT"


class InstanceNotBindable(AgentInstanceAccessError):
    code = "INSTANCE_NOT_BINDABLE"


class ProvisioningBackendRequired(AgentInstanceAccessError):
    code = "PROVISIONING_BACKEND_REQUIRED"


class ProvisioningBackendUnavailable(AgentInstanceAccessError):
    code = "PROVISIONING_BACKEND_UNAVAILABLE"


class BindingHasResources(AgentInstanceAccessError):
    code = "BINDING_HAS_RESOURCES"
    status_code = 409


@dataclass(frozen=True, slots=True)
class AgentInstanceAccessView:
    agent_id: str
    instance_id: str
    credential_id: str | None
    permission: Permission | None
    direct_enabled: bool | None
    capabilities: tuple[AgentInstanceAccessCapability, ...]
    direct_binding_id: str | None
    provisioning_binding_id: str | None
    provisioning_backend_id: str | None
    create_availability: CreateAvailability


_DIRECT_CAPABILITIES = frozenset(
    capability
    for capability in AgentInstanceAccessCapability
    if capability != AgentInstanceAccessCapability.DB_INSTANCE_CREATE
)
_CAPABILITY_ORDER = {
    capability: index
    for index, capability in enumerate(AgentInstanceAccessCapability)
}


def _is_eligible_instance(instance: Instance) -> bool:
    return (
        instance.engine == InstanceEngine.POLARDB_MYSQL
        and instance.topology == InstanceTopology.MULTITENANT
        and instance.status == InstanceStatus.ACTIVE
    )


def _create_availability(
    instance: Instance,
    backend: ProvisioningBackend | None,
) -> CreateAvailability:
    if not _is_eligible_instance(instance):
        return CreateAvailability.INSTANCE_INELIGIBLE
    if backend is None:
        return CreateAvailability.BACKEND_REQUIRED
    if backend.status != ProvisioningBackendStatus.ACTIVE:
        return CreateAvailability.BACKEND_INACTIVE
    if not backend_is_fresh_and_healthy(backend):
        return CreateAvailability.BACKEND_UNHEALTHY
    return CreateAvailability.AVAILABLE


def _direct_capabilities(
    requested: frozenset[AgentInstanceAccessCapability],
) -> frozenset[BindingCapability]:
    return frozenset(
        BindingCapability(capability.value)
        for capability in requested
        if capability in _DIRECT_CAPABILITIES
    )


def _view(
    *,
    agent_id: str,
    instance: Instance,
    direct: AgentInstanceBinding | None,
    backend: ProvisioningBackend | None,
    provisioning: AgentProvisioningBinding | None,
) -> AgentInstanceAccessView:
    capabilities = {
        AgentInstanceAccessCapability(row.capability.value)
        for row in direct.capabilities
    } if direct is not None else set()
    if provisioning is not None and provisioning.enabled:
        capabilities.add(
            AgentInstanceAccessCapability.DB_INSTANCE_CREATE
        )
    return AgentInstanceAccessView(
        agent_id=agent_id,
        instance_id=instance.id,
        credential_id=(
            direct.credential_id if direct is not None else None
        ),
        permission=direct.permission if direct is not None else None,
        direct_enabled=direct.enabled if direct is not None else None,
        capabilities=tuple(
            sorted(capabilities, key=_CAPABILITY_ORDER.__getitem__)
        ),
        direct_binding_id=direct.id if direct is not None else None,
        provisioning_binding_id=(
            provisioning.id if provisioning is not None else None
        ),
        provisioning_backend_id=(
            backend.id if backend is not None else None
        ),
        create_availability=_create_availability(instance, backend),
    )


async def _instance(
    session: AsyncSession, instance_id: str
) -> Instance:
    instance = await session.get(Instance, instance_id)
    if instance is None:
        raise admin_binding_service.BindingNotFound(
            "Instance not found"
        )
    return instance


async def _require_agent(
    session: AsyncSession, agent_id: str
) -> None:
    if await session.get(Agent, agent_id) is None:
        raise admin_binding_service.BindingNotFound("Agent not found")


async def _backend(
    session: AsyncSession, instance_id: str
) -> ProvisioningBackend | None:
    return (
        await session.execute(
            select(ProvisioningBackend)
            .where(ProvisioningBackend.instance_id == instance_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _direct_binding(
    session: AsyncSession, agent_id: str, instance_id: str
) -> AgentInstanceBinding | None:
    return (
        await session.execute(
            select(AgentInstanceBinding)
            .where(
                AgentInstanceBinding.agent_id == agent_id,
                AgentInstanceBinding.instance_id == instance_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _provisioning_binding(
    session: AsyncSession,
    agent_id: str,
    backend_id: str | None,
) -> AgentProvisioningBinding | None:
    if backend_id is None:
        return None
    return (
        await session.execute(
            select(AgentProvisioningBinding)
            .where(
                AgentProvisioningBinding.agent_id == agent_id,
                AgentProvisioningBinding.backend_id == backend_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()


def _validate_backend_for_create(
    instance: Instance,
    backend: ProvisioningBackend | None,
) -> ProvisioningBackend:
    availability = _create_availability(instance, backend)
    if availability == CreateAvailability.INSTANCE_INELIGIBLE:
        raise InstanceNotMultitenant(
            "Managed database creation requires an active multitenant "
            "PolarDB for MySQL instance"
        )
    if availability == CreateAvailability.BACKEND_REQUIRED:
        raise ProvisioningBackendRequired(
            "Configure a provisioning backend for this instance first"
        )
    if availability != CreateAvailability.AVAILABLE:
        raise ProvisioningBackendUnavailable(
            "Provisioning backend is not active and healthy"
        )
    assert backend is not None
    try:
        validate_backend_definition(
            backend, instance, backend.admin_credential
        )
    except BackendValidationError as exc:
        raise ProvisioningBackendUnavailable(
            "Provisioning backend is not available"
        ) from exc
    return backend


async def list_agent_instance_access(
    session: AsyncSession, agent_id: str
) -> list[AgentInstanceAccessView]:
    await _require_agent(session, agent_id)
    direct_rows = list(
        (
            await session.execute(
                select(AgentInstanceBinding)
                .where(AgentInstanceBinding.agent_id == agent_id)
            )
        ).scalars()
    )
    provisioning_rows = list(
        (
            await session.execute(
                select(AgentProvisioningBinding)
                .where(AgentProvisioningBinding.agent_id == agent_id)
            )
        ).scalars()
    )
    direct_by_instance = {
        row.instance_id: row for row in direct_rows
    }
    provisioning_by_instance = {
        row.backend.instance_id: row for row in provisioning_rows
    }
    instance_ids = sorted(
        set(direct_by_instance) | set(provisioning_by_instance)
    )
    views: list[AgentInstanceAccessView] = []
    for instance_id in instance_ids:
        direct = direct_by_instance.get(instance_id)
        provisioning = provisioning_by_instance.get(instance_id)
        backend = (
            provisioning.backend
            if provisioning is not None
            else await _backend(session, instance_id)
        )
        instance = (
            direct.instance
            if direct is not None
            else backend.instance
        )
        views.append(
            _view(
                agent_id=agent_id,
                instance=instance,
                direct=direct,
                backend=backend,
                provisioning=provisioning,
            )
        )
    return views


async def upsert_agent_instance_access(
    session: AsyncSession,
    *,
    agent_id: str,
    instance_id: str,
    credential_id: str | None,
    permission: Permission | None,
    direct_enabled: bool | None,
    capabilities: Iterable[AgentInstanceAccessCapability],
    admin_id: str,
    require_existing: bool,
) -> AgentInstanceAccessView:
    await _require_agent(session, agent_id)
    instance = await _instance(session, instance_id)
    direct = await _direct_binding(session, agent_id, instance_id)
    backend = await _backend(session, instance_id)
    provisioning = await _provisioning_binding(
        session,
        agent_id,
        backend.id if backend is not None else None,
    )
    if require_existing and direct is None and provisioning is None:
        raise admin_binding_service.BindingNotFound(
            "Agent instance access not found"
        )
    if (
        direct is None
        and provisioning is None
        and instance.status
        not in (InstanceStatus.ACTIVE, InstanceStatus.STOPPED)
    ):
        raise InstanceNotBindable(
            "Instance is not available for binding "
            f"(status: {instance.status.value})"
        )

    requested = frozenset(capabilities)
    create_requested = (
        AgentInstanceAccessCapability.DB_INSTANCE_CREATE in requested
    )
    direct_requested = requested & _DIRECT_CAPABILITIES
    normalized_direct = _direct_capabilities(requested)

    if direct_requested:
        if (
            credential_id is None
            or permission is None
            or direct_enabled is None
        ):
            raise DirectCredentialRequired(
                "Direct capabilities require an active direct-access "
                "credential, permission, and direct_enabled state"
            )
        if direct is None:
            direct = (
                await admin_binding_service
                .create_agent_instance_binding(
                    session,
                    agent_id=agent_id,
                    instance_id=instance_id,
                    credential_id=credential_id,
                    permission=permission,
                    capabilities=normalized_direct,
                    enabled=direct_enabled,
                    admin_id=admin_id,
                )
            )
        else:
            direct = (
                await admin_binding_service
                .update_agent_instance_binding(
                    session,
                    agent_id=agent_id,
                    binding_id=direct.id,
                    credential_id=credential_id,
                    permission=permission,
                    capabilities=normalized_direct,
                    enabled=direct_enabled,
                )
            )
    else:
        if any(
            value is not None
            for value in (
                credential_id,
                permission,
                direct_enabled,
            )
        ):
            raise DirectCredentialRequired(
                "Direct fields require a direct capability"
            )
        if direct is not None:
            await admin_binding_service.delete_agent_instance_binding(
                session,
                agent_id=agent_id,
                binding_id=direct.id,
            )
            direct = None

    if create_requested:
        backend = _validate_backend_for_create(instance, backend)
        if provisioning is None:
            provisioning = (
                await admin_binding_service
                .create_agent_provisioning_binding(
                    session,
                    agent_id=agent_id,
                    backend_id=backend.id,
                    enabled=True,
                    admin_id=admin_id,
                )
            )
        elif not provisioning.enabled:
            provisioning = (
                await admin_binding_service
                .update_agent_provisioning_binding(
                    session,
                    agent_id=agent_id,
                    binding_id=provisioning.id,
                    enabled=True,
                )
            )
    elif provisioning is not None and provisioning.enabled:
        provisioning = (
            await admin_binding_service
            .update_agent_provisioning_binding(
                session,
                agent_id=agent_id,
                binding_id=provisioning.id,
                enabled=False,
            )
        )

    return _view(
        agent_id=agent_id,
        instance=instance,
        direct=direct,
        backend=backend,
        provisioning=provisioning,
    )


async def delete_agent_instance_access(
    session: AsyncSession,
    *,
    agent_id: str,
    instance_id: str,
) -> AgentInstanceAccessView:
    await _require_agent(session, agent_id)
    instance = await _instance(session, instance_id)
    direct = await _direct_binding(session, agent_id, instance_id)
    backend = await _backend(session, instance_id)
    provisioning = await _provisioning_binding(
        session,
        agent_id,
        backend.id if backend is not None else None,
    )
    if direct is None and provisioning is None:
        raise admin_binding_service.BindingNotFound(
            "Agent instance access not found"
        )
    view = _view(
        agent_id=agent_id,
        instance=instance,
        direct=direct,
        backend=backend,
        provisioning=provisioning,
    )
    if provisioning is not None:
        active_resource = await session.scalar(
            select(DBInstanceResource.id)
            .where(
                DBInstanceResource.owner_agent_id == agent_id,
                DBInstanceResource.backend_id
                == provisioning.backend_id,
                DBInstanceResource.status
                != DBInstanceStatus.DELETED,
            )
            .limit(1)
        )
        if active_resource is not None:
            raise BindingHasResources(
                "Agent instance access has non-deleted resources"
            )
        await admin_binding_service.delete_agent_provisioning_binding(
            session,
            agent_id=agent_id,
            binding_id=provisioning.id,
        )
    if direct is not None:
        await admin_binding_service.delete_agent_instance_binding(
            session,
            agent_id=agent_id,
            binding_id=direct.id,
        )
    return view
