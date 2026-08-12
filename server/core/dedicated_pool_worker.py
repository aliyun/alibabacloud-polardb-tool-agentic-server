from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import TenantProvisioningConfig
from server.core.dedicated_mysql import (
    DedicatedGrantVerificationError,
    DedicatedMySQL,
)
from server.core.dedicated_pool_provisioner import DedicatedPoolProvisioner
from server.core.dedicated_pool_readiness import record_worker_heartbeat
from server.core.dedicated_pool_repository import (
    PoolCapacityLimitReached,
    activate_dedicated_member,
    claim_fresh_member,
    reserve_member_purchase,
)
from server.core.db_instance_metrics import emit_dedicated_pool_signal
from server.core.provisioning_operation_budget import RateLimited
from server.core.provisioning_capacity import release_active_capacity_counters
from server.core.permission_template_service import (
    permission_snapshot_for_revision,
    permission_snapshot_to_json,
)
from server.models import (
    AllocationMode,
    DBInstanceResource,
    DBInstanceStatus,
    CredentialPurpose,
    CredentialStatus,
    DedicatedMemberStatus,
    DedicatedPool,
    DedicatedPoolMember,
    DedicatedPoolStatus,
    DedicatedPreparationStep,
    DeleteLifecycleStep,
    Instance,
    InstanceStatus,
    InstanceTopology,
    ReadinessStatus,
    ProvisioningBackend,
    ProvisioningMode,
    PermissionSyncJob,
    PermissionSyncMode,
    PermissionSyncStatus,
    PermissionSyncTarget,
    PermissionSyncTargetStatus,
    ReclaimPolicy,
)
from server.models.base import utc_now

