from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.credential_policy import (
    is_valid_direct_access_credential,
)
from server.models import (
    AgentInstanceBinding,
    AgentStatus,
    BindingCapability,
    BindingOrigin,
    CredentialCapability,
    DepartmentInstanceBinding,
    Instance,
    InstanceCredential,
    Permission,
    User,
    UserDepartment,
    UserInstanceBinding,
    UserStatus,
)

_DEPENDENCIES: dict[
    BindingCapability, frozenset[BindingCapability]
] = {
    BindingCapability.DB_INSTANCE_CREDENTIALS_READ: frozenset(
        {
            BindingCapability.DB_INSTANCE_DESCRIBE,
            BindingCapability.DB_INSTANCE_LIST,
        }
    ),
    BindingCapability.DB_INSTANCE_DESCRIBE: frozenset(
        {BindingCapability.DB_INSTANCE_LIST}
    ),
    BindingCapability.SQL_WRITE: frozenset(
        {BindingCapability.SQL_READ}
    ),
}


@dataclass(frozen=True)
class EffectiveInstanceAccess:
    instance: Instance
    binding: UserInstanceBinding | AgentInstanceBinding | None
    capabilities: frozenset[BindingCapability]
    permission: Permission | None
    access_type: str


def validate_capability_set(
    capabilities: Iterable[BindingCapability],
) -> frozenset[BindingCapability]:
    """Return persisted capabilities with their transitive dependencies."""
    provided = tuple(capabilities)
    if any(
        not isinstance(capability, BindingCapability)
        for capability in provided
    ):
        raise ValueError("Unknown binding capability")
    expanded = set(provided)
    pending = list(expanded)
    while pending:
        capability = pending.pop()
        for dependency in _DEPENDENCIES.get(capability, ()):
            if dependency not in expanded:
                expanded.add(dependency)
                pending.append(dependency)
    return frozenset(expanded)


def _sql_capabilities_for_permission(
    permission: Permission,
) -> set[BindingCapability]:
    capabilities = {BindingCapability.SQL_READ}
    if permission == Permission.READWRITE:
        capabilities.add(BindingCapability.SQL_WRITE)
    return capabilities


def _effective_permission(
    requested: Permission,
    capabilities: frozenset[BindingCapability],
    credential: InstanceCredential | None,
    *,
    credential_required: bool,
) -> Permission | None:
    if BindingCapability.SQL_READ not in capabilities:
        return None
    if credential_required and credential is None:
        return None

    permission = (
        Permission.READWRITE
        if (
            requested == Permission.READWRITE
            and BindingCapability.SQL_WRITE in capabilities
        )
        else Permission.READONLY
    )
    if credential is not None:
        if credential.capability == CredentialCapability.READONLY:
            return Permission.READONLY
        if credential.capability != CredentialCapability.READWRITE:
            return None
    return permission


async def _department_access(
    session: AsyncSession,
    user_id: str,
    instance_id: str,
) -> tuple[list[DepartmentInstanceBinding], Permission | None]:
    inherited = (
        await session.execute(
            select(DepartmentInstanceBinding)
            .join(
                UserDepartment,
                UserDepartment.department_id
                == DepartmentInstanceBinding.department_id,
            )
            .where(
                UserDepartment.user_id == user_id,
                DepartmentInstanceBinding.instance_id == instance_id,
            )
        )
    ).scalars().all()
    if not inherited:
        return [], None
    permission = (
        Permission.READONLY
        if any(
            binding.default_permission == Permission.READONLY
            for binding in inherited
        )
        else Permission.READWRITE
    )
    return list(inherited), permission


async def resolve_user_instance_access(
    session: AsyncSession,
    user_id: str,
    instance_id: str,
) -> EffectiveInstanceAccess | None:
    """Resolve explicit User capabilities and inherited SQL-only access."""
    user = await session.get(User, user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        return None

    personal = (
        await session.execute(
            select(UserInstanceBinding).where(
                UserInstanceBinding.instance_id == instance_id,
                UserInstanceBinding.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if personal is not None and (
        not personal.enabled or personal.instance is None
    ):
        return None

    inherited, inherited_permission = await _department_access(
        session, user_id, instance_id
    )

    if personal is None:
        if not inherited or inherited_permission is None:
            return None
        instance = inherited[0].instance
        if instance is None:
            return None
        capabilities = validate_capability_set(
            _sql_capabilities_for_permission(inherited_permission)
        )
        return EffectiveInstanceAccess(
            instance=instance,
            binding=None,
            capabilities=capabilities,
            permission=inherited_permission,
            access_type="department",
        )

    instance = personal.instance
    requested_permission = personal.permission
    access_type = "personal"
    is_owned_system_binding = (
        personal.origin == BindingOrigin.SYSTEM
        and instance.owner_user_id == user_id
    )
    if personal.origin == BindingOrigin.SYSTEM and not is_owned_system_binding:
        if inherited_permission is None:
            return None
        if (
            personal.permission == Permission.READONLY
            or inherited_permission == Permission.READONLY
        ):
            requested_permission = Permission.READONLY
        access_type = "department"

    if personal.origin == BindingOrigin.SYSTEM:
        stored = _sql_capabilities_for_permission(requested_permission)
    else:
        stored = {
            item.capability for item in personal.capabilities
        }
    capabilities = validate_capability_set(stored)

    credential_required = (
        personal.origin == BindingOrigin.ADMIN
        or personal.credential_id is not None
    )
    credential = personal.credential
    if credential_required and not is_valid_direct_access_credential(
        credential, instance_id
    ):
        credential = None
    permission = _effective_permission(
        requested_permission,
        capabilities,
        credential,
        credential_required=credential_required,
    )
    return EffectiveInstanceAccess(
        instance=instance,
        binding=personal,
        capabilities=capabilities,
        permission=permission,
        access_type=access_type,
    )


async def resolve_agent_instance_access(
    session: AsyncSession,
    agent_id: str,
    instance_id: str,
) -> EffectiveInstanceAccess | None:
    """Resolve an Agent's explicit direct-instance binding."""
    binding = (
        await session.execute(
            select(AgentInstanceBinding).where(
                AgentInstanceBinding.agent_id == agent_id,
                AgentInstanceBinding.instance_id == instance_id,
            )
        )
    ).scalar_one_or_none()
    if (
        binding is None
        or not binding.enabled
        or binding.instance is None
        or binding.agent.status != AgentStatus.ACTIVE
    ):
        return None

    capabilities = validate_capability_set(
        {item.capability for item in binding.capabilities}
    )
    credential = (
        binding.credential
        if is_valid_direct_access_credential(
            binding.credential, instance_id
        )
        else None
    )
    permission = _effective_permission(
        binding.permission,
        capabilities,
        credential,
        credential_required=True,
    )
    return EffectiveInstanceAccess(
        instance=binding.instance,
        binding=binding,
        capabilities=capabilities,
        permission=permission,
        access_type="agent",
    )
