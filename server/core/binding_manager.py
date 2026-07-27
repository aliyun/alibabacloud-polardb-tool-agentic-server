from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.aliyun.polardb_client import get_polardb_client_async
from server.core.crypto import encrypt
from server.core.access_control import (
    EffectiveInstanceAccess,
    resolve_user_instance_access as _resolve_user_instance_access,
)
from server.models import (
    BindingCapability,
    BindingOrigin,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DepartmentInstanceBinding,
    Instance,
    InstanceCredential,
    Permission,
    User,
    UserDepartment,
    UserInstanceBinding,
    UserInstanceBindingCapability,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserSQLCredential:
    binding: UserInstanceBinding
    credential: InstanceCredential
    permission: Permission


def _intersect_permission(
    binding_permission: Permission,
    credential_capability: CredentialCapability,
) -> Permission:
    if (
        binding_permission == Permission.READONLY
        or credential_capability == CredentialCapability.READONLY
    ):
        return Permission.READONLY
    return Permission.READWRITE


def is_valid_direct_access_credential(
    credential: InstanceCredential | None,
    instance_id: str,
) -> bool:
    """Return whether a credential is safe for direct user SQL access."""
    return (
        credential is not None
        and credential.instance_id == instance_id
        and credential.resource_id is None
        and credential.purpose == CredentialPurpose.DIRECT_ACCESS
        and credential.status == CredentialStatus.ACTIVE
        and credential.username_ciphertext is not None
        and credential.password_ciphertext is not None
        and credential.capability
        in (
            CredentialCapability.READONLY,
            CredentialCapability.READWRITE,
        )
    )


async def resolve_user_instance_access(
    session: AsyncSession,
    instance_id: str,
    user_id: str,
) -> EffectiveInstanceAccess | None:
    """Compatibility wrapper for the historical argument order."""
    return await _resolve_user_instance_access(
        session, user_id, instance_id
    )


def _generate_password(length: int = 24) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(chars) for _ in range(length))


def _generate_account_name(user_id: str) -> str:
    """Generate a deterministic, MySQL-compliant account name."""
    short_id = user_id.replace("-", "")[:12]
    return f"pas_{short_id}"


async def get_user_credential(
    session: AsyncSession,
    instance_id: str,
    user_id: str,
) -> UserSQLCredential | None:
    access = await resolve_user_instance_access(
        session, instance_id, user_id
    )
    if (
        access is None
        or access.binding is None
        or not isinstance(access.binding, UserInstanceBinding)
        or access.binding.credential_id is None
        or access.permission is None
    ):
        return None
    credential = await session.get(
        InstanceCredential, access.binding.credential_id
    )
    if not is_valid_direct_access_credential(credential, instance_id):
        return None
    assert credential is not None
    return UserSQLCredential(
        binding=access.binding,
        credential=credential,
        permission=_intersect_permission(
            access.permission, credential.capability
        ),
    )


def _sql_capabilities(
    permission: Permission,
) -> list[UserInstanceBindingCapability]:
    capabilities = [
        UserInstanceBindingCapability(capability=BindingCapability.SQL_READ)
    ]
    if permission == Permission.READWRITE:
        capabilities.append(
            UserInstanceBindingCapability(capability=BindingCapability.SQL_WRITE)
        )
    return capabilities


