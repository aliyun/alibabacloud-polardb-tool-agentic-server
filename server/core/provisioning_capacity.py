from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.dml import Insert

from server.config import get_config
from server.core.resource_write_guard import serialized_resource_write
from server.models import (
    Agent,
    AgentProvisioningBinding,
    AgentStatus,
    DBInstanceResource,
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

class CapacityUnavailable(Exception):
    pass


def _dialect_name(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


async def _ensure_capacity_row(
    session: AsyncSession,
    scope_type: str,
    scope_id: str,
) -> None:
    dialect = _dialect_name(session)
    if dialect in {"sqlite", "postgresql", "mysql", "mariadb"}:
        await session.execute(
            _capacity_insert_statement(
                dialect,
                scope_type=scope_type,
                scope_id=scope_id,
            )
        )
    else:
        existing = (
            await session.execute(
                select(ProvisioningCapacity.id).where(
                    ProvisioningCapacity.scope_type == scope_type,
                    ProvisioningCapacity.scope_id == scope_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                ProvisioningCapacity(
                    scope_type=scope_type,
                    scope_id=scope_id,
                    active_count=0,
                )
            )


def _capacity_insert_statement(
    dialect: str,
    *,
    scope_type: str,
    scope_id: str,
) -> Insert:
    values = {
        "scope_type": scope_type,
        "scope_id": scope_id,
        "active_count": 0,
    }
    if dialect == "sqlite":
        return (
            sqlite_insert(ProvisioningCapacity)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["scope_type", "scope_id"]
            )
        )
    if dialect == "postgresql":
        return (
            postgresql_insert(ProvisioningCapacity)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["scope_type", "scope_id"]
            )
        )
    if dialect in {"mysql", "mariadb"}:
        return mysql_insert(ProvisioningCapacity).values(**values).prefix_with(
            "IGNORE"
        )
    raise ValueError(f"Unsupported capacity dialect: {dialect}")


def _capacity_lock_statement(
    dialect: str,
    *,
    agent_id: str,
    backend_id: str,
) -> Select[tuple[ProvisioningCapacity]]:
    statement = (
        select(ProvisioningCapacity)
        .where(
            or_(
                and_(
                    ProvisioningCapacity.scope_type == "agent",
                    ProvisioningCapacity.scope_id == agent_id,
                ),
                and_(
                    ProvisioningCapacity.scope_type == "backend",
                    ProvisioningCapacity.scope_id == backend_id,
                ),
            )
        )
        .order_by(
            ProvisioningCapacity.scope_type,
            ProvisioningCapacity.scope_id,
        )
    )
    if dialect != "sqlite":
        statement = statement.with_for_update()
    return statement


async def _locked_capacity_rows(
    session: AsyncSession,
    *,
    agent_id: str,
    backend_id: str,
) -> tuple[ProvisioningCapacity, ProvisioningCapacity]:
    scopes = sorted([("agent", agent_id), ("backend", backend_id)])
    for scope_type, scope_id in scopes:
        await _ensure_capacity_row(session, scope_type, scope_id)
    statement = _capacity_lock_statement(
        _dialect_name(session),
        agent_id=agent_id,
        backend_id=backend_id,
    )
    rows = (await session.execute(statement)).scalars().all()
    by_scope = {(row.scope_type, row.scope_id): row for row in rows}
    return by_scope[("agent", agent_id)], by_scope[("backend", backend_id)]


async def _reserve_candidate(
    session: AsyncSession,
    *,
    agent_id: str,
    backend_id: str,
    engine: InstanceEngine,
    build_resource: Callable[[str], DBInstanceResource],
    before_commit: (
        Callable[[AsyncSession, DBInstanceResource], Awaitable[None]] | None
    ) = None,
) -> DBInstanceResource:
    agent_statement = select(
        Agent.status,
        Agent.max_active_resources,
    ).where(Agent.id == agent_id)
    backend_statement = (
        select(
            ProvisioningBackend.status,
            ProvisioningBackend.max_active_resources,
        )
        .join(Instance, Instance.id == ProvisioningBackend.instance_id)
        .where(
            ProvisioningBackend.id == backend_id,
            Instance.engine == engine,
            Instance.topology == InstanceTopology.MULTITENANT,
            Instance.status == InstanceStatus.ACTIVE,
        )
    )
    binding_statement = select(AgentProvisioningBinding.id).where(
        AgentProvisioningBinding.agent_id == agent_id,
        AgentProvisioningBinding.backend_id == backend_id,
        AgentProvisioningBinding.enabled.is_(True),
    )
    healthy_since = utc_now() - timedelta(
        seconds=get_config()
        .polardb.tenant_provisioning.effective_backend_health_stale_after_seconds
    )
    health_statement = select(ProvisioningBackendHealth.backend_id).where(
        ProvisioningBackendHealth.backend_id == backend_id,
        ProvisioningBackendHealth.healthy.is_(True),
        ProvisioningBackendHealth.checked_at >= healthy_since,
    )
    if _dialect_name(session) != "sqlite":
        agent_statement = agent_statement.with_for_update()
        backend_statement = backend_statement.with_for_update()
        binding_statement = binding_statement.with_for_update()
        health_statement = health_statement.with_for_update()
    agent = (await session.execute(agent_statement)).one_or_none()
    backend = (await session.execute(backend_statement)).one_or_none()
    binding = (
        await session.execute(binding_statement)
    ).scalar_one_or_none()
    health = (await session.execute(health_statement)).scalar_one_or_none()
    if (
        agent is None
        or agent.status != AgentStatus.ACTIVE
        or backend is None
        or backend.status != ProvisioningBackendStatus.ACTIVE
        or binding is None
        or health is None
    ):
        raise CapacityUnavailable

    agent_capacity, backend_capacity = await _locked_capacity_rows(
        session,
        agent_id=agent_id,
        backend_id=backend_id,
    )
    default_agent_limit = (
        get_config()
        .polardb.tenant_provisioning.effective_max_active_resources_per_agent
    )
    agent_limit = agent.max_active_resources or default_agent_limit
    if backend_capacity.active_count >= backend.max_active_resources:
        raise CapacityUnavailable
    if agent_capacity.active_count >= agent_limit:
        raise CapacityUnavailable

    agent_capacity.active_count += 1
    backend_capacity.active_count += 1
    resource = build_resource(backend_id)
    session.add(resource)
    await session.flush()
    if before_commit is not None:
        await before_commit(session, resource)
    await session.commit()
    return resource


async def reserve_capacity_and_insert(
    session: AsyncSession,
    *,
    agent_id: str,
    engine: InstanceEngine,
    candidate_ids: Sequence[str],
    build_resource: Callable[[str], DBInstanceResource],
    before_commit: (
        Callable[[AsyncSession, DBInstanceResource], Awaitable[None]] | None
    ) = None,
) -> DBInstanceResource:
    async with serialized_resource_write(session):
        for backend_id in candidate_ids:
            try:
                return await _reserve_candidate(
                    session,
                    agent_id=agent_id,
                    backend_id=backend_id,
                    engine=engine,
                    build_resource=build_resource,
                    before_commit=before_commit,
                )
            except CapacityUnavailable:
                await session.rollback()
                if _dialect_name(session) == "sqlite":
                    await session.execute(text("BEGIN IMMEDIATE"))
        raise CapacityUnavailable
