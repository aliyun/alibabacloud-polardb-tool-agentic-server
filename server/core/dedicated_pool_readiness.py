from __future__ import annotations

import enum
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import DedicatedWorkerHeartbeat
from server.models import DedicatedMemberStatus, PermissionTemplateRevision
from server.core.dedicated_purchase_profile import (
    AGENTIC_DEDICATED_PROFILE,
    PurchaseProfileStatus,
    build_purchase_config,
)
from server.core.permission_template_service import (
    PermissionScope,
    compile_permission_snapshot,
)
from server.core.legacy_pool_retirement import audit_legacy_pool_state

if TYPE_CHECKING:
    from server.config import AppConfig
    from server.core.dedicated_pool_service import DedicatedPoolView


class DedicatedSupplyState(str, enum.Enum):
    NOT_STARTED = "not_started"
    PREWARMING = "prewarming"
    READY = "ready"
    PARTIALLY_READY = "partially_ready"
    CAPACITY_LIMITED = "capacity_limited"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class WorkerReadinessSnapshot:
    active_worker_count: int
    last_heartbeat_at: datetime | None
    database_now: datetime


@dataclass(frozen=True, slots=True)
class AliyunAccessReadiness:
    configured: bool
    validated: bool
    credential_mode: str


@dataclass(frozen=True, slots=True)
class PurchaseProfileReadiness:
    valid: bool
    profile_id: str
    revision: int
    default_storage_type: str
    supported_storage_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PermissionTemplateReadiness:
    valid: bool
    default_revision_id: str | None


@dataclass(frozen=True, slots=True)
class DedicatedReadiness:
    worker: WorkerReadinessSnapshot
    aliyun_access: AliyunAccessReadiness
    purchase_profile: PurchaseProfileReadiness
    permission_template: PermissionTemplateReadiness
    simulation_mode: bool
    blocking_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PoolSupplySnapshot:
    state: DedicatedSupplyState
    current: int
    target: int
    blocking_reasons: tuple[str, ...]


def derive_supply_state(
    *,
    target: int,
    allocatable: int,
    planning: int,
    blockers: Collection[str],
    failures: int,
    capacity_limited: bool,
) -> DedicatedSupplyState:
    if allocatable == 0 and planning == 0 and blockers:
        return DedicatedSupplyState.NOT_STARTED
    if allocatable >= target:
        return DedicatedSupplyState.READY
    if capacity_limited:
        return DedicatedSupplyState.CAPACITY_LIMITED
    if failures > 0 and planning == 0:
        return DedicatedSupplyState.ERROR
    if allocatable == 0 and planning > 0:
        return DedicatedSupplyState.PREWARMING
    return DedicatedSupplyState.PARTIALLY_READY


async def record_worker_heartbeat(
    session: AsyncSession,
    *,
    worker_id: str,
    config_revision: int,
) -> None:
    heartbeat = await session.get(DedicatedWorkerHeartbeat, worker_id)
    if heartbeat is None:
        session.add(
            DedicatedWorkerHeartbeat(
                worker_id=worker_id,
                config_revision=config_revision,
            )
        )
        await session.flush()
        return
    await session.execute(
        update(DedicatedWorkerHeartbeat)
        .where(DedicatedWorkerHeartbeat.worker_id == worker_id)
        .values(
            config_revision=config_revision,
            last_heartbeat_at=func.current_timestamp(),
        )
        .execution_options(synchronize_session=False)
    )


async def worker_readiness_snapshot(
    session: AsyncSession,
    *,
    stale_after_seconds: int,
) -> WorkerReadinessSnapshot:
    database_now = await session.scalar(select(func.current_timestamp()))
    if database_now is None:  # pragma: no cover - database contract guard
        raise RuntimeError("Database did not return its current timestamp")
    cutoff = database_now - timedelta(seconds=stale_after_seconds)
    row = (
        await session.execute(
            select(
                func.count(DedicatedWorkerHeartbeat.worker_id),
                func.max(DedicatedWorkerHeartbeat.last_heartbeat_at),
            ).where(DedicatedWorkerHeartbeat.last_heartbeat_at >= cutoff)
        )
    ).one()
    return WorkerReadinessSnapshot(
        active_worker_count=int(row[0] or 0),
        last_heartbeat_at=row[1],
        database_now=database_now,
    )


