from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_config
from server.models import (
    AgentProvisioningBinding,
    Instance,
    InstanceEngine,
    InstanceStatus,
    InstanceTopology,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningBackendStatus,
    ProvisioningCapacity,
)
from server.models.base import utc_now


@dataclass(frozen=True, slots=True)
class BackendCandidate:
    backend_id: str
    priority: int
    active_count: int
    max_active_resources: int


def backend_health_cutoff() -> datetime:
    stale_after = (
        get_config()
        .polardb.tenant_provisioning
        .effective_backend_health_stale_after_seconds
    )
    return utc_now() - timedelta(seconds=stale_after)


def backend_is_fresh_and_healthy(
    backend: ProvisioningBackend,
) -> bool:
    health = backend.health
    if (
        backend.status != ProvisioningBackendStatus.ACTIVE
        or health is None
        or not health.healthy
    ):
        return False
    checked_at = health.checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    return checked_at >= backend_health_cutoff()


async def list_candidates(
    session: AsyncSession,
    agent_id: str,
    engine: InstanceEngine,
    client_token: str,
) -> list[BackendCandidate]:
    healthy_since = backend_health_cutoff()
    statement = (
        select(
            ProvisioningBackend.id,
            ProvisioningBackend.priority,
            func.coalesce(ProvisioningCapacity.active_count, 0),
            ProvisioningBackend.max_active_resources,
        )
        .join(
            AgentProvisioningBinding,
            AgentProvisioningBinding.backend_id == ProvisioningBackend.id,
        )
        .join(Instance, Instance.id == ProvisioningBackend.instance_id)
        .join(
            ProvisioningBackendHealth,
            ProvisioningBackendHealth.backend_id == ProvisioningBackend.id,
        )
        .outerjoin(
            ProvisioningCapacity,
            and_(
                ProvisioningCapacity.scope_type == "backend",
                ProvisioningCapacity.scope_id == ProvisioningBackend.id,
            ),
        )
        .where(
            AgentProvisioningBinding.agent_id == agent_id,
            AgentProvisioningBinding.enabled.is_(True),
            ProvisioningBackend.status == ProvisioningBackendStatus.ACTIVE,
            Instance.engine == engine,
            Instance.topology == InstanceTopology.MULTITENANT,
            Instance.status == InstanceStatus.ACTIVE,
            ProvisioningBackendHealth.healthy.is_(True),
            ProvisioningBackendHealth.checked_at >= healthy_since,
        )
    )
    rows = (await session.execute(statement)).all()
    candidates = [
        BackendCandidate(
            backend_id=backend_id,
            priority=priority,
            active_count=active_count,
            max_active_resources=max_active_resources,
        )
        for backend_id, priority, active_count, max_active_resources in rows
    ]
    from server.core.backend_selector import order_candidates

    return order_candidates(
        candidates,
        agent_id=agent_id,
        client_token=client_token,
    )
