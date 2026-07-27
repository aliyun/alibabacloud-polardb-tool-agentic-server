from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.core import instance_connection
from server.core.crypto import decrypt, encrypt
from server.models import (
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    SecretRevealLimit,
)
from server.models.base import utc_now

REVEAL_LIMIT = 5
REVEAL_WINDOW = timedelta(minutes=1)
REVEAL_STATE_RETENTION = timedelta(days=1)


class CredentialValidationError(ValueError):
    pass


class CredentialNotFound(LookupError):
    pass


class CredentialUnavailable(RuntimeError):
    pass


class CredentialVersionConflict(RuntimeError):
    pass


class RevealRateLimitExceeded(RuntimeError):
    pass


def validate_credential_definition(
    instance: Instance,
    *,
    purpose: CredentialPurpose,
    capability: CredentialCapability,
    database_name: str | None,
) -> None:
    if instance.status != InstanceStatus.ACTIVE:
        raise CredentialValidationError(
            "Credentials require an active instance"
        )
    if purpose == CredentialPurpose.PROVISIONING_ADMIN:
        if (
            instance.engine != InstanceEngine.POLARDB_MYSQL
            or instance.topology != InstanceTopology.MULTITENANT
            or capability != CredentialCapability.ADMIN
            or database_name is not None
        ):
            raise CredentialValidationError(
                "Provisioning admin credentials require an active "
                "polardb_mysql multitenant instance, admin capability, "
                "and no database name"
            )
        return
    if purpose == CredentialPurpose.DIRECT_ACCESS:
        if capability not in {
            CredentialCapability.READONLY,
            CredentialCapability.READWRITE,
        }:
            raise CredentialValidationError(
                "Direct access credentials require readonly or readwrite "
                "capability"
            )
        return
    raise CredentialValidationError(
        "Resource credentials cannot be created through the admin API"
    )


async def list_instance_credentials(
    session: AsyncSession, instance_id: str
) -> list[InstanceCredential]:
    if await session.get(Instance, instance_id) is None:
        raise CredentialNotFound("Instance not found")
    return list(
        (
            await session.execute(
                select(InstanceCredential)
                .where(InstanceCredential.instance_id == instance_id)
                .order_by(
                    InstanceCredential.created_at,
                    InstanceCredential.id,
                )
            )
        )
        .scalars()
        .all()
    )


async def create_instance_credential(
    session: AsyncSession,
    *,
    instance_id: str,
    name: str,
    purpose: CredentialPurpose,
    capability: CredentialCapability,
    username: str,
    password: str,
    database_name: str | None,
    created_by_user_id: str,
) -> InstanceCredential:
    instance = await session.get(Instance, instance_id)
    if instance is None:
        raise CredentialNotFound("Instance not found")
    validate_credential_definition(
        instance,
        purpose=purpose,
        capability=capability,
        database_name=database_name,
    )
    await test_credential_connection(
        instance,
        purpose=purpose,
        capability=capability,
        username=username,
        password=password,
        database_name=database_name,
    )
    credential = InstanceCredential(
        instance_id=instance.id,
        resource_id=None,
        name=name,
        purpose=purpose,
        capability=capability,
        username_ciphertext=encrypt(username),
        password_ciphertext=encrypt(password),
        database_name=database_name,
        status=CredentialStatus.ACTIVE,
        version=1,
        created_by_user_id=created_by_user_id,
    )
    session.add(credential)
    await session.flush()
    return credential


async def test_credential_connection(
    instance: Instance,
    *,
    purpose: CredentialPurpose,
    capability: CredentialCapability,
    username: str | None,
    password: str,
    database_name: str | None,
    host: str | None = None,
    port: int | None = None,
) -> None:
    validate_credential_definition(
        instance,
        purpose=purpose,
        capability=capability,
        database_name=database_name,
    )
    effective_host = host if host is not None else instance.host
    effective_port = port if port is not None else instance.port
    if effective_host is None or effective_port is None:
        raise CredentialValidationError(
            "Credential connection tests require an instance endpoint"
        )
    await instance_connection.test_mysql_connection(
        host=effective_host,
        port=effective_port,
        username=username,
        password=password,
        database=database_name,
        require_multitenant=(
            purpose == CredentialPurpose.PROVISIONING_ADMIN
        ),
    )