async def _default_permission_template_readiness(
    session: AsyncSession,
) -> PermissionTemplateReadiness:
    revision = await session.get(
        PermissionTemplateRevision, "builtin-mysql-default-v1"
    )
    if revision is None:
        return PermissionTemplateReadiness(False, None)
    try:
        compile_permission_snapshot(
            revision_id=revision.id,
            template_id=revision.template_id,
            privileges_json=revision.privileges_json,
            grant_option=revision.grant_option,
            scope=PermissionScope.DEDICATED,
        )
    except ValueError:
        return PermissionTemplateReadiness(False, revision.id)
    return PermissionTemplateReadiness(True, revision.id)


async def aggregate_readiness(
    session: AsyncSession,
    *,
    config: AppConfig,
) -> DedicatedReadiness:
    tenant = config.polardb.tenant_provisioning
    worker = await worker_readiness_snapshot(
        session,
        stale_after_seconds=(
            tenant.dedicated_worker_heartbeat_stale_after_seconds
        ),
    )
    aliyun_configured = config.aliyun.has_active_credentials()
    # The client factory always prefers an active Alibaba Cloud credential.
    # Report the effective mode rather than the dormant fallback switch so the
    # console never labels real STS/AK-backed purchases as simulated.
    simulation_mode = (
        tenant.dedicated_pool_simulation_enabled and not aliyun_configured
    )
    aliyun_access = AliyunAccessReadiness(
        configured=aliyun_configured,
        # Runtime configuration is effective only after the module's
        # validation/activation workflow succeeds.
        validated=aliyun_configured,
        credential_mode=config.aliyun.credential_mode,
    )
    profile = AGENTIC_DEDICATED_PROFILE
    try:
        build_purchase_config(storage_type=profile.default_storage_type)
        profile_valid = True
    except ValueError:  # pragma: no cover - immutable profile integrity guard
        profile_valid = False
    purchase_profile = PurchaseProfileReadiness(
        valid=profile_valid,
        profile_id=profile.profile_id,
        revision=profile.revision,
        default_storage_type=profile.default_storage_type,
        supported_storage_types=profile.supported_storage_types,
    )
    permission_template = await _default_permission_template_readiness(session)
    legacy_audit = await audit_legacy_pool_state(session)
    blockers: list[str] = []
    if not tenant.dedicated_pool_enabled:
        blockers.append("DEDICATED_WORKER_DISABLED")
    elif worker.active_worker_count == 0:
        blockers.append("DEDICATED_WORKER_NOT_RUNNING")
    if not aliyun_configured and not simulation_mode:
        blockers.append("ALIYUN_ACCESS_NOT_CONFIGURED")
    if not profile_valid:
        blockers.append("PURCHASE_PROFILE_INVALID")
    if not permission_template.valid:
        blockers.append("PERMISSION_TEMPLATE_UNAVAILABLE")
    blockers.extend(legacy_audit.blocking_codes)
    return DedicatedReadiness(
        worker=worker,
        aliyun_access=aliyun_access,
        purchase_profile=purchase_profile,
        permission_template=permission_template,
        simulation_mode=simulation_mode,
        blocking_reasons=tuple(blockers),
    )


def pool_supply_snapshot(
    view: DedicatedPoolView,
    readiness: DedicatedReadiness,
    *,
    purchase_profile_status: PurchaseProfileStatus,
) -> PoolSupplySnapshot:
    pool = view.pool
    blockers = list(readiness.blocking_reasons)
    if (
        "PERMISSION_TEMPLATE_UNAVAILABLE" in blockers
        and bool(pool.permission_template_revision_id)
    ):
        blockers.remove("PERMISSION_TEMPLATE_UNAVAILABLE")
    if purchase_profile_status is PurchaseProfileStatus.UPGRADE_REQUIRED:
        blockers.append("PURCHASE_PROFILE_UPGRADE_REQUIRED")
    failures = sum(
        member.status is DedicatedMemberStatus.QUARANTINED
        for member in pool.members
    )
    capacity_limited = (
        view.capacity.billable_total >= pool.max_total_members
        and view.capacity.planning < pool.target_size
    )
    if capacity_limited:
        blockers.append("POOL_CAPACITY_LIMIT_REACHED")
    return PoolSupplySnapshot(
        state=derive_supply_state(
            target=pool.target_size,
            allocatable=view.capacity.allocatable,
            planning=view.capacity.planning,
            blockers=blockers,
            failures=failures,
            capacity_limited=capacity_limited,
        ),
        current=view.capacity.allocatable,
        target=pool.target_size,
        blocking_reasons=tuple(blockers),
    )
