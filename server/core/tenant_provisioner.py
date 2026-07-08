from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.crypto import decrypt, encrypt
from server.core.provisioner import generate_db_password
from server.models import DBAccount, Instance, User
from server.models.db_account import AccountType, TenantProvisioningStep

logger = logging.getLogger(__name__)

_STEP_ORDER = list(TenantProvisioningStep)


async def generate_tenant_name(
    session: AsyncSession, instance_id: str, user_id: str
) -> str:
    short_id = user_id.replace("-", "")
    candidate = f"t{short_id[:8]}"
    existing = (await session.execute(
        select(DBAccount.tenant_name).where(
            DBAccount.instance_id == instance_id,
            DBAccount.tenant_name == candidate,
            DBAccount.user_id != user_id,
        )
    )).scalar_one_or_none()
    if existing is None:
        return candidate
    for suffix in range(2, 10):
        candidate = f"t{short_id[:7]}{suffix}"
        existing = (await session.execute(
            select(DBAccount.tenant_name).where(
                DBAccount.instance_id == instance_id,
                DBAccount.tenant_name == candidate,
                DBAccount.user_id != user_id,
            )
        )).scalar_one_or_none()
        if existing is None:
            return candidate
    raise RuntimeError(f"cannot generate unique tenant name for user {user_id}")


async def _get_super_account(
    session: AsyncSession, instance_id: str
) -> DBAccount:
    result = await session.execute(
        select(DBAccount).where(
            DBAccount.instance_id == instance_id,
            DBAccount.account_type == AccountType.SUPER,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise RuntimeError("SUPER account not found for instance")
    return account


async def _connect_as_super(instance: Instance, super_account: DBAccount):
    import asyncmy  # type: ignore[import-untyped]

    password = decrypt(super_account.account_password_enc)
    conn = await asyncmy.connect(
        host=instance.host,
        port=instance.port or 3306,
        user=super_account.account_name,
        password=password,
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
    user: User, instance: Instance, session: AsyncSession
) -> DBAccount:
    result = await session.execute(
        select(DBAccount).where(
            DBAccount.instance_id == instance.id,
            DBAccount.user_id == user.id,
        ).with_for_update()
    )
    account = result.scalar_one_or_none()

    if account is not None and account.provisioning_step is None:
        return account

    if account is None:
        tenant_name = await generate_tenant_name(session, instance.id, user.id)
        password = generate_db_password()
        account = DBAccount(
            instance_id=instance.id,
            user_id=user.id,
            account_name=f"agentic@{tenant_name}",
            account_password_enc=encrypt(password),
            account_type=AccountType.NORMAL,
            tenant_name=tenant_name,
            provisioning_step=TenantProvisioningStep.PENDING,
        )
        session.add(account)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            result = await session.execute(
                select(DBAccount).where(
                    DBAccount.instance_id == instance.id,
                    DBAccount.user_id == user.id,
                ).with_for_update()
            )
            account = result.scalar_one()
            if account.provisioning_step is None:
                return account
        await session.commit()

    if account.provisioning_step is None or account.tenant_name is None:
        raise RuntimeError("ensure_tenant reached step machine with NULL step or tenant_name")

    super_account = await _get_super_account(session, instance.id)
    password = decrypt(account.account_password_enc)
    conn = await _connect_as_super(instance, super_account)

    tenant_name = account.tenant_name
    try:
        start_idx = _STEP_ORDER.index(account.provisioning_step)
        for step in _STEP_ORDER[start_idx:]:
            # Re-acquire row lock to prevent concurrent double-execution
            account = (await session.execute(
                select(DBAccount).where(DBAccount.id == account.id).with_for_update()
            )).scalar_one()
            if account.provisioning_step is None:
                break
            current_idx = _STEP_ORDER.index(account.provisioning_step)
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
                                "step": step.value, "tenant": account.tenant_name,
                            })
                        else:
                            raise
            next_idx = _STEP_ORDER.index(step) + 1
            if next_idx < len(_STEP_ORDER):
                account.provisioning_step = _STEP_ORDER[next_idx]
            else:
                account.provisioning_step = None
            await session.commit()
    finally:
        conn.close()

    logger.info("tenant_provision.completed", extra={
        "metric": "tenant_provision.completed",
        "user_id": user.id, "tenant_name": account.tenant_name,
        "instance_id": instance.id,
    })
    return account
