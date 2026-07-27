from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.binding_manager import is_valid_direct_access_credential
from server.core.crypto import decrypt, encrypt
from server.core.provisioner import generate_db_password
from server.models import (
    BindingCapability,
    BindingOrigin,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    Instance,
    InstanceCredential,
    Permission,
    TenantProvisioningStep,
    User,
    UserInstanceBinding,
    UserInstanceBindingCapability,
)

logger = logging.getLogger(__name__)

_STEP_ORDER = list(TenantProvisioningStep)


async def generate_tenant_name(
    session: AsyncSession, instance_id: str, user_id: str
) -> str:
    short_id = user_id.replace("-", "")
    candidate = f"t{short_id[:8]}"
    existing = (await session.execute(
        select(UserInstanceBinding.tenant_name).where(
            UserInstanceBinding.instance_id == instance_id,
            UserInstanceBinding.tenant_name == candidate,
            UserInstanceBinding.user_id != user_id,
        )
    )).scalar_one_or_none()
    if existing is None:
        return candidate
    for suffix in range(2, 10):
        candidate = f"t{short_id[:7]}{suffix}"
        existing = (await session.execute(
            select(UserInstanceBinding.tenant_name).where(
                UserInstanceBinding.instance_id == instance_id,
                UserInstanceBinding.tenant_name == candidate,
                UserInstanceBinding.user_id != user_id,
            )
        )).scalar_one_or_none()
        if existing is None:
            return candidate
    raise RuntimeError(f"cannot generate unique tenant name for user {user_id}")


async def _get_admin_credential(
    session: AsyncSession, instance_id: str
) -> InstanceCredential:
    result = await session.execute(
        select(InstanceCredential).where(
            InstanceCredential.instance_id == instance_id,
            InstanceCredential.purpose
            == CredentialPurpose.PROVISIONING_ADMIN,
            InstanceCredential.capability == CredentialCapability.ADMIN,
            InstanceCredential.status == CredentialStatus.ACTIVE,
        )
    )
    credential = result.scalar_one_or_none()
    if credential is None:
        raise RuntimeError("provisioning admin credential not found for instance")
    return credential


async def _connect_as_super(
    instance: Instance, admin_credential: InstanceCredential
):
    import asyncmy  # type: ignore[import-untyped]

    if (
        admin_credential.username_ciphertext is None
        or admin_credential.password_ciphertext is None
    ):
        raise RuntimeError("provisioning admin credential is unavailable")
    conn = await asyncmy.connect(
        host=instance.host,
        port=instance.port or 3306,
        user=decrypt(admin_credential.username_ciphertext),
        password=decrypt(admin_credential.password_ciphertext),
    )
    return conn


def _build_step_sql(
    step: TenantProvisioningStep, tenant_name: str, password: str
) -> str:
    rc_name = f"rc_{tenant_name}"
    if step == TenantProvisioningStep.PENDING:
        return f"CREATE resource_config {rc_name} min_cpu 0 max_cpu 2"
    elif step == TenantProvisioningStep.RESOURCE_CONFIG:
        return f"CREATE tenant {tenant_name} resource_config {rc_name}"
    elif step == TenantProvisioningStep.TENANT:
        return (
            f"CREATE USER 'agentic@{tenant_name}'@'%' "
            f"IDENTIFIED WITH mysql_native_password BY '{password}'"
        )
    elif step == TenantProvisioningStep.USER:
        return f"CREATE DATABASE IF NOT EXISTS `agentic@{tenant_name}`"
    elif step == TenantProvisioningStep.DATABASE:
        return (
            f"GRANT ALL PRIVILEGES ON `%@{tenant_name}`.* "
            f"TO 'agentic@{tenant_name}'@'%' WITH GRANT OPTION"
        )
    raise ValueError(f"unexpected step: {step}")


