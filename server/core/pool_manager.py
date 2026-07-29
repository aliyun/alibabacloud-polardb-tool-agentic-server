# server/core/pool_manager.py
from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import get_config
from server.core.quota_manager import check_and_increment_quota
from server.models import (
    AllocationMode,
    Instance,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
)
from server.models.instance import ProvisioningStep

logger = logging.getLogger(__name__)


class _QuotaExceededError(Exception):
    """Internal exception to roll back a savepoint on quota failure."""

    def __init__(self, detail: dict):
        self.detail = detail


async def allocate_from_pool(
    user_id: str, department_id: str | None, session: AsyncSession,
) -> Instance | dict:
    """Claim a POOLED instance for a user, or fall back to creating one.

    Returns an Instance on success, or a dict with ``"error"`` on quota failure.
    Uses a savepoint so that quota check failure does not invalidate the
    caller's session state.
    """
    try:
        async with session.begin_nested():
            quota_error = await check_and_increment_quota(session, department_id)
            if quota_error:
                raise _QuotaExceededError(quota_error)
    except _QuotaExceededError as exc:
        return exc.detail

    row = await session.execute(
        select(Instance)
        .where(
            Instance.allocation_mode == AllocationMode.POOLED,
            Instance.status == InstanceStatus.ACTIVE,
            Instance.owner_user_id.is_(None),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    instance = row.scalar_one_or_none()

    if instance is None:
        return await create_placeholder_instance(user_id, session)

    instance.owner_user_id = user_id
    instance.provisioning_step = ProvisioningStep.CLUSTER_READY
    instance.quota_held = True

    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _existing_creating_for_user(session, user_id)
        if existing is not None:
            logger.info(
                "pool.allocate.race_existing",
                extra={
                    "metric": "pool.allocate.race_existing",
                    "instance_id": existing.id,
                    "user_id": user_id,
                },
            )
            return existing
        raise

    await session.commit()
    logger.info(
        "pool.hit",
        extra={"metric": "pool.hit", "instance_id": instance.id, "user_id": user_id},
    )
    return instance


async def _existing_creating_for_user(
    session: AsyncSession, user_id: str,
) -> Instance | None:
    row = await session.execute(
        select(Instance).where(
            Instance.owner_user_id == user_id,
            Instance.allocation_mode == AllocationMode.AUTO_PROVISIONED,
            Instance.status == InstanceStatus.CREATING,
        )
    )
    return row.scalar_one_or_none()


async def create_placeholder_instance(user_id: str, session: AsyncSession) -> Instance:
    """Create a placeholder instance for a user.

    Uses a unique ``pending-<uuid>`` cluster_id so that multiple creates
    can be in-flight simultaneously (cluster_id has a unique constraint).
    ``complete_provisioning`` detects these via ``startswith("pending")``.
    """
    instance = Instance(
        cluster_id=f"pending-{uuid.uuid4()}",
        name=f"auto-{user_id[:8]}",
        engine=InstanceEngine.POLARDB_MYSQL,
        topology=InstanceTopology.SINGLE_TENANT,
        allocation_mode=AllocationMode.AUTO_PROVISIONED,
        status=InstanceStatus.CREATING,
        owner_user_id=user_id,
        provisioning_step=ProvisioningStep.PENDING,
        quota_held=True,
    )
    session.add(instance)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await _existing_creating_for_user(session, user_id)
        if existing is not None:
            logger.info(
                "pool.fallback.race_existing",
                extra={
                    "metric": "pool.fallback.race_existing",
                    "instance_id": existing.id,
                    "user_id": user_id,
                },
            )
            return existing
        raise

    await session.commit()
    logger.info(
        "pool.miss",
        extra={"metric": "pool.miss", "instance_id": instance.id, "user_id": user_id},
    )
    return instance


async def replenishment_loop(
    session_factory: async_sessionmaker[AsyncSession],
    background_tasks: set[asyncio.Task],
) -> None:
    """Periodically create new pool instances to maintain target pool size."""
    while True:
        await asyncio.sleep(60)
        try:
            await _replenish_once(session_factory, background_tasks)
        except Exception:
            logger.exception("replenishment loop error")


async def _replenish_once(
    session_factory: async_sessionmaker[AsyncSession],
    background_tasks: set[asyncio.Task],
) -> None:
    async with session_factory() as session:
        # Acquire a row-level lock on the global quota counter to serialize
        # concurrent replenishment decisions and prevent over-creation.
        from server.models.quota_counter import QuotaCounter

        await session.execute(
            select(QuotaCounter)
            .where(QuotaCounter.scope == "global")
            .with_for_update()
        )

        pool = get_config().polardb.resource_pool
        target = pool.target_size

        # Placeholder rows whose fire-and-forget creation task died (e.g.
        # process restart) keep cluster_id = pool-pending-* forever and
        # would permanently consume replenishment slots. Remove them once
        # they are far older than any live creation could be.
        from datetime import datetime, timedelta, timezone

        stale_cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=pool.provisioning_poll_timeout_seconds * 2
        )
        stale_placeholders = (
            await session.execute(
                select(Instance).where(
                    Instance.allocation_mode == AllocationMode.POOLED,
                    Instance.status.in_(
                        [InstanceStatus.CREATING, InstanceStatus.FAILED]
                    ),
                    Instance.owner_user_id.is_(None),
                    Instance.cluster_id.like("pool-pending-%"),
                    Instance.updated_at < stale_cutoff,
                )
            )
        ).scalars().all()
        for stale in stale_placeholders:
            await session.delete(stale)
            logger.warning(
                "pool.replenish.stale_placeholder_removed",
                extra={
                    "metric": "pool.replenish.stale_placeholder_removed",
                    "cluster_id": stale.cluster_id,
                },
            )

        if target <= 0:
            if stale_placeholders:
                await session.commit()
            return

        if (
            not pool.region_id
            or not pool.zone_id
            or not pool.vpc_id
            or not pool.vswitch_id
        ):
            if stale_placeholders:
                await session.commit()
            logger.warning(
                "pool replenishment skipped: explicit region, zone, VPC, "
                "and VSwitch configuration is required"
            )
            return

        pooled_count = (
            await session.execute(
                select(func.count())
                .select_from(Instance)
                .where(
                    Instance.allocation_mode == AllocationMode.POOLED,
                    Instance.status.in_(
                        [InstanceStatus.CREATING, InstanceStatus.ACTIVE]
                    ),
                    Instance.owner_user_id.is_(None),
                )
            )
        ).scalar() or 0

        fallback_count = (
            await session.execute(
                select(func.count())
                .select_from(Instance)
                .where(
                    Instance.allocation_mode == AllocationMode.AUTO_PROVISIONED,
                    Instance.status == InstanceStatus.CREATING,
                    Instance.provisioning_step.in_(
                        [ProvisioningStep.PENDING, ProvisioningStep.CLUSTER_READY]
                    ),
                )
            )
        ).scalar() or 0

        effective = pooled_count + fallback_count
        if effective >= target:
            if stale_placeholders:
                await session.commit()
            return

        needed = target - effective
        settings = get_config().polardb.provisioning_settings()

        # Create POOL_CREATING placeholder rows inside the lock to reserve slots,
        # so concurrent replenish runs see accurate counts.
        placeholder_ids: list[str] = []
        for _ in range(needed):
            inst = Instance(
                cluster_id=f"pool-pending-{uuid.uuid4()}",
                name="pool-pending",
                engine=InstanceEngine.POLARDB_MYSQL,
                topology=InstanceTopology.SINGLE_TENANT,
                allocation_mode=AllocationMode.POOLED,
                status=InstanceStatus.CREATING,
            )
            session.add(inst)
            placeholder_ids.append(inst.id)

        await session.commit()

    async with session_factory() as cred_session:
        from server.aliyun.polardb_client import get_polardb_client_async

        client = await get_polardb_client_async(cred_session)
    timeout = pool.provisioning_poll_timeout_seconds

    for placeholder_id in placeholder_ids:
        task = asyncio.create_task(
            _create_pool_instance(client, settings, timeout, session_factory, placeholder_id)
        )
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)

    logger.info(
        "pool.replenish.created",
        extra={"metric": "pool.replenish.created", "count": needed, "target": target},
    )


