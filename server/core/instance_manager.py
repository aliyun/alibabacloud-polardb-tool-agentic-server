from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.aliyun.polardb_client import get_polardb_client_async
from server.config import get_config
from server.models import Instance, InstanceType, InstanceStatus

logger = logging.getLogger(__name__)


async def list_instances(session: AsyncSession, instance_type: InstanceType | None = None) -> Sequence[Instance]:
    query = select(Instance).order_by(Instance.created_at.desc())
    if instance_type is not None:
        query = query.where(Instance.type == instance_type)
    result = await session.execute(query)
    return result.scalars().all()


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
    instance_type: InstanceType,
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
        type=instance_type,
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


async def remove_instance(session: AsyncSession, instance_id: str) -> None:
    instance = await get_instance(session, instance_id)
    if instance is None:
        raise ValueError("Instance not found")
    await session.delete(instance)
    await session.commit()


async def discover_instances(session: AsyncSession) -> list[dict]:
    """Discover PolarDB clusters via OpenAPI."""
    config = get_config()
    client = await get_polardb_client_async(session)
    clusters = await client.discover_clusters(config.aliyun.region_id)
    return clusters