async def ensure_tenant(
    user: User,
    instance: Instance,
    session: AsyncSession,
    *,
    permission: Permission = Permission.READWRITE,
    origin: BindingOrigin = BindingOrigin.SYSTEM,
) -> UserInstanceBinding:
    result = await session.execute(
        select(UserInstanceBinding).where(
            UserInstanceBinding.instance_id == instance.id,
            UserInstanceBinding.user_id == user.id,
        ).with_for_update()
    )
    binding = result.scalar_one_or_none()

    if (
        binding is not None
        and binding.provisioning_step is None
        and binding.credential_id is not None
    ):
        return binding

    if binding is None:
        tenant_name = await generate_tenant_name(session, instance.id, user.id)
        password = generate_db_password()
        credential = InstanceCredential(
            instance_id=instance.id,
            name=f"direct-{user.id}",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=(
                CredentialCapability.READONLY
                if permission == Permission.READONLY
                else CredentialCapability.READWRITE
            ),
            username_ciphertext=encrypt(f"agentic@{tenant_name}"),
            password_ciphertext=encrypt(password),
            database_name=f"agentic@{tenant_name}",
            created_by_user_id=user.id,
        )
        session.add(credential)
        await session.flush()
        binding = UserInstanceBinding(
            instance_id=instance.id,
            user_id=user.id,
            credential_id=credential.id,
            permission=permission,
            origin=origin,
            tenant_name=tenant_name,
            provisioning_step=TenantProvisioningStep.PENDING,
        )
        binding.capabilities = [
            UserInstanceBindingCapability(
                capability=BindingCapability.SQL_READ
            )
        ]
        if permission == Permission.READWRITE:
            binding.capabilities.append(
                UserInstanceBindingCapability(
                    capability=BindingCapability.SQL_WRITE
                )
            )
        session.add(binding)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            result = await session.execute(
                select(UserInstanceBinding).where(
                    UserInstanceBinding.instance_id == instance.id,
                    UserInstanceBinding.user_id == user.id,
                ).with_for_update()
            )
            binding = result.scalar_one()
            if binding.provisioning_step is None:
                return binding
        await session.commit()

    if binding.provisioning_step is None or binding.tenant_name is None:
        raise RuntimeError("ensure_tenant reached step machine with NULL step or tenant_name")

    if binding.credential_id is None:
        raise RuntimeError("tenant binding credential is unavailable")
    tenant_credential = await session.get(
        InstanceCredential, binding.credential_id
    )
    if not is_valid_direct_access_credential(
        tenant_credential, instance.id
    ):
        raise RuntimeError("tenant credential is unavailable")
    assert tenant_credential is not None
    password_ciphertext = tenant_credential.password_ciphertext
    if password_ciphertext is None:
        raise RuntimeError("tenant credential is unavailable")
    admin_credential = await _get_admin_credential(session, instance.id)
    password = decrypt(password_ciphertext)
    conn = await _connect_as_super(instance, admin_credential)

    tenant_name = binding.tenant_name
    try:
        start_idx = _STEP_ORDER.index(binding.provisioning_step)
        for step in _STEP_ORDER[start_idx:]:
            # Re-acquire row lock to prevent concurrent double-execution
            binding = (await session.execute(
                select(UserInstanceBinding).where(
                    UserInstanceBinding.id == binding.id
                ).with_for_update()
            )).scalar_one()
            if binding.provisioning_step is None:
                break
            current_idx = _STEP_ORDER.index(binding.provisioning_step)
            if current_idx > _STEP_ORDER.index(step):
                continue

            if step != TenantProvisioningStep.GRANT:
                sql = _build_step_sql(step, tenant_name, password)
                async with conn.cursor() as cur:
                    try:
                        await cur.execute(sql)
                    except Exception as e:
                        err_str = str(e).lower()
                        if "duplicate" in err_str or "already exists" in err_str or "1396" in str(e):
                            logger.info("tenant_provision.idempotent_skip", extra={
                                "step": step.value,
                                "user_id": user.id,
                                "instance_id": instance.id,
                            })
                        else:
                            raise
            next_idx = _STEP_ORDER.index(step) + 1
            if next_idx < len(_STEP_ORDER):
                binding.provisioning_step = _STEP_ORDER[next_idx]
            else:
                binding.provisioning_step = None
            await session.commit()
    finally:
        conn.close()

    logger.info("tenant_provision.completed", extra={
        "metric": "tenant_provision.completed",
        "user_id": user.id,
        "instance_id": instance.id,
    })
    return binding
