from __future__ import annotations

import logging
import secrets
import string

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.aliyun.polardb_client import get_polardb_client_async
from server.core.crypto import encrypt
from server.models import (
    Instance, UserInstanceBinding, DepartmentInstanceBinding, UserDepartment,
    DBAccount, Permission, AccountType, User,
)

logger = logging.getLogger(__name__)


def _generate_password(length: int = 24) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(chars) for _ in range(length))


def _generate_account_name(user_id: str) -> str:
    """Generate a deterministic, MySQL-compliant account name."""
    short_id = user_id.replace("-", "")[:12]
    return f"pas_{short_id}"


async def _create_db_account(
    session: AsyncSession, instance: Instance, user: User, encryption_key: bytes | None = None
) -> DBAccount:
    """Create a DB account for user on instance (via PolarDB OpenAPI)."""
    # Check if account already exists
    existing = (await session.execute(
        select(DBAccount).where(
            DBAccount.instance_id == instance.id,
            DBAccount.user_id == user.id,
        )
    )).scalar_one_or_none()
    if existing:
        return existing

    account_name = _generate_account_name(user.id)
    password = _generate_password()

    # Create account via OpenAPI
    client = await get_polardb_client_async(session)
    await client.create_account(instance.cluster_id, account_name, password)

    # Store encrypted password
    encrypted_pw = encrypt(password, key=encryption_key)

    db_account = DBAccount(
        instance_id=instance.id,
        user_id=user.id,
        account_name=account_name,
        account_password_enc=encrypted_pw,
        account_type=AccountType.NORMAL,
    )
    session.add(db_account)
    await session.flush()
    return db_account


async def create_db_account(
    session: AsyncSession, instance: Instance, user: User, encryption_key: bytes | None = None
) -> DBAccount:
    """Create a DB account for user on instance (public API)."""
    return await _create_db_account(session, instance, user, encryption_key)


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
    db_account = await _create_db_account(session, instance, user, encryption_key)

    binding = UserInstanceBinding(
        user_id=user_id,
        instance_id=instance_id,
        db_account_id=db_account.id,
        permission=permission,
    )
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
    result: dict[str, dict] = {}

    personal_bindings = (await session.execute(
        select(UserInstanceBinding).where(UserInstanceBinding.user_id == user.id)
    )).scalars().all()

    for binding in personal_bindings:
        inst = binding.instance
        if inst:
            result[inst.id] = {
                "instance": inst,
                "access_type": "personal",
                "permission": binding.permission.value,
            }

    memberships = (await session.execute(
        select(UserDepartment).where(UserDepartment.user_id == user.id)
    )).scalars().all()

    dept_ids = [m.department_id for m in memberships]
    if dept_ids:
        dept_bindings = (await session.execute(
            select(DepartmentInstanceBinding)
            .where(DepartmentInstanceBinding.department_id.in_(dept_ids))
        )).scalars().all()

        for dept_binding in dept_bindings:
            if dept_binding.instance_id not in result:
                inst = dept_binding.instance
                if inst:
                    result[inst.id] = {
                        "instance": inst,
                        "access_type": "department",
                        "permission": dept_binding.default_permission.value,
                    }

    return list(result.values())
