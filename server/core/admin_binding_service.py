from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.access_control import validate_capability_set
from server.core.credential_policy import (
    is_valid_direct_access_credential,
)
from server.core.provisioning_backend_service import (
    BackendValidationError,
    validate_backend_definition,
)
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentInstanceBindingCapability,
    AgentProvisioningBinding,
    BindingCapability,
    BindingOrigin,
    CredentialCapability,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceStatus,
    Permission,
    ProvisioningBackend,
    ProvisioningBackendStatus,
    User,
    UserInstanceBinding,
    UserInstanceBindingCapability,
)


class BindingNotFound(LookupError):
    pass


class BindingValidationError(ValueError):
    pass


class BindingConflict(RuntimeError):
    pass


_DIRECT_BINDING_CAPABILITIES = frozenset(
    {
        BindingCapability.DB_INSTANCE_LIST,
        BindingCapability.DB_INSTANCE_DESCRIBE,
        BindingCapability.DB_INSTANCE_CREDENTIALS_READ,
    }
)

_AGENT_DIRECT_BINDING_CAPABILITIES = frozenset(
    {
        *_DIRECT_BINDING_CAPABILITIES,
        BindingCapability.SQL_READ,
        BindingCapability.SQL_WRITE,
    }
)


async def _require_agent(session: AsyncSession, agent_id: str) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise BindingNotFound("Agent not found")
    return agent


async def _require_user(session: AsyncSession, user_id: str) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise BindingNotFound("User not found")
    return user


async def _direct_context(
    session: AsyncSession,
    *,
    instance_id: str,
    credential_id: str,
    permission: Permission,
) -> tuple[Instance, InstanceCredential]:
    instance = await session.get(Instance, instance_id)
    credential = await session.get(InstanceCredential, credential_id)
    if instance is None:
        raise BindingNotFound("Instance not found")
    if credential is None:
        raise BindingNotFound("Credential not found")
    if not is_valid_direct_access_credential(credential, instance_id):
        raise BindingValidationError("Credential must be an active direct-access credential for the selected instance")
    if permission == Permission.READWRITE and credential.capability != CredentialCapability.READWRITE:
        raise BindingValidationError("Requested permission exceeds credential capability")
    return instance, credential


def _normalized_capabilities(
    capabilities: Iterable[BindingCapability],
    *,
    allowed: frozenset[BindingCapability],
) -> frozenset[BindingCapability]:
    expanded = validate_capability_set(capabilities)
    if not expanded.issubset(allowed):
        raise BindingValidationError("Capability is not allowed for this binding")
    return expanded


def _normalized_agent_capabilities(
    capabilities: Iterable[BindingCapability],
    *,
    permission: Permission,
) -> frozenset[BindingCapability]:
    normalized = _normalized_capabilities(
        capabilities,
        allowed=_AGENT_DIRECT_BINDING_CAPABILITIES,
    )
    if (
        BindingCapability.SQL_WRITE in normalized
        and permission != Permission.READWRITE
    ):
        raise BindingValidationError(
            "sql:write requires readwrite binding permission"
        )
    return normalized


def _agent_capability_rows(
    capabilities: Iterable[BindingCapability],
) -> list[AgentInstanceBindingCapability]:
    return [
        AgentInstanceBindingCapability(capability=capability)
        for capability in sorted(capabilities, key=lambda item: item.value)
    ]


def _user_capability_rows(
    capabilities: Iterable[BindingCapability],
) -> list[UserInstanceBindingCapability]:
    return [
        UserInstanceBindingCapability(capability=capability)
        for capability in sorted(capabilities, key=lambda item: item.value)
    ]


async def list_agent_instance_bindings(session: AsyncSession, agent_id: str) -> list[AgentInstanceBinding]:
    await _require_agent(session, agent_id)
    return list(
        (
            await session.execute(
                select(AgentInstanceBinding)
                .where(AgentInstanceBinding.agent_id == agent_id)
                .order_by(AgentInstanceBinding.created_at, AgentInstanceBinding.id)
            )
        )
        .scalars()
        .all()
    )