async def update_instance_credential(
    session: AsyncSession,
    *,
    credential_id: str,
    expected_version: int,
    name: str,
    capability: CredentialCapability,
    username: str,
    password: str | None,
    database_name: str | None,
) -> InstanceCredential:
    credential = (
        await session.execute(
            select(InstanceCredential)
            .where(InstanceCredential.id == credential_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if credential is None:
        raise CredentialNotFound("Credential not found")
    if (
        credential.status != CredentialStatus.ACTIVE
        or credential.instance_id is None
        or credential.username_ciphertext is None
        or credential.password_ciphertext is None
    ):
        raise CredentialUnavailable("Credential is not active")
    if credential.version != expected_version:
        raise CredentialVersionConflict(
            "Credential was changed by another administrator"
        )
    instance = await session.get(Instance, credential.instance_id)
    if instance is None:
        raise CredentialNotFound("Instance not found")
    effective_password = (
        password
        if password is not None
        else decrypt(credential.password_ciphertext)
    )
    effective_username = (
        username
        if username is not None
        else decrypt(credential.username_ciphertext)
    )
    await test_credential_connection(
        instance,
        purpose=credential.purpose,
        capability=capability,
        username=effective_username,
        password=effective_password,
        database_name=database_name,
    )
    credential.name = name
    credential.capability = capability
    credential.username_ciphertext = encrypt(effective_username)
    credential.password_ciphertext = encrypt(effective_password)
    credential.database_name = database_name
    credential.version += 1
    await session.flush()
    return credential


async def test_stored_credential_connection(
    session: AsyncSession,
    *,
    instance: Instance,
    credential_id: str,
    host: str,
    port: int,
    required_credential_id: str | None = None,
) -> None:
    credential = await session.get(InstanceCredential, credential_id)
    if (
        credential is None
        or credential.instance_id != instance.id
        or credential.status != CredentialStatus.ACTIVE
        or credential.username_ciphertext is None
        or credential.password_ciphertext is None
        or (
            required_credential_id is not None
            and credential.id != required_credential_id
        )
    ):
        raise CredentialValidationError(
            "The selected test credential is not valid for this instance"
        )
    await test_credential_connection(
        instance,
        purpose=credential.purpose,
        capability=credential.capability,
        username=decrypt(credential.username_ciphertext),
        password=decrypt(credential.password_ciphertext),
        database_name=credential.database_name,
        host=host,
        port=port,
    )


async def consume_reveal_budget(
    session: AsyncSession,
    admin_id: str,
    target_kind: str,
    target_id: str,
    *,
    now: datetime | None = None,
) -> None:
    current_time = now or utc_now()
    window_cutoff = current_time - REVEAL_WINDOW
    retention_cutoff = current_time - REVEAL_STATE_RETENTION
    key = (admin_id, target_kind, target_id)
    await session.execute(
        delete(SecretRevealLimit).where(
            SecretRevealLimit.window_started_at < retention_cutoff
        )
    )

    for _ in range(3):
        current_window = cast(
            CursorResult[Any],
            await session.execute(
                update(SecretRevealLimit)
                .where(
                    SecretRevealLimit.admin_id == admin_id,
                    SecretRevealLimit.target_kind == target_kind,
                    SecretRevealLimit.target_id == target_id,
                    SecretRevealLimit.window_started_at > window_cutoff,
                    SecretRevealLimit.request_count < REVEAL_LIMIT,
                )
                .values(
                    request_count=SecretRevealLimit.request_count + 1
                )
            ),
        )
        if current_window.rowcount == 1:
            return

        reset_window = cast(
            CursorResult[Any],
            await session.execute(
                update(SecretRevealLimit)
                .where(
                    SecretRevealLimit.admin_id == admin_id,
                    SecretRevealLimit.target_kind == target_kind,
                    SecretRevealLimit.target_id == target_id,
                    SecretRevealLimit.window_started_at <= window_cutoff,
                )
                .values(
                    window_started_at=current_time,
                    request_count=1,
                )
            ),
        )
        if reset_window.rowcount == 1:
            return

        if await session.get(SecretRevealLimit, key) is not None:
            raise RevealRateLimitExceeded
        try:
            async with session.begin_nested():
                session.add(
                    SecretRevealLimit(
                        admin_id=admin_id,
                        target_kind=target_kind,
                        target_id=target_id,
                        window_started_at=current_time,
                        request_count=1,
                    )
                )
                await session.flush()
            return
        except IntegrityError:
            continue
    raise RevealRateLimitExceeded


async def reveal_credential(
    session: AsyncSession,
    credential_id: str,
    *,
    admin_id: str,
) -> tuple[str, str, str | None]:
    credential = (
        await session.execute(
            select(InstanceCredential)
            .where(InstanceCredential.id == credential_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if credential is None:
        raise CredentialNotFound("Credential not found")
    if (
        credential.status != CredentialStatus.ACTIVE
        or not credential.username_ciphertext
        or not credential.password_ciphertext
    ):
        raise CredentialUnavailable("Credential is not active")
    await consume_reveal_budget(
        session,
        admin_id,
        "credential",
        credential.id,
    )
    try:
        username = decrypt(credential.username_ciphertext)
        password = decrypt(credential.password_ciphertext)
    except Exception as exc:
        raise CredentialUnavailable(
            "Credential cannot be decrypted"
        ) from exc
    return username, password, credential.database_name


async def revoke_credential(
    session: AsyncSession, credential_id: str
) -> InstanceCredential:
    credential = (
        await session.execute(
            select(InstanceCredential)
            .where(InstanceCredential.id == credential_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if credential is None:
        raise CredentialNotFound("Credential not found")
    credential.status = CredentialStatus.REVOKED
    credential.username_ciphertext = None
    credential.password_ciphertext = None
    await session.flush()
    return credential
