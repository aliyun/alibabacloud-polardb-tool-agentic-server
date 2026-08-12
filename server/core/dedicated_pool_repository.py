from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal, NamedTuple

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.db_instance_metrics import (
    emit_dedicated_pool_capacity,
    emit_dedicated_pool_guard_metric,
    emit_dedicated_pool_signal,
)
from server.core.dedicated_purchase_profile import (
    PROFILE_ID,
    PROFILE_REVISION,
    PurchaseProfileStatus,
    classify_legacy_purchase_config,
)
from server.core.provisioning_operation_budget import (
    RateLimited,
    consume_hourly_budget,
)
from server.core.legacy_pool_retirement import assert_legacy_purchase_safe
from server.models import (
    CredentialCapability,
    CredentialPurpose,
    DBInstanceResource,
    DBInstanceStatus,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    InstanceCredential,
    LeaseProvisioningStep,
    ReadinessStatus,
)


class CapacitySnapshot(NamedTuple):
    allocatable: int
    planning: int
    billable_total: int


class PoolCapacityLimitReached(RuntimeError):
    code = "POOL_CAPACITY_LIMIT_REACHED"


class PurchaseProfileUpgradeRequired(RuntimeError):
    code = "PURCHASE_PROFILE_UPGRADE_REQUIRED"


class DedicatedMemberNotReady(RuntimeError):
    pass


PurchaseIntent = Literal["prewarm", "request"]


def activate_dedicated_member(
    member: DedicatedPoolMember,
    resource: DBInstanceResource,
) -> None:
    required = (
        member.host,
        member.port,
        member.database_name,
        member.sandbox_username_ciphertext,
        member.sandbox_password_ciphertext,
        member.permission_snapshot_json,
    )
    if any(value is None for value in required):
        raise DedicatedMemberNotReady(
            "Dedicated member readiness data is incomplete"
        )
    if member.allocated_resource_id != resource.id:
        raise DedicatedMemberNotReady(
            "Dedicated member is not reserved for the resource"
        )
    resource.allocated_instance_id = member.instance_id
    resource.database_name = member.database_name
    resource.permission_template_revision_id = (
        member.permission_template_revision_id
    )
    resource.permission_template_id = (
        member.permission_template_revision.template_id
        if member.permission_template_revision is not None
        else None
    )
    resource.permission_snapshot_json = member.permission_snapshot_json
    resource.status = DBInstanceStatus.READY
    resource.provisioning_step = LeaseProvisioningStep.VERIFIED
    resource.failure_reason = None
    if not resource.credentials:
        resource.credentials.append(
            InstanceCredential(
                name="resource-access",
                purpose=CredentialPurpose.RESOURCE_ACCESS,
                capability=CredentialCapability.READWRITE,
                username_ciphertext=member.sandbox_username_ciphertext,
                password_ciphertext=member.sandbox_password_ciphertext,
                database_name=member.database_name,
                version=1,
            )
        )
    member.status = DedicatedMemberStatus.ALLOCATED


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def capacity_snapshot(
    session: AsyncSession,
    *,
    pool_id: str,
    now: datetime,
) -> CapacitySnapshot:
    pool = await session.get(DedicatedPool, pool_id)
    if pool is None:
        raise LookupError("Dedicated pool not found")
    members = list(
        (
            await session.execute(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.pool_id == pool_id
                )
            )
        )
        .scalars()
        .all()
    )
    cutoff = _as_utc(now) - timedelta(
        seconds=pool.available_health_stale_after_seconds
    )
    allocatable = sum(
        member.status == DedicatedMemberStatus.AVAILABLE
        and member.readiness_status == ReadinessStatus.FRESH
        and member.last_ready_verified_at is not None
        and _as_utc(member.last_ready_verified_at) >= cutoff
        for member in members
    )
    planning = sum(
        member.status == DedicatedMemberStatus.AVAILABLE
        or (
            member.status == DedicatedMemberStatus.REPLENISHING
            and member.allocated_resource_id is None
        )
        for member in members
    )
    billable_total = sum(
        member.status != DedicatedMemberStatus.DELETED
        for member in members
    )
    capacity_values = {
        "allocatable": allocatable,
        "planning": planning,
        "billable_total": billable_total,
        "surplus": max(0, planning - pool.target_size),
        "stale": sum(
            member.status == DedicatedMemberStatus.AVAILABLE
            and member.readiness_status == ReadinessStatus.STALE
            for member in members
        ),
        "checking": sum(
            member.status == DedicatedMemberStatus.AVAILABLE
            and member.readiness_status == ReadinessStatus.CHECKING
            for member in members
        ),
        "quarantined": sum(
            member.status == DedicatedMemberStatus.QUARANTINED
            for member in members
        ),
    }
    for kind, value in capacity_values.items():
        emit_dedicated_pool_capacity(kind=kind, value=float(value))
    return CapacitySnapshot(
        allocatable=allocatable,
        planning=planning,
        billable_total=billable_total,
    )


