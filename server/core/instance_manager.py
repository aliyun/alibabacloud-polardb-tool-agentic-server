from __future__ import annotations

import logging
from typing import Any, Sequence

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.aliyun.polardb_client import get_polardb_client_async
from server.config import get_config
from server.models import (
    AllocationMode,
    AgentInstanceBinding,
    AuditLog,
    DepartmentInstanceBinding,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    CredentialCapability,
    CredentialPurpose,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    User,
    UserInstanceBinding,
)
from server.core.crypto import encrypt

logger = logging.getLogger(__name__)


def instance_category(instance: Instance) -> str:
    """Return the legacy API category derived from the target instance fields."""
    if instance.topology == InstanceTopology.MULTITENANT:
        return "multitenant"
    if (
        instance.allocation_mode == AllocationMode.AUTO_PROVISIONED
        or instance.owner_user_id is not None
    ):
        return "personal"
    return "shared"


async def list_instances(
    session: AsyncSession,
    topology: InstanceTopology | None = None,
    allocation_mode: AllocationMode | None = None,
) -> Sequence[Instance]:
    query = select(Instance).order_by(Instance.created_at.desc())
    if topology is not None:
        query = query.where(Instance.topology == topology)
    if allocation_mode is not None:
        query = query.where(Instance.allocation_mode == allocation_mode)
    result = await session.execute(query)
    return result.scalars().all()