Clock = Callable[[], datetime]
logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class DedicatedPoolWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: TenantProvisioningConfig,
        provisioner: DedicatedPoolProvisioner,
        mysql: DedicatedMySQL,
        *,
        worker_id: str,
        clock: Clock = utc_now,
    ) -> None:
        if not worker_id or len(worker_id) > 64:
            raise ValueError("worker_id must be 1-64 characters")
        self._session_factory = session_factory
        self._config = config
        self._provisioner = provisioner
        self._mysql = mysql
        self._worker_id = worker_id
        self._clock = clock
        self._next_heartbeat_at: datetime | None = None
        self._wake = asyncio.Event()

    def request_run(self) -> None:
        """Wake an idle worker after an administrator queues work."""
        self._wake.set()

    def _lease_until(self) -> datetime:
        return self._clock() + timedelta(
            seconds=self._config.worker_claim_ttl_seconds
        )

    def _backoff(self, retry_count: int) -> timedelta:
        seconds = min(
            self._config.worker_initial_backoff_seconds
            * (2 ** max(0, retry_count - 1)),
            self._config.worker_max_backoff_seconds,
        )
        return timedelta(seconds=seconds)

    async def run_once(self) -> bool:
        await self._record_heartbeat_if_due()
        lifecycle_resource_id = await self._claim_lifecycle_resource()
        if lifecycle_resource_id is not None:
            await self._process_lifecycle_resource(lifecycle_resource_id)
            return True
        deleting_member_id = await self._claim_deleting_member()
        if deleting_member_id is not None:
            await self._destroy_deleting_member(deleting_member_id)
            return True
        permission_target_id = await self._claim_permission_sync_target()
        if permission_target_id is not None:
            await self._process_permission_sync_target(permission_target_id)
            return True
        if await self._mark_expired_evidence_stale():
            return True
        member = await self._claim_health_check()
        if member is not None:
            await self._check_member(member)
            return True
        resource_id = await self._claim_pending_resource()
        if resource_id is not None:
            await self._fulfill_pending_resource(resource_id)
            return True
        member_id = await self._claim_replenishing_member()
        if member_id is not None:
            await self._advance_replenishing_member(member_id)
            return True
        return await self._reserve_replacement()

    async def _record_heartbeat_if_due(self) -> None:
        now = self._clock()
        if self._next_heartbeat_at is not None and now < self._next_heartbeat_at:
            return
        async with self._session_factory() as session:
            await record_worker_heartbeat(
                session,
                worker_id=self._worker_id,
                config_revision=1,
            )
            await session.commit()
        self._next_heartbeat_at = now + timedelta(
            seconds=self._config.dedicated_worker_heartbeat_interval_seconds
        )

    async def _claim_deleting_member(self) -> str | None:
        now = self._clock()
        lease_available = or_(
            DedicatedPoolMember.worker_id.is_(None),
            DedicatedPoolMember.worker_lease_until.is_(None),
            DedicatedPoolMember.worker_lease_until <= now,
        )
        due = and_(
            DedicatedPoolMember.status == DedicatedMemberStatus.DELETING,
            DedicatedPoolMember.allocated_resource_id.is_(None),
            or_(
                DedicatedPoolMember.next_retry_at.is_(None),
                DedicatedPoolMember.next_retry_at <= now,
            ),
            lease_available,
        )
        async with self._session_factory() as session:
            member_id = await session.scalar(
                select(DedicatedPoolMember.id)
                .where(due)
                .order_by(
                    DedicatedPoolMember.created_at,
                    DedicatedPoolMember.id,
                )
                .limit(1)
            )
            if member_id is None:
                return None
            result = await session.execute(
                update(DedicatedPoolMember)
                .where(DedicatedPoolMember.id == member_id, due)
                .values(
                    worker_id=self._worker_id,
                    worker_lease_until=self._lease_until(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return member_id

    async def _destroy_deleting_member(self, member_id: str) -> None:
        async with self._session_factory() as session:
            member = await session.get(DedicatedPoolMember, member_id)
            if member is None or member.worker_id != self._worker_id:
                return
            cluster_id = member.instance.cluster_id
        try:
            await self._provisioner.delete_cluster(cluster_id)
        except Exception:
            async with self._session_factory() as session:
                member = await session.get(DedicatedPoolMember, member_id)
                if member is None or member.worker_id != self._worker_id:
                    return
                member.retry_count += 1
                member.failure_reason = "DEDICATED_MEMBER_DESTROY_FAILED"
                member.worker_id = None
                member.worker_lease_until = None
                member.next_retry_at = self._clock() + self._backoff(
                    member.retry_count
                )
                await session.commit()
            return
        async with self._session_factory() as session:
            member = await session.get(DedicatedPoolMember, member_id)
            if member is None or member.worker_id != self._worker_id:
                return
            member.status = DedicatedMemberStatus.DELETED
            member.failure_reason = None
            member.worker_id = None
            member.worker_lease_until = None
            member.next_retry_at = None
            await session.commit()

    async def _claim_permission_sync_target(self) -> str | None:
        now = self._clock()
        lease_available = or_(
            PermissionSyncTarget.worker_id.is_(None),
            PermissionSyncTarget.worker_lease_until.is_(None),
            PermissionSyncTarget.worker_lease_until <= now,
        )
        due = and_(
            PermissionSyncTarget.status
            == PermissionSyncTargetStatus.PENDING,
            or_(
                PermissionSyncTarget.next_retry_at.is_(None),
                PermissionSyncTarget.next_retry_at <= now,
            ),
            lease_available,
        )
        async with self._session_factory() as session:
            target_id = await session.scalar(
                select(PermissionSyncTarget.id)
                .join(
                    PermissionSyncJob,
                    PermissionSyncJob.id == PermissionSyncTarget.job_id,
                )
                .where(
                    PermissionSyncJob.mode == PermissionSyncMode.APPLY,
                    PermissionSyncJob.status.in_(
                        [
                            PermissionSyncStatus.PENDING,
                            PermissionSyncStatus.RUNNING,
                        ]
                    ),
                    due,
                )
                .order_by(
                    PermissionSyncTarget.created_at,
                    PermissionSyncTarget.id,
                )
                .limit(1)
            )
            if target_id is None:
                return None
            result = await session.execute(
                update(PermissionSyncTarget)
                .where(PermissionSyncTarget.id == target_id, due)
                .values(
                    status=PermissionSyncTargetStatus.RUNNING,
                    worker_id=self._worker_id,
                    worker_lease_until=self._lease_until(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            target = await session.get(PermissionSyncTarget, target_id)
            if target is not None:
                target.job.status = PermissionSyncStatus.RUNNING
            await session.commit()
            return target_id

    async def _process_permission_sync_target(
        self, target_id: str
    ) -> None:
        try:
            async with self._session_factory() as session:
                target = await session.get(PermissionSyncTarget, target_id)
                if (
                    target is None
                    or target.worker_id != self._worker_id
                    or target.status != PermissionSyncTargetStatus.RUNNING
                ):
                    return
                member = await session.get(
                    DedicatedPoolMember, target.member_id
                )
                if member is None:
                    raise RuntimeError("Permission sync member is unavailable")
                revision = target.job.template_revision
                snapshot = permission_snapshot_for_revision(revision)
                encoded = permission_snapshot_to_json(snapshot)
                if target.change_required:
                    # The new desired snapshot exists only in this transaction
                    # until exact grants and credential health are verified.
                    member.permission_template_revision_id = revision.id
                    member.permission_snapshot_json = encoded
                    resource = (
                        await session.get(
                            DBInstanceResource, target.resource_id
                        )
                        if target.resource_id is not None
                        else None
                    )
                    if resource is not None:
                        resource.permission_template_id = revision.template_id
                        resource.permission_template_revision_id = revision.id
                        resource.permission_snapshot_json = encoded
                    await self._mysql.synchronize_permissions(member, snapshot)
                target.status = PermissionSyncTargetStatus.SUCCEEDED
                target.failure_reason = None
                target.worker_id = None
                target.worker_lease_until = None
                target.next_retry_at = None
                await self._refresh_permission_sync_job(session, target.job)
                await session.commit()
                emit_dedicated_pool_signal(
                    signal="permission_sync", outcome="succeeded"
                )
        except Exception:
            await self._record_permission_sync_failure(target_id)
            emit_dedicated_pool_signal(
                signal="permission_sync", outcome="failed"
            )

    async def _record_permission_sync_failure(self, target_id: str) -> None:
        async with self._session_factory() as session:
            target = await session.get(PermissionSyncTarget, target_id)
            if target is None or target.worker_id != self._worker_id:
                return
            target.retry_count += 1
            target.failure_reason = "PERMISSION_SYNC_FAILED"
            target.worker_id = None
            target.worker_lease_until = None
            if target.retry_count > self._config.worker_max_retries:
                target.status = PermissionSyncTargetStatus.FAILED
                target.next_retry_at = None
            else:
                target.status = PermissionSyncTargetStatus.PENDING
                target.next_retry_at = self._clock() + self._backoff(
                    target.retry_count
                )
            await self._refresh_permission_sync_job(session, target.job)
            await session.commit()

    @staticmethod
    async def _refresh_permission_sync_job(
        session: AsyncSession, job: PermissionSyncJob
    ) -> None:
        await session.flush()
        statuses = list(
            await session.scalars(
                select(PermissionSyncTarget.status).where(
                    PermissionSyncTarget.job_id == job.id
                )
            )
        )
        job.completed_count = sum(
            value == PermissionSyncTargetStatus.SUCCEEDED
            for value in statuses
        )
        job.failed_count = sum(
            value == PermissionSyncTargetStatus.FAILED for value in statuses
        )
        if job.completed_count + job.failed_count == job.total_count:
            if job.failed_count:
                job.status = PermissionSyncStatus.FAILED
                job.failure_reason = "PERMISSION_SYNC_FAILED"
            else:
                job.status = PermissionSyncStatus.SUCCEEDED
                job.failure_reason = None
        else:
            job.status = PermissionSyncStatus.RUNNING

    async def _claim_lifecycle_resource(self) -> str | None:
        now = self._clock()
        lifecycle_due = or_(
            DBInstanceResource.status.in_(
                [DBInstanceStatus.DELETING, DBInstanceStatus.RESTORING]
            ),
            and_(
                DBInstanceResource.status == DBInstanceStatus.COOLING_DOWN,
                DBInstanceResource.cooldown_until.is_not(None),
                DBInstanceResource.cooldown_until <= now,
            ),
        )
        lease_available = or_(
            DBInstanceResource.worker_id.is_(None),
            DBInstanceResource.worker_lease_until.is_(None),
            DBInstanceResource.worker_lease_until <= now,
        )
        async with self._session_factory() as session:
            candidate = await session.scalar(
                select(DBInstanceResource.id)
                .where(
                    DBInstanceResource.provisioning_mode
                    == ProvisioningMode.DEDICATED,
                    lifecycle_due,
                    or_(
                        DBInstanceResource.next_retry_at.is_(None),
                        DBInstanceResource.next_retry_at <= now,
                    ),
                    lease_available,
                )
                .order_by(DBInstanceResource.created_at, DBInstanceResource.id)
                .limit(1)
            )
            if candidate is None:
                return None
            result = await session.execute(
                update(DBInstanceResource)
                .where(
                    DBInstanceResource.id == candidate,
                    lifecycle_due,
                    lease_available,
                )
                .values(
                    worker_id=self._worker_id,
                    worker_lease_until=self._lease_until(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return candidate

    async def _process_lifecycle_resource(self, resource_id: str) -> None:
        async with self._session_factory() as session:
            resource = await session.get(DBInstanceResource, resource_id)
            if resource is None or resource.worker_id != self._worker_id:
                return
            member = await session.scalar(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.allocated_resource_id == resource.id
                )
            )
            if member is None:
                await self._record_lifecycle_failure(
                    resource_id, RuntimeError("Dedicated member is unavailable")
                )
                return
            status = resource.status
            delete_step = resource.delete_step
            cluster_id = member.instance.cluster_id

        try:
            if status == DBInstanceStatus.RESTORING:
                await self._mysql.restore(member)
                await self._finish_dedicated_restore(resource_id)
                return
            if status == DBInstanceStatus.COOLING_DOWN:
                delete_step = await self._begin_dedicated_cleanup(resource_id)
                if delete_step is None:
                    return
            if delete_step == DeleteLifecycleStep.PHYSICAL_DESTROY:
                await self._provisioner.delete_cluster(cluster_id)
                await self._finish_dedicated_cleanup(
                    resource_id, sanitized=False
                )
                return
            if delete_step == DeleteLifecycleStep.SANITIZE_DISPATCH:
                await self._mysql.drop_sandbox(member)
                await self._finish_dedicated_cleanup(
                    resource_id, sanitized=True
                )
                return
            await self._mysql.disconnect(member)
            await self._finish_dedicated_disconnect(resource_id)
        except Exception as error:
            await self._record_lifecycle_failure(resource_id, error)

    async def _finish_dedicated_disconnect(self, resource_id: str) -> None:
        async with self._session_factory() as session:
            resource = await session.get(DBInstanceResource, resource_id)
            if (
                resource is None
                or resource.worker_id != self._worker_id
                or resource.status != DBInstanceStatus.DELETING
            ):
                return
            member = await session.scalar(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.allocated_resource_id == resource.id
                )
            )
            duration = resource.effective_delete_cooldown_duration_hours
            if member is None or duration is None:
                raise RuntimeError("Dedicated cooldown state is incomplete")
            disconnected_at = self._clock()
            resource.disconnected_at = disconnected_at
            resource.cooldown_until = disconnected_at + timedelta(hours=duration)
            resource.status = DBInstanceStatus.COOLING_DOWN
            resource.delete_step = DeleteLifecycleStep.COOLING_DOWN
            resource.failure_reason = None
            resource.retry_count = 0
            resource.next_retry_at = None
            resource.worker_id = None
            resource.worker_lease_until = None
            member.status = DedicatedMemberStatus.COOLING_DOWN
            await session.commit()
            emit_dedicated_pool_signal(
                signal="lifecycle", outcome="disconnected"
            )
            emit_dedicated_pool_signal(
                signal="lifecycle", outcome="cooling_down"
            )

    async def _begin_dedicated_cleanup(
        self, resource_id: str
    ) -> DeleteLifecycleStep | None:
        async with self._session_factory() as session:
            resource = await session.get(DBInstanceResource, resource_id)
            if (
                resource is None
                or resource.worker_id != self._worker_id
                or resource.status != DBInstanceStatus.COOLING_DOWN
                or resource.disconnected_at is None
                or resource.cooldown_until is None
            ):
                return None
            deadline = resource.cooldown_until
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if deadline > self._clock():
                resource.worker_id = None
                resource.worker_lease_until = None
                await session.commit()
                return None
            member = await session.scalar(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.allocated_resource_id == resource.id
                )
            )
            if member is None:
                raise RuntimeError("Dedicated member is unavailable")
            step = (
                DeleteLifecycleStep.SANITIZE_DISPATCH
                if resource.reclaim_policy == ReclaimPolicy.SANITIZE_AND_REUSE
                else DeleteLifecycleStep.PHYSICAL_DESTROY
            )
            resource.status = DBInstanceStatus.DELETING
            resource.delete_step = step
            member.status = (
                DedicatedMemberStatus.SANITIZING
                if step == DeleteLifecycleStep.SANITIZE_DISPATCH
                else DedicatedMemberStatus.DELETING
            )
            await session.commit()
            return step

    async def _finish_dedicated_cleanup(
        self, resource_id: str, *, sanitized: bool
    ) -> None:
        async with self._session_factory() as session:
            resource = await session.get(DBInstanceResource, resource_id)
            if resource is None or resource.worker_id != self._worker_id:
                return
            expected_step = (
                DeleteLifecycleStep.SANITIZE_DISPATCH
                if sanitized
                else DeleteLifecycleStep.PHYSICAL_DESTROY
            )
            if resource.delete_step != expected_step:
                return
            member = await session.scalar(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.allocated_resource_id == resource.id
                )
            )
            if member is None:
                raise RuntimeError("Dedicated member is unavailable")
            if resource.capacity_released_at is None:
                await release_active_capacity_counters(
                    session,
                    agent_id=resource.owner_agent_id,
                    backend_id=resource.backend_id,
                )
                resource.capacity_released_at = self._clock()
            for credential in resource.credentials:
                if credential.purpose == CredentialPurpose.RESOURCE_ACCESS:
                    credential.status = CredentialStatus.REVOKED
                    credential.username_ciphertext = None
                    credential.password_ciphertext = None
            resource.status = DBInstanceStatus.DELETED
            resource.delete_step = DeleteLifecycleStep.COMPLETE
            resource.cleanup_required = False
            resource.worker_id = None
            resource.worker_lease_until = None
            resource.failure_reason = None
            member.allocated_resource_id = None
            if sanitized:
                member.sandbox_username_ciphertext = None
                member.sandbox_password_ciphertext = None
                member.database_name = None
                member.permission_template_revision_id = None
                member.permission_snapshot_json = None
                member.last_ready_verified_at = None
                member.readiness_status = ReadinessStatus.STALE
                member.preparation_step = (
                    DedicatedPreparationStep.LIFECYCLE_ACCOUNT_CREATED
                )
                member.status = DedicatedMemberStatus.REPLENISHING
            else:
                member.status = DedicatedMemberStatus.DELETED
            await session.commit()
            emit_dedicated_pool_signal(
                signal="lifecycle",
                outcome="sanitized" if sanitized else "destroyed",
            )

    async def _finish_dedicated_restore(self, resource_id: str) -> None:
        async with self._session_factory() as session:
            resource = await session.get(DBInstanceResource, resource_id)
            if (
                resource is None
                or resource.worker_id != self._worker_id
                or resource.status != DBInstanceStatus.RESTORING
            ):
                return
            member = await session.scalar(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.allocated_resource_id == resource.id
                )
            )
            if member is None:
                raise RuntimeError("Dedicated member is unavailable")
            for credential in resource.credentials:
                if credential.purpose == CredentialPurpose.RESOURCE_ACCESS:
                    credential.status = CredentialStatus.ACTIVE
            resource.status = DBInstanceStatus.READY
            resource.delete_step = DeleteLifecycleStep.PENDING
            resource.delete_requested_at = None
            resource.disconnected_at = None
            resource.cooldown_until = None
            resource.restore_source_status = None
            resource.restore_failure_reason = None
            resource.failure_reason = None
            resource.worker_id = None
            resource.worker_lease_until = None
            member.status = DedicatedMemberStatus.ALLOCATED
            await session.commit()
            emit_dedicated_pool_signal(
                signal="lifecycle", outcome="restored"
            )

    async def _record_lifecycle_failure(
        self, resource_id: str, error: Exception
    ) -> None:
        async with self._session_factory() as session:
            resource = await session.get(DBInstanceResource, resource_id)
            if resource is None or resource.worker_id != self._worker_id:
                return
            member = await session.scalar(
                select(DedicatedPoolMember).where(
                    DedicatedPoolMember.allocated_resource_id == resource.id
                )
            )
            if resource.status == DBInstanceStatus.RESTORING:
                source = resource.restore_source_status
                resource.status = (
                    DBInstanceStatus.COOLING_DOWN
                    if source == DBInstanceStatus.COOLING_DOWN
                    else DBInstanceStatus.DELETE_FAILED
                )
                resource.restore_failure_reason = (
                    f"Restore verification failed with {type(error).__name__}"
                )
            else:
                resource.retry_count += 1
                resource.failure_reason = (
                    f"Dedicated lifecycle step failed with {type(error).__name__}"
                )
                if resource.retry_count > self._config.worker_max_retries:
                    resource.status = DBInstanceStatus.DELETE_FAILED
                    resource.next_retry_at = None
                    if member is not None and resource.delete_step in {
                        DeleteLifecycleStep.SANITIZE_DISPATCH,
                        DeleteLifecycleStep.PHYSICAL_DESTROY,
                    }:
                        member.status = DedicatedMemberStatus.QUARANTINED
                else:
                    resource.next_retry_at = self._clock() + self._backoff(
                        resource.retry_count
                    )
            resource.worker_id = None
            resource.worker_lease_until = None
            await session.commit()
            emit_dedicated_pool_signal(
                signal="lifecycle", outcome="failed"
            )

    async def _claim_pending_resource(self) -> str | None:
        now = self._clock()
        async with self._session_factory() as session:
            candidate = await session.scalar(
                select(DBInstanceResource.id)
                .where(
                    DBInstanceResource.provisioning_mode
                    == ProvisioningMode.DEDICATED,
                    DBInstanceResource.status == DBInstanceStatus.CREATING,
                    DBInstanceResource.allocated_instance_id.is_(None),
                    or_(
                        DBInstanceResource.next_retry_at.is_(None),
                        DBInstanceResource.next_retry_at <= now,
                    ),
                    or_(
                        DBInstanceResource.worker_id.is_(None),
                        DBInstanceResource.worker_lease_until.is_(None),
                        DBInstanceResource.worker_lease_until <= now,
                    ),
                )
                .order_by(DBInstanceResource.created_at, DBInstanceResource.id)
                .limit(1)
            )
            if candidate is None:
                return None
            result = await session.execute(
                update(DBInstanceResource)
                .where(
                    DBInstanceResource.id == candidate,
                    DBInstanceResource.status == DBInstanceStatus.CREATING,
                    DBInstanceResource.allocated_instance_id.is_(None),
                    or_(
                        DBInstanceResource.worker_id.is_(None),
                        DBInstanceResource.worker_lease_until.is_(None),
                        DBInstanceResource.worker_lease_until <= now,
                    ),
                )
                .values(
                    worker_id=self._worker_id,
                    worker_lease_until=self._lease_until(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return candidate

    async def _fulfill_pending_resource(self, resource_id: str) -> None:
        async with self._session_factory() as session:
            resource = await session.get(DBInstanceResource, resource_id)
            if resource is None or resource.worker_id != self._worker_id:
                return
            backend = await session.get(ProvisioningBackend, resource.backend_id)
            if backend is None or backend.dedicated_pool_id is None:
                resource.status = DBInstanceStatus.FAILED
                resource.failure_reason = "DEDICATED_BACKEND_UNAVAILABLE"
            else:
                member = await claim_fresh_member(
                    session,
                    pool_id=backend.dedicated_pool_id,
                    resource_id=resource.id,
                    now=self._clock(),
                )
                if member is not None:
                    activate_dedicated_member(member, resource)
                else:
                    resource.next_retry_at = self._clock() + timedelta(
                        seconds=self._config.worker_poll_interval_seconds
                    )
            resource.worker_id = None
            resource.worker_lease_until = None
            await session.commit()

    async def _active_pool_ids(self) -> list[str]:
        async with self._session_factory() as session:
            return list(
                (
                    await session.execute(
                        select(DedicatedPool.id)
                        .where(DedicatedPool.status == DedicatedPoolStatus.ACTIVE)
                        .order_by(DedicatedPool.id)
                    )
                )
                .scalars()
                .all()
            )

    async def _mark_expired_evidence_stale(self) -> bool:
        now = self._clock()
        changed = 0
        async with self._session_factory() as session:
            pools = list(
                (
                    await session.execute(
                        select(DedicatedPool).where(
                            DedicatedPool.status == DedicatedPoolStatus.ACTIVE
                        )
                    )
                )
                .scalars()
                .all()
            )
            for pool in pools:
                cutoff = _as_utc(now) - timedelta(
                    seconds=pool.available_health_stale_after_seconds
                )
                result = await session.execute(
                    update(DedicatedPoolMember)
                    .where(
                        DedicatedPoolMember.pool_id == pool.id,
                        DedicatedPoolMember.status
                        == DedicatedMemberStatus.AVAILABLE,
                        DedicatedPoolMember.readiness_status
                        == ReadinessStatus.FRESH,
                        or_(
                            DedicatedPoolMember.last_ready_verified_at.is_(None),
                            DedicatedPoolMember.last_ready_verified_at < cutoff,
                        ),
                    )
                    .values(readiness_status=ReadinessStatus.STALE)
                    .execution_options(synchronize_session=False)
                )
                changed += result.rowcount
            if changed:
                await session.commit()
            else:
                await session.rollback()
        if changed:
            emit_dedicated_pool_signal(
                signal="readiness",
                outcome="stale_excluded",
                value=float(changed),
            )
        return changed > 0

    async def _claim_health_check(self) -> DedicatedPoolMember | None:
        now = self._clock()
        async with self._session_factory() as session:
            pools = (
                await session.execute(
                    select(
                        DedicatedPool.id,
                        DedicatedPool.available_health_check_interval_seconds,
                    )
                    .where(DedicatedPool.status == DedicatedPoolStatus.ACTIVE)
                    .order_by(DedicatedPool.id)
                )
            ).all()
        for pool_id, interval_seconds in pools:
            check_cutoff = _as_utc(now) - timedelta(
                seconds=interval_seconds
            )
            due = or_(
                DedicatedPoolMember.readiness_status.in_(
                    [ReadinessStatus.STALE, ReadinessStatus.CHECKING]
                ),
                and_(
                    DedicatedPoolMember.readiness_status
                    == ReadinessStatus.FRESH,
                    DedicatedPoolMember.last_ready_verified_at.is_not(None),
                    DedicatedPoolMember.last_ready_verified_at <= check_cutoff,
                ),
            )
            lease_available = or_(
                DedicatedPoolMember.worker_id.is_(None),
                DedicatedPoolMember.worker_lease_until.is_(None),
                DedicatedPoolMember.worker_lease_until <= now,
            )
            async with self._session_factory() as session:
                candidate = await session.scalar(
                    select(DedicatedPoolMember.id)
                    .where(
                        DedicatedPoolMember.pool_id == pool_id,
                        DedicatedPoolMember.status
                        == DedicatedMemberStatus.AVAILABLE,
                        due,
                        or_(
                            DedicatedPoolMember.next_retry_at.is_(None),
                            DedicatedPoolMember.next_retry_at <= now,
                        ),
                        lease_available,
                    )
                    .order_by(
                        case(
                            (
                                DedicatedPoolMember.readiness_status
                                == ReadinessStatus.CHECKING,
                                0,
                            ),
                            (
                                DedicatedPoolMember.readiness_status
                                == ReadinessStatus.STALE,
                                1,
                            ),
                            else_=2,
                        ),
                        DedicatedPoolMember.id,
                    )
                    .limit(1)
                )
                if candidate is None:
                    continue
                result = await session.execute(
                    update(DedicatedPoolMember)
                    .where(
                        DedicatedPoolMember.id == candidate,
                        DedicatedPoolMember.status
                        == DedicatedMemberStatus.AVAILABLE,
                        due,
                        lease_available,
                    )
                    .values(
                        readiness_status=ReadinessStatus.CHECKING,
                        worker_id=self._worker_id,
                        worker_lease_until=self._lease_until(),
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    await session.rollback()
                    continue
                await session.commit()
                emit_dedicated_pool_signal(
                    signal="readiness", outcome="check_started"
                )
                return await session.get(DedicatedPoolMember, candidate)
        return None

    async def _check_member(self, member: DedicatedPoolMember) -> None:
        try:
            await self._mysql.verify(member)
        except DedicatedGrantVerificationError:
            await self._finish_health_check(member.id, conclusive_failure=True)
        except Exception:
            await self._finish_health_check(member.id, inconclusive=True)
        else:
            await self._finish_health_check(member.id, success=True)

    async def _finish_health_check(
        self,
        member_id: str,
        *,
        success: bool = False,
        inconclusive: bool = False,
        conclusive_failure: bool = False,
    ) -> None:
        if sum((success, inconclusive, conclusive_failure)) != 1:
            raise ValueError("Exactly one health outcome is required")
        async with self._session_factory() as session:
            member = await session.get(DedicatedPoolMember, member_id)
            if member is None or member.worker_id != self._worker_id:
                return
            member.worker_id = None
            member.worker_lease_until = None
            if success:
                member.readiness_status = ReadinessStatus.FRESH
                member.last_ready_verified_at = self._clock()
                member.failure_reason = None
                member.retry_count = 0
                member.next_retry_at = None
            elif conclusive_failure:
                member.status = DedicatedMemberStatus.QUARANTINED
                member.readiness_status = ReadinessStatus.STALE
                member.failure_reason = "DEDICATED_READINESS_VERIFICATION_FAILED"
                member.retry_count += 1
                member.next_retry_at = None
            else:
                member.readiness_status = ReadinessStatus.STALE
                member.failure_reason = "DEDICATED_READINESS_RECHECK_INCONCLUSIVE"
                member.retry_count += 1
                member.next_retry_at = self._clock() + self._backoff(
                    member.retry_count
                )
            await session.commit()
            emit_dedicated_pool_signal(
                signal="readiness",
                outcome=(
                    "fresh"
                    if success
                    else "failed"
                    if conclusive_failure
                    else "inconclusive"
                ),
            )

    async def _claim_replenishing_member(self) -> str | None:
        now = self._clock()
        preparation_filters = [
            DedicatedPoolMember.preparation_step
            != DedicatedPreparationStep.VERIFIED
        ]
        if self._config.dedicated_pool_preparation_mode == "openapi_only":
            preparation_filters.append(
                DedicatedPoolMember.preparation_step
                != DedicatedPreparationStep.OPENAPI_READY
            )
        async with self._session_factory() as session:
            candidate = await session.scalar(
                select(DedicatedPoolMember.id)
                .join(DedicatedPool)
                .where(
                    DedicatedPool.status == DedicatedPoolStatus.ACTIVE,
                    DedicatedPoolMember.status.in_(
                        (
                            DedicatedMemberStatus.REPLENISHING,
                            DedicatedMemberStatus.ALLOCATED_PREPARING,
                        )
                    ),
                    *preparation_filters,
                    or_(
                        DedicatedPoolMember.next_retry_at.is_(None),
                        DedicatedPoolMember.next_retry_at <= now,
                    ),
                    or_(
                        DedicatedPoolMember.worker_id.is_(None),
                        DedicatedPoolMember.worker_lease_until.is_(None),
                        DedicatedPoolMember.worker_lease_until <= now,
                    ),
                )
                .order_by(DedicatedPoolMember.created_at, DedicatedPoolMember.id)
                .limit(1)
            )
            if candidate is None:
                return None
            result = await session.execute(
                update(DedicatedPoolMember)
                .where(
                    DedicatedPoolMember.id == candidate,
                    DedicatedPoolMember.status.in_(
                        (
                            DedicatedMemberStatus.REPLENISHING,
                            DedicatedMemberStatus.ALLOCATED_PREPARING,
                        )
                    ),
                    or_(
                        DedicatedPoolMember.worker_id.is_(None),
                        DedicatedPoolMember.worker_lease_until.is_(None),
                        DedicatedPoolMember.worker_lease_until <= now,
                    ),
                )
                .values(
                    worker_id=self._worker_id,
                    worker_lease_until=self._lease_until(),
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return candidate

    async def _advance_replenishing_member(self, member_id: str) -> None:
        async with self._session_factory() as session:
            member = await session.get(DedicatedPoolMember, member_id)
            if member is None or member.worker_id != self._worker_id:
                return
            previous_step = member.preparation_step
        try:
            resulting_step = await self._provisioner.advance(
                member_id,
                allow_data_plane=(
                    self._config.dedicated_pool_preparation_mode == "full"
                ),
            )
        except Exception:
            failed = True
        else:
            failed = False
        async with self._session_factory() as session:
            member = await session.get(DedicatedPoolMember, member_id)
            if member is None or member.worker_id != self._worker_id:
                return
            member.worker_id = None
            member.worker_lease_until = None
            if failed:
                member.next_retry_at = self._clock() + self._backoff(
                    max(1, member.retry_count)
                )
            elif resulting_step == previous_step:
                member.next_retry_at = self._clock() + timedelta(
                    seconds=self._config.worker_poll_interval_seconds
                )
            else:
                member.next_retry_at = None
            await session.commit()

    async def _reserve_replacement(self) -> bool:
        now = self._clock()
        for pool_id in await self._active_pool_ids():
            async with self._session_factory() as session:
                try:
                    required = await reserve_member_purchase(
                        session,
                        pool_id=pool_id,
                        now=now,
                        intent="prewarm",
                    )
                except (PoolCapacityLimitReached, RateLimited):
                    await session.rollback()
                    continue
                if not required:
                    await session.rollback()
                    continue
                token = uuid.uuid4().hex
                instance = Instance(
                    cluster_id=f"pending-dedicated-{token}",
                    name=f"Dedicated pool member {token[:8]}",
                    topology=InstanceTopology.SINGLE_TENANT,
                    allocation_mode=AllocationMode.DEDICATED_POOL,
                    status=InstanceStatus.CREATING,
                )
                session.add(instance)
                await session.flush()
                session.add(
                    DedicatedPoolMember(
                        pool_id=pool_id,
                        instance_id=instance.id,
                        status=DedicatedMemberStatus.REPLENISHING,
                        readiness_status=ReadinessStatus.STALE,
                        preparation_step=DedicatedPreparationStep.PENDING,
                    )
                )
                await session.commit()
                emit_dedicated_pool_signal(
                    signal="replenishment", outcome="succeeded"
                )
                return True
        return False

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            try:
                processed = await self.run_once()
            except Exception as error:
                logger.error(
                    "Dedicated pool worker iteration failed with %s",
                    type(error).__name__,
                )
                processed = True
            if processed:
                await asyncio.sleep(0)
                continue
            stop_wait = asyncio.create_task(stop_event.wait())
            wake_wait = asyncio.create_task(self._wake.wait())
            waiters = {stop_wait, wake_wait}
            done: set[asyncio.Task[bool]] = set()
            try:
                done, _pending = await asyncio.wait(
                    waiters,
                    timeout=self._config.worker_poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in waiters:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
            if wake_wait in done:
                self._wake.clear()