async def _create_pool_instance(
    client,
    settings: dict,
    timeout: int,
    session_factory: async_sessionmaker[AsyncSession],
    placeholder_id: str | None = None,
) -> None:
    async with session_factory() as session:
        try:
            result = await client.create_agentic_db(settings)
            cluster_id = result["cluster_id"]

            if placeholder_id:
                inst = await session.get(Instance, placeholder_id)
                if inst:
                    inst.cluster_id = cluster_id
                    inst.name = f"pool-{cluster_id[-8:]}"
                    await session.commit()
                else:
                    logger.warning("placeholder %s disappeared", placeholder_id)
                    return
            else:
                inst = Instance(
                    cluster_id=cluster_id,
                    name=f"pool-{cluster_id[-8:]}",
                    engine=InstanceEngine.POLARDB_MYSQL,
                    topology=InstanceTopology.SINGLE_TENANT,
                    allocation_mode=AllocationMode.POOLED,
                    status=InstanceStatus.CREATING,
                )
                session.add(inst)
                await session.commit()
        except Exception:
            logger.exception("pool instance creation failed")
            if placeholder_id:
                async with session_factory() as err_session:
                    row = await err_session.get(Instance, placeholder_id)
                    if row:
                        row.status = InstanceStatus.FAILED
                        await err_session.commit()
            return

    try:
        from server.core.provisioner import _poll_until_running

        await _poll_until_running(cluster_id, client, timeout)
    except Exception:
        async with session_factory() as session:
            row = (
                await session.execute(
                    select(Instance).where(Instance.cluster_id == cluster_id)
                )
            ).scalar_one_or_none()
            if row:
                row.status = InstanceStatus.FAILED
                await session.commit()
        logger.exception("pool instance poll failed for %s", cluster_id)
        return

    async with session_factory() as session:
        row = (
            await session.execute(
                select(Instance).where(Instance.cluster_id == cluster_id)
            )
        ).scalar_one_or_none()
        if row:
            row.status = InstanceStatus.ACTIVE
            await session.commit()


async def health_check_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Periodically check pooled instances are still running."""
    while True:
        await asyncio.sleep(300)
        try:
            await _health_check_once(session_factory)
        except Exception:
            logger.exception("health check loop error")


async def _health_check_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as cred_session:
        from server.aliyun.polardb_client import get_polardb_client_async

        client = await get_polardb_client_async(cred_session)
    async with session_factory() as session:
        pooled = (
            await session.execute(
                select(Instance).where(
                    Instance.allocation_mode == AllocationMode.POOLED,
                    Instance.status == InstanceStatus.ACTIVE,
                    Instance.owner_user_id.is_(None),
                )
            )
        ).scalars().all()

    for inst in pooled:
        try:
            attr = await client.describe_cluster_attribute(inst.cluster_id)
            if attr["status"] != "Running":
                async with session_factory() as session:
                    i = await session.get(Instance, inst.id)
                    if i:
                        i.status = InstanceStatus.FAILED
                        await session.commit()
                logger.warning(
                    "pool.health_check.failed",
                    extra={
                        "metric": "pool.health_check.failed",
                        "cluster_id": inst.cluster_id,
                    },
                )
        except Exception:
            logger.warning(
                "health check API error for %s, skipping", inst.cluster_id
            )