async def list_instance_summaries(
    session: AsyncSession,
    *,
    engine: InstanceEngine | None = None,
    topology: InstanceTopology | None = None,
    allocation_mode: AllocationMode | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    filters = []
    if engine is not None:
        filters.append(Instance.engine == engine)
    if topology is not None:
        filters.append(Instance.topology == topology)
    if allocation_mode is not None:
        filters.append(Instance.allocation_mode == allocation_mode)

    user_count = (
        select(func.count(UserInstanceBinding.id))
        .where(UserInstanceBinding.instance_id == Instance.id)
        .correlate(Instance)
        .scalar_subquery()
    )
    department_count = (
        select(func.count(DepartmentInstanceBinding.id))
        .where(DepartmentInstanceBinding.instance_id == Instance.id)
        .correlate(Instance)
        .scalar_subquery()
    )
    agent_count = (
        select(func.count(AgentInstanceBinding.id))
        .where(AgentInstanceBinding.instance_id == Instance.id)
        .correlate(Instance)
        .scalar_subquery()
    )
    query = (
        select(
            Instance.id,
            Instance.cluster_id,
            Instance.name,
            Instance.usage,
            Instance.engine,
            Instance.topology,
            Instance.allocation_mode,
            Instance.region,
            Instance.host,
            Instance.port,
            Instance.status,
            Instance.owner_user_id,
            ProvisioningBackendHealth.healthy.label("health_healthy"),
            ProvisioningBackendHealth.checked_at.label("health_checked_at"),
            ProvisioningBackendHealth.consecutive_failures.label(
                "health_consecutive_failures"
            ),
            ProvisioningBackendHealth.error_code.label("health_error_code"),
            user_count.label("user_binding_count"),
            department_count.label("department_binding_count"),
            agent_count.label("agent_binding_count"),
        )
        .outerjoin(
            ProvisioningBackend,
            ProvisioningBackend.instance_id == Instance.id,
        )
        .outerjoin(
            ProvisioningBackendHealth,
            ProvisioningBackendHealth.backend_id == ProvisioningBackend.id,
        )
        .where(*filters)
        .order_by(Instance.created_at.desc(), Instance.id.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await session.execute(query)).mappings().all()
    total = (
        await session.execute(
            select(func.count(Instance.id)).where(*filters)
        )
    ).scalar_one()
    return [dict(row) for row in rows], total


async def get_instance(session: AsyncSession, instance_id: str) -> Instance | None:
    result = await session.execute(select(Instance).where(Instance.id == instance_id))
    return result.scalar_one_or_none()


async def get_instance_by_cluster_id(session: AsyncSession, cluster_id: str) -> Instance | None:
    result = await session.execute(select(Instance).where(Instance.cluster_id == cluster_id))
    return result.scalar_one_or_none()


async def register_instance(
    session: AsyncSession,
    cluster_id: str,
    name: str,
    topology: InstanceTopology,
    usage: str | None = None,
    engine: InstanceEngine = InstanceEngine.POLARDB_MYSQL,
    allocation_mode: AllocationMode = AllocationMode.REGISTERED,
    region: str | None = None,
    host: str | None = None,
    port: int | None = None,
    owner_user_id: str | None = None,
) -> Instance:
    existing = await get_instance_by_cluster_id(session, cluster_id)
    if existing:
        raise ValueError(f"Instance with cluster_id {cluster_id} already registered")

    instance = Instance(
        cluster_id=cluster_id,
        name=name,
        usage=usage,
        engine=engine,
        topology=topology,
        allocation_mode=allocation_mode,
        region=region,
        host=host,
        port=port,
        owner_user_id=owner_user_id,
        status=InstanceStatus.ACTIVE,
    )
    session.add(instance)
    await session.commit()
    await session.refresh(instance)
    return instance


async def register_instance_with_credential(
    session: AsyncSession,
    *,
    cluster_id: str,
    name: str,
    usage: str | None,
    engine: InstanceEngine,
    topology: InstanceTopology,
    region: str | None,
    host: str,
    port: int,
    username: str,
    password: str,
    created_by_user_id: str,
) -> Instance:
    existing = await get_instance_by_cluster_id(session, cluster_id)
    if existing:
        raise ValueError(
            f"Instance with cluster_id {cluster_id} already registered"
        )

    instance = Instance(
        cluster_id=cluster_id,
        name=name,
        usage=usage,
        engine=engine,
        topology=topology,
        allocation_mode=AllocationMode.REGISTERED,
        region=region,
        host=host,
        port=port,
        status=InstanceStatus.ACTIVE,
    )
    session.add(instance)
    await session.flush()
    if topology == InstanceTopology.MULTITENANT:
        purpose = CredentialPurpose.PROVISIONING_ADMIN
        capability = CredentialCapability.ADMIN
        credential_name = "provisioning-admin"
    else:
        purpose = CredentialPurpose.DIRECT_ACCESS
        capability = CredentialCapability.READWRITE
        credential_name = "registered-access"
    session.add(
        InstanceCredential(
            instance_id=instance.id,
            name=credential_name,
            purpose=purpose,
            capability=capability,
            username_ciphertext=encrypt(username),
            password_ciphertext=encrypt(password),
            created_by_user_id=created_by_user_id,
        )
    )
    await session.commit()
    await session.refresh(instance)
    return instance


class InstanceRemovalConflict(ValueError):
    pass


async def remove_instance(
    session: AsyncSession,
    instance_id: str,
    *,
    commit: bool = True,
) -> None:
    instance = (
        await session.execute(
            select(Instance)
            .where(Instance.id == instance_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if instance is None:
        raise ValueError("Instance not found")
    if (
        instance.allocation_mode != AllocationMode.REGISTERED
        or instance.owner_user_id is not None
        or instance.status not in {InstanceStatus.ACTIVE, InstanceStatus.STOPPED}
    ):
        raise InstanceRemovalConflict(
            "Only lifecycle-safe registered instances can be removed"
        )

    reference_checks = (
        select(exists().where(UserInstanceBinding.instance_id == instance_id)),
        select(exists().where(DepartmentInstanceBinding.instance_id == instance_id)),
        select(exists().where(AgentInstanceBinding.instance_id == instance_id)),
        select(exists().where(ProvisioningBackend.instance_id == instance_id)),
    )
    for query in reference_checks:
        if (await session.execute(query)).scalar():
            raise InstanceRemovalConflict(
                "Instance cannot be removed while it is referenced"
            )

    try:
        # Backfill legacy audit identity before releasing its nullable FK.
        await session.execute(
            update(AuditLog)
            .where(
                AuditLog.instance_id == instance_id,
                AuditLog.target_type.is_(None),
            )
            .values(target_type="instance")
        )
        await session.execute(
            update(AuditLog)
            .where(
                AuditLog.instance_id == instance_id,
                AuditLog.target_id.is_(None),
            )
            .values(target_id=instance_id)
        )
        await session.execute(
            update(AuditLog)
            .where(AuditLog.instance_id == instance_id)
            .values(instance_id=None)
        )
        await session.execute(
            update(User)
            .where(User.default_instance_id == instance_id)
            .values(default_instance_id=None)
        )
        await session.execute(
            delete(InstanceCredential).where(
                InstanceCredential.instance_id == instance_id
            )
        )
        await session.execute(delete(Instance).where(Instance.id == instance_id))
        if commit:
            await session.commit()
        else:
            await session.flush()
    except IntegrityError:
        await session.rollback()
        raise InstanceRemovalConflict(
            "Instance cannot be removed while it is referenced"
        ) from None


async def discover_instances(session: AsyncSession) -> list[dict]:
    """Discover PolarDB clusters via OpenAPI."""
    config = get_config()
    client = await get_polardb_client_async(session)
    clusters = await client.discover_clusters(config.aliyun.region_id)
    return clusters