async def claim_fresh_member(
    session: AsyncSession,
    *,
    pool_id: str,
    resource_id: str,
    now: datetime,
) -> DedicatedPoolMember | None:
    resource_statement = select(DBInstanceResource).where(
        DBInstanceResource.id == resource_id
    )
    if session.get_bind().dialect.name != "sqlite":
        resource_statement = resource_statement.with_for_update()
    resource = (
        await session.execute(resource_statement)
    ).scalar_one_or_none()
    if resource is None:
        raise LookupError("Database resource not found")
    if resource.allocated_instance_id is not None:
        return None
    pool = await session.get(DedicatedPool, pool_id)
    if pool is None:
        raise LookupError("Dedicated pool not found")
    cutoff = _as_utc(now) - timedelta(
        seconds=pool.available_health_stale_after_seconds
    )
    candidate_statement = (
        select(DedicatedPoolMember.id)
        .where(
            DedicatedPoolMember.pool_id == pool_id,
            DedicatedPoolMember.status
            == DedicatedMemberStatus.AVAILABLE,
            DedicatedPoolMember.readiness_status == ReadinessStatus.FRESH,
            DedicatedPoolMember.last_ready_verified_at.is_not(None),
            DedicatedPoolMember.last_ready_verified_at >= cutoff,
            DedicatedPoolMember.allocated_resource_id.is_(None),
        )
        .order_by(DedicatedPoolMember.id)
        .limit(1)
    )
    if session.get_bind().dialect.name != "sqlite":
        candidate_statement = candidate_statement.with_for_update(
            skip_locked=True
        )
    candidate_id = await session.scalar(candidate_statement)
    if candidate_id is None:
        return None
    result = await session.execute(
        update(DedicatedPoolMember)
        .where(
            DedicatedPoolMember.id == candidate_id,
            DedicatedPoolMember.status
            == DedicatedMemberStatus.AVAILABLE,
            DedicatedPoolMember.readiness_status == ReadinessStatus.FRESH,
            DedicatedPoolMember.last_ready_verified_at >= cutoff,
            DedicatedPoolMember.allocated_resource_id.is_(None),
        )
        .values(
            status=DedicatedMemberStatus.ALLOCATED_PREPARING,
            allocated_resource_id=resource_id,
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return None
    member = await session.get(DedicatedPoolMember, candidate_id)
    if member is None:
        return None
    await session.refresh(member)
    resource.allocated_instance_id = member.instance_id
    await session.flush()
    return member


async def reserve_member_purchase(
    session: AsyncSession,
    *,
    pool_id: str,
    now: datetime,
    intent: PurchaseIntent,
) -> bool:
    await assert_legacy_purchase_safe(session)
    if (
        session.get_bind().dialect.name == "sqlite"
        and not session.in_transaction()
    ):
        await session.execute(text("BEGIN IMMEDIATE"))
    statement = select(DedicatedPool).where(DedicatedPool.id == pool_id)
    if session.get_bind().dialect.name != "sqlite":
        statement = statement.with_for_update()
    pool = (await session.execute(statement)).scalar_one_or_none()
    if pool is None:
        raise LookupError("Dedicated pool not found")
    if (
        pool.purchase_profile_id != PROFILE_ID
        or pool.purchase_profile_revision != PROFILE_REVISION
        or pool.storage_type is None
        or classify_legacy_purchase_config(pool.purchase_config_json)
        != PurchaseProfileStatus.VALID
    ):
        raise PurchaseProfileUpgradeRequired(
            "Dedicated pool purchase profile requires administrator upgrade"
        )
    snapshot = await capacity_snapshot(session, pool_id=pool_id, now=now)
    if intent == "prewarm" and snapshot.planning >= pool.target_size:
        emit_dedicated_pool_guard_metric(
            guard="planning_deficit",
            outcome="not_required",
        )
        emit_dedicated_pool_signal(
            signal="replenishment", outcome="not_required"
        )
        return False
    if snapshot.billable_total >= pool.max_total_members:
        emit_dedicated_pool_guard_metric(
            guard="max_total_members",
            outcome="rejected",
        )
        emit_dedicated_pool_signal(
            signal="replenishment", outcome="capacity_limited"
        )
        raise PoolCapacityLimitReached("Dedicated pool capacity limit reached")
    try:
        await consume_hourly_budget(
            session,
            scope_type="pool",
            scope_id=pool.id,
            operation="purchase",
            limit=pool.max_member_purchases_per_hour,
            now=now,
        )
    except RateLimited:
        emit_dedicated_pool_guard_metric(
            guard="purchase_rate",
            outcome="rejected",
        )
        emit_dedicated_pool_signal(
            signal="replenishment", outcome="rate_limited"
        )
        raise
    emit_dedicated_pool_guard_metric(
        guard="purchase_rate",
        outcome="accepted",
    )
    emit_dedicated_pool_signal(
        signal="replenishment", outcome="purchase_reserved"
    )
    return True


async def consume_agent_operation_budget(
    session: AsyncSession,
    *,
    pool: DedicatedPool,
    agent_id: str,
    operation: str,
    now: datetime,
) -> None:
    if operation == "create":
        limit = pool.max_create_requests_per_agent_per_hour
    elif operation == "delete":
        limit = pool.max_delete_requests_per_agent_per_hour
    else:
        raise ValueError("Unsupported Dedicated Agent operation")
    try:
        await consume_hourly_budget(
            session,
            scope_type="pool_agent",
            scope_id=f"{pool.id}:{agent_id}",
            operation=operation,
            limit=limit,
            now=now,
        )
    except RateLimited:
        emit_dedicated_pool_guard_metric(
            guard=f"agent_{operation}_rate",
            outcome="rejected",
        )
        raise
    emit_dedicated_pool_guard_metric(
        guard=f"agent_{operation}_rate",
        outcome="accepted",
    )