async def _create_instance_credential(
    session: AsyncSession,
    instance: Instance,
    user: User,
    permission: Permission,
    encryption_key: bytes | None = None,
) -> InstanceCredential:
    """Create a DB account for user on instance (via PolarDB OpenAPI)."""
    account_name = _generate_account_name(user.id)
    existing = (
        await session.execute(
            select(InstanceCredential).where(
                InstanceCredential.instance_id == instance.id,
                InstanceCredential.name == account_name,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing

    password = _generate_password()

    # Create account via OpenAPI
    client = await get_polardb_client_async(session)
    await client.create_account(instance.cluster_id, account_name, password)

    # Store encrypted password
    credential = InstanceCredential(
        instance_id=instance.id,
        name=account_name,
        purpose=CredentialPurpose.DIRECT_ACCESS,
        capability=(
            CredentialCapability.READONLY
            if permission == Permission.READONLY
            else CredentialCapability.READWRITE
        ),
        username_ciphertext=encrypt(account_name, key=encryption_key),
        password_ciphertext=encrypt(password, key=encryption_key),
        database_name="agentic",
        created_by_user_id=user.id,
    )
    session.add(credential)
    await session.flush()
    return credential


async def create_db_account(
    session: AsyncSession, instance: Instance, user: User, encryption_key: bytes | None = None
) -> InstanceCredential:
    """Create a DB account for user on instance (public API)."""
    access = await resolve_user_instance_access(
        session, instance.id, user.id
    )
    if access is None or access.permission is None:
        raise PermissionError("User is not authorized for this instance")

    resolved = await get_user_credential(session, instance.id, user.id)
    if resolved is not None:
        return resolved.credential
    if (
        access.binding is not None
        and access.binding.credential_id is not None
    ):
        raise PermissionError("User credential is invalid")

    credential = await _create_instance_credential(
        session, instance, user, access.permission, encryption_key
    )
    binding = access.binding
    if binding is None:
        binding = UserInstanceBinding(
            user_id=user.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=access.permission,
            origin=BindingOrigin.SYSTEM,
        )
        binding.capabilities = _sql_capabilities(binding.permission)
        session.add(binding)
    else:
        binding.credential_id = credential.id
    await session.flush()
    return credential


async def bind_user_to_instance(
    session: AsyncSession,
    user_id: str,
    instance_id: str,
    permission: Permission = Permission.READWRITE,
    encryption_key: bytes | None = None,
) -> UserInstanceBinding:
    """Bind user to instance and auto-create DB account."""
    # Check existing binding
    existing = (await session.execute(
        select(UserInstanceBinding).where(
            UserInstanceBinding.user_id == user_id,
            UserInstanceBinding.instance_id == instance_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise ValueError("User already bound to this instance")

    instance = (await session.execute(
        select(Instance).where(Instance.id == instance_id)
    )).scalar_one_or_none()
    if instance is None:
        raise ValueError("Instance not found")

    user = (await session.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()
    if user is None:
        raise ValueError("User not found")

    # Create DB account
    credential = await _create_instance_credential(
        session, instance, user, permission, encryption_key
    )

    binding = UserInstanceBinding(
        user_id=user_id,
        instance_id=instance_id,
        credential_id=credential.id,
        permission=permission,
        origin=BindingOrigin.ADMIN,
    )
    binding.capabilities = _sql_capabilities(permission)
    session.add(binding)
    await session.commit()
    await session.refresh(binding)
    return binding


async def unbind_user_from_instance(session: AsyncSession, user_id: str, instance_id: str) -> None:
    binding = (await session.execute(
        select(UserInstanceBinding).where(
            UserInstanceBinding.user_id == user_id,
            UserInstanceBinding.instance_id == instance_id,
        )
    )).scalar_one_or_none()
    if binding is None:
        raise ValueError("Binding not found")
    await session.delete(binding)
    await session.commit()


async def bind_department_to_instance(
    session: AsyncSession,
    department_id: str,
    instance_id: str,
    tenant_name: str | None = None,
    default_permission: Permission = Permission.READWRITE,
) -> DepartmentInstanceBinding:
    existing = (await session.execute(
        select(DepartmentInstanceBinding).where(
            DepartmentInstanceBinding.department_id == department_id,
            DepartmentInstanceBinding.instance_id == instance_id,
        )
    )).scalar_one_or_none()
    if existing:
        raise ValueError("Department already bound to this instance")

    binding = DepartmentInstanceBinding(
        department_id=department_id,
        instance_id=instance_id,
        tenant_name=tenant_name,
        default_permission=default_permission,
    )
    session.add(binding)
    await session.commit()
    await session.refresh(binding)
    return binding


async def unbind_department_from_instance(session: AsyncSession, department_id: str, instance_id: str) -> None:
    binding = (await session.execute(
        select(DepartmentInstanceBinding).where(
            DepartmentInstanceBinding.department_id == department_id,
            DepartmentInstanceBinding.instance_id == instance_id,
        )
    )).scalar_one_or_none()
    if binding is None:
        raise ValueError("Binding not found")
    await session.delete(binding)
    await session.commit()


async def get_accessible_instances(session: AsyncSession, user: User) -> list[dict]:
    """Get all instances accessible to a user (personal + department inherited)."""
    personal_bindings = (await session.execute(
        select(UserInstanceBinding).where(UserInstanceBinding.user_id == user.id)
    )).scalars().all()

    memberships = (await session.execute(
        select(UserDepartment).where(UserDepartment.user_id == user.id)
    )).scalars().all()

    dept_ids = [m.department_id for m in memberships]
    dept_bindings: Sequence[DepartmentInstanceBinding] = []
    if dept_ids:
        dept_bindings = (await session.execute(
            select(DepartmentInstanceBinding)
            .where(DepartmentInstanceBinding.department_id.in_(dept_ids))
        )).scalars().all()

    candidate_ids = list(
        dict.fromkeys(
            [
                *(binding.instance_id for binding in personal_bindings),
                *(binding.instance_id for binding in dept_bindings),
            ]
        )
    )
    result = []
    for instance_id in candidate_ids:
        access = await resolve_user_instance_access(
            session, instance_id, user.id
        )
        if access is not None and access.permission is not None:
            result.append(
                {
                    "instance": access.instance,
                    "access_type": access.access_type,
                    "permission": access.permission.value,
                }
            )
    return result
