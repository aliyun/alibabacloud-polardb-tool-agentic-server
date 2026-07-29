# server/core/provisioner.py
from __future__ import annotations

import asyncio
import logging
import secrets
import string
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.aliyun.polardb_client import PolarDBClient
from server.config import get_config
from server.models import (
    AllocationMode,
    Instance,
    InstanceStatus,
    User,
)
from server.models.instance import ProvisioningStep
from server.models.user import ProvisioningMode

logger = logging.getLogger(__name__)


class ProvisioningError(Exception):
    pass


def generate_db_password() -> str:
    """Generate a 19-char password guaranteed to satisfy MySQL complexity rules.

    Format: 'Aa1' prefix (uppercase + lowercase + digit) + 16 random alphanumeric chars.
    """
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(16))
    return f"Aa1{body}"


async def resolve_primary_endpoint(
    client: PolarDBClient, cluster_id: str, preferred_net_type: str = "Private"
) -> tuple[str, int]:
    """Resolve the best available endpoint for a cluster.

    Priority: Primary > PrimaryOnProxy > Cluster (RW mode).
    Serverless clusters often only expose a Cluster-type endpoint.

    Args:
        preferred_net_type: "Private" (VPC) or "Public" (internet).
    """
    result = await client.describe_endpoints(cluster_id)
    items = result.get("items", [])
    by_type = {it["endpoint_type"]: it for it in items}
    chosen = (
        by_type.get("Primary")
        or by_type.get("PrimaryOnProxy")
        or by_type.get("Cluster")
    )
    if not chosen or not chosen.get("address_items"):
        raise ProvisioningError(
            f"no usable endpoint for {cluster_id}; "
            f"available types: {list(by_type.keys())}"
        )
    addr = next(
        (
            item
            for item in chosen["address_items"]
            if item.get("net_type") == preferred_net_type
        ),
        None,
    )
    if addr is None:
        available_net_types = sorted(
            {
                str(item.get("net_type"))
                for item in chosen["address_items"]
                if item.get("net_type")
            }
        )
        raise ProvisioningError(
            f"no {preferred_net_type} endpoint for {cluster_id}; "
            f"available network types: {available_net_types}"
        )
    return addr["connection_string"], int(addr["port"])


async def _poll_until_running(
    cluster_id: str,
    client: PolarDBClient,
    timeout_seconds: int = 600,
) -> None:
    """Poll cluster status until Running or timeout."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        attr = await client.describe_cluster_attribute(cluster_id)
        if attr["status"] == "Running":
            return
        if time.monotonic() >= deadline:
            raise ProvisioningError(
                f"cluster {cluster_id} not Running within {timeout_seconds}s "
                f"(last={attr['status']})"
            )
        await asyncio.sleep(10)


def resolve_provisioning_mode(user: User) -> ProvisioningMode:
    """Return the user's provisioning mode, defaulting to DEDICATED."""
    if user.provisioning_mode is not None:
        return user.provisioning_mode
    return ProvisioningMode.DEDICATED


async def complete_provisioning(
    instance_id: str,
    user_id: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Run all provisioning steps from current step to DONE.

    Delegates to the State Pattern runner; each state commits
    independently so that on failure the instance can be resumed
    from the last successful step.
    """
    from server.core.provisioning import run_provisioning

    await run_provisioning(
        instance_id=instance_id,
        user_id=user_id,
        session_factory=session_factory,
    )


async def _preflight_check(session: AsyncSession) -> dict | None:
    """Return an error dict if provisioning prerequisites are missing, else None."""
    errors: list[str] = []
    config = get_config()
    if (
        not config.aliyun.access_key_id
        or not config.aliyun.access_key_secret
    ):
        errors.append("Cloud credentials not configured")
    pool = config.polardb.resource_pool
    if not pool.region_id or not pool.zone_id:
        errors.append("Network config (Region/Zone) not configured")
    if not pool.vpc_id or not pool.vswitch_id:
        errors.append("Network config (VPC/VSwitch) not configured")
    if errors:
        return {"error": "PROVISIONING_NOT_READY", "message": "; ".join(errors)}
    return None


def _launch_provisioning_task(
    instance_id: str,
    user_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    background_tasks: set[asyncio.Task],
) -> None:
    """Fire-and-forget an asyncio task for provisioning."""
    task = asyncio.create_task(
        complete_provisioning(instance_id, user_id, session_factory)
    )
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)


async def startup_recovery_sweep(
    session_factory: async_sessionmaker[AsyncSession],
    background_tasks: set[asyncio.Task],
) -> None:
    """Recover instances stranded in transient states during a previous crash."""
    async with session_factory() as session:
        from server.aliyun.polardb_client import get_polardb_client_async

        client = await get_polardb_client_async(session)
        timeout = get_config().polardb.resource_pool.provisioning_poll_timeout_seconds
        threshold = timeout * 2
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=threshold)

        pool_creating = (
            await session.execute(
                select(Instance).where(
                    Instance.allocation_mode == AllocationMode.POOLED,
                    Instance.status == InstanceStatus.CREATING,
                    Instance.owner_user_id.is_(None),
                    Instance.updated_at < cutoff,
                )
            )
        ).scalars().all()

        for inst in pool_creating:
            try:
                attr = await client.describe_cluster_attribute(inst.cluster_id)
                if attr["status"] == "Running":
                    inst.status = InstanceStatus.ACTIVE
                    logger.info(
                        "recovered creating pool instance as active",
                        extra={"metric": "provisioning.recovered", "cluster_id": inst.cluster_id},
                    )
                else:
                    inst.status = InstanceStatus.FAILED
                    logger.warning(
                        "recovered creating pool instance as failed",
                        extra={"cluster_id": inst.cluster_id},
                    )
            except Exception:
                inst.status = InstanceStatus.FAILED
                logger.exception("recovery check failed for %s", inst.cluster_id)
        await session.commit()

        creating = (
            await session.execute(
                select(Instance).where(
                    Instance.status.in_(
                        [InstanceStatus.CREATING, InstanceStatus.ACTIVE]
                    ),
                    Instance.owner_user_id.isnot(None),
                    Instance.provisioning_step != ProvisioningStep.DONE,
                    Instance.updated_at < cutoff,
                )
            )
        ).scalars().all()

    for inst in creating:
        assert inst.owner_user_id is not None  # guaranteed by query filter
        logger.info(
            "re-launching stranded provisioning",
            extra={
                "metric": "provisioning.recovered",
                "instance_id": inst.id,
                "step": str(inst.provisioning_step),
            },
        )
        _launch_provisioning_task(inst.id, inst.owner_user_id, session_factory, background_tasks)