async def create_agent_instance_binding(
    session: AsyncSession,
    *,
    agent_id: str,
    instance_id: str,
    credential_id: str,
    permission: Permission,
    capabilities: Iterable[BindingCapability],
    enabled: bool,
    admin_id: str,
) -> AgentInstanceBinding:
    await _require_agent(session, agent_id)
    await _direct_context(
        session,
        instance_id=instance_id,
        credential_id=credential_id,
        permission=permission,
    )
    normalized = _normalized_agent_capabilities(
        capabilities,
        permission=permission,
    )
    binding = AgentInstanceBinding(
        agent_id=agent_id,
        instance_id=instance_id,
        credential_id=credential_id,
        permission=permission,
        enabled=enabled,
        created_by_user_id=admin_id,
    )
    binding.capabilities = _agent_capability_rows(normalized)
    session.add(binding)
    await session.flush()
    return binding


async def _agent_instance_binding(session: AsyncSession, agent_id: str, binding_id: str) -> AgentInstanceBinding:
    binding = (
        await session.execute(
            select(AgentInstanceBinding)
            .where(
                AgentInstanceBinding.id == binding_id,
                AgentInstanceBinding.agent_id == agent_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if binding is None:
        raise BindingNotFound("Agent instance binding not found")
    return binding


async def update_agent_instance_binding(
    session: AsyncSession,
    *,
    agent_id: str,
    binding_id: str,
    credential_id: str,
    permission: Permission,
    capabilities: Iterable[BindingCapability],
    enabled: bool,
) -> AgentInstanceBinding:
    binding = await _agent_instance_binding(session, agent_id, binding_id)
    await _direct_context(
        session,
        instance_id=binding.instance_id,
        credential_id=credential_id,
        permission=permission,
    )
    normalized = _normalized_agent_capabilities(
        capabilities,
        permission=permission,
    )
    binding.credential_id = credential_id
    binding.permission = permission
    binding.enabled = enabled
    binding.capabilities = _agent_capability_rows(normalized)
    await session.flush()
    return binding


async def delete_agent_instance_binding(
    session: AsyncSession, *, agent_id: str, binding_id: str
) -> AgentInstanceBinding:
    binding = await _agent_instance_binding(session, agent_id, binding_id)
    await session.delete(binding)
    await session.flush()
    return binding


async def _validate_backend(
    session: AsyncSession,
    backend_id: str,
    *,
    allow_create: bool,
) -> ProvisioningBackend:
    backend = await session.get(ProvisioningBackend, backend_id)
    if backend is None:
        raise BindingNotFound("Provisioning backend not found")
    if allow_create and backend.status != ProvisioningBackendStatus.ACTIVE:
        raise BindingValidationError("Provisioning backend must be active to allow creation")
    instance = backend.instance
    credential = backend.admin_credential
    try:
        validate_backend_definition(backend, instance, credential)
    except BackendValidationError as exc:
        raise BindingValidationError(str(exc)) from exc
    return backend


async def list_agent_provisioning_bindings(session: AsyncSession, agent_id: str) -> list[AgentProvisioningBinding]:
    await _require_agent(session, agent_id)
    return list(
        (
            await session.execute(
                select(AgentProvisioningBinding)
                .where(AgentProvisioningBinding.agent_id == agent_id)
                .order_by(
                    AgentProvisioningBinding.created_at,
                    AgentProvisioningBinding.id,
                )
            )
        )
        .scalars()
        .all()
    )


async def create_agent_provisioning_binding(
    session: AsyncSession,
    *,
    agent_id: str,
    backend_id: str,
    enabled: bool,
    admin_id: str,
) -> AgentProvisioningBinding:
    await _require_agent(session, agent_id)
    backend = await _validate_backend(session, backend_id, allow_create=enabled)
    binding = AgentProvisioningBinding(
        agent_id=agent_id,
        backend=backend,
        enabled=enabled,
        created_by_user_id=admin_id,
    )
    session.add(binding)
    await session.flush()
    return binding


async def _agent_provisioning_binding(
    session: AsyncSession, agent_id: str, binding_id: str
) -> AgentProvisioningBinding:
    binding = (
        await session.execute(
            select(AgentProvisioningBinding)
            .where(
                AgentProvisioningBinding.id == binding_id,
                AgentProvisioningBinding.agent_id == agent_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if binding is None:
        raise BindingNotFound("Agent provisioning binding not found")
    return binding


async def update_agent_provisioning_binding(
    session: AsyncSession,
    *,
    agent_id: str,
    binding_id: str,
    enabled: bool,
) -> AgentProvisioningBinding:
    binding = await _agent_provisioning_binding(session, agent_id, binding_id)
    if enabled:
        await _validate_backend(session, binding.backend_id, allow_create=True)
    binding.enabled = enabled
    await session.flush()
    return binding


async def delete_agent_provisioning_binding(
    session: AsyncSession, *, agent_id: str, binding_id: str
) -> AgentProvisioningBinding:
    binding = await _agent_provisioning_binding(session, agent_id, binding_id)
    active_resource_id = await session.scalar(
        select(DBInstanceResource.id)
        .where(
            DBInstanceResource.owner_agent_id == agent_id,
            DBInstanceResource.backend_id == binding.backend_id,
            DBInstanceResource.status != DBInstanceStatus.DELETED,
        )
        .limit(1)
    )
    if active_resource_id is not None:
        raise BindingConflict("Provisioning binding has non-deleted Agent resources")
    await session.delete(binding)
    await session.flush()
    return binding


async def list_agent_resources(session: AsyncSession, agent_id: str) -> list[DBInstanceResource]:
    await _require_agent(session, agent_id)
    return list(
        (
            await session.execute(
                select(DBInstanceResource)
                .where(
                    DBInstanceResource.owner_agent_id == agent_id,
                    DBInstanceResource.status != DBInstanceStatus.DELETED,
                )
                .order_by(
                    DBInstanceResource.created_at,
                    DBInstanceResource.id,
                )
            )
        )
        .scalars()
        .all()
    )


async def get_user_instance_access(session: AsyncSession, *, user_id: str, instance_id: str) -> UserInstanceBinding:
    await _require_user(session, user_id)
    if await session.get(Instance, instance_id) is None:
        raise BindingNotFound("Instance not found")
    binding = (
        await session.execute(
            select(UserInstanceBinding).where(
                UserInstanceBinding.user_id == user_id,
                UserInstanceBinding.instance_id == instance_id,
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        raise BindingNotFound("User instance access not found")
    return binding


async def update_user_instance_access(
    session: AsyncSession,
    *,
    user_id: str,
    instance_id: str,
    credential_id: str,
    permission: Permission,
    capabilities: Iterable[BindingCapability],
    enabled: bool,
) -> tuple[UserInstanceBinding, bool]:
    await _require_user(session, user_id)
    instance, _credential = await _direct_context(
        session,
        instance_id=instance_id,
        credential_id=credential_id,
        permission=permission,
    )
    normalized = validate_capability_set(capabilities)
    if permission != Permission.READWRITE and BindingCapability.SQL_WRITE in normalized:
        raise BindingValidationError("sql:write requires readwrite binding permission")
    existing = (
        await session.execute(
            select(UserInstanceBinding)
            .where(
                UserInstanceBinding.user_id == user_id,
                UserInstanceBinding.instance_id == instance_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    created = existing is None
    if created and instance.status not in (
        InstanceStatus.ACTIVE,
        InstanceStatus.STOPPED,
    ):
        raise BindingValidationError(
            "Instance is not available for binding "
            f"(status: {instance.status.value})"
        )
    if existing is None:
        existing = UserInstanceBinding(
            user_id=user_id,
            instance_id=instance_id,
            origin=BindingOrigin.ADMIN,
        )
        session.add(existing)
    if existing.origin == BindingOrigin.SYSTEM:
        sql = (
            {
                BindingCapability.SQL_READ,
                BindingCapability.SQL_WRITE,
            }
            if permission == Permission.READWRITE
            else {BindingCapability.SQL_READ}
        )
        normalized = frozenset(normalized | sql)
        existing.origin = BindingOrigin.ADMIN
    existing.credential_id = credential_id
    existing.permission = permission
    existing.enabled = enabled
    existing.capabilities = _user_capability_rows(normalized)
    await session.flush()
    return existing, created
