from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime
from typing import Callable, Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import TenantProvisioningConfig
from server.core.adapter_registry import AdapterRegistry
from server.core.db_instance_worker import (
    DBInstanceResourceWorker,
    claim_serialization,
    dialect_name,
)
from server.core.multitenant_ddl import mysql_error_code
from server.models import (
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    LeaseCleanupStep,
    LeaseProvisioningStep,
    ProvisioningBackend,
    ProvisioningBackendStatus,
    ProvisioningCapacity,
)
from server.models.base import utc_now

Clock = Callable[[], datetime]
logger = logging.getLogger(__name__)

# MySQL/PolarDB error codes whose vendor messages are static text with no
# interpolated hosts, accounts, or statement fragments.
_VENDOR_MESSAGE_SAFE_CODES = frozenset({9900, 9901, 9902})

# Our own static error messages that carry no interpolated values.
_STATIC_SAFE_MESSAGES = frozenset({
    "Provisioning capacity rows are unavailable",
    "Provisioning capacity counters are inconsistent",
    "Adapter returned an invalid provisioning step",
    "Adapter returned an invalid cleanup step",
    "Provisioning backend is unavailable",
})


def _log_safe_failure(
    resource_id: str,
    phase: str,
    step: str,
    error: Exception,
) -> None:
    code = mysql_error_code(error)
    message = str(error)
    if code is not None and code in _VENDOR_MESSAGE_SAFE_CODES:
        reason = message
    elif message in _STATIC_SAFE_MESSAGES:
        reason = message
    else:
        reason = None
    logger.error(
        "database instance %s %s step %s failed: %s code=%s%s",
        resource_id,
        phase,
        step,
        type(error).__name__,
        code,
        f" reason={reason}" if reason else "",
    )

_NEXT_PROVISIONING_STEP = {
    LeaseProvisioningStep.PENDING: LeaseProvisioningStep.RESOURCE_CONFIG_CREATED,
    LeaseProvisioningStep.RESOURCE_CONFIG_CREATED: LeaseProvisioningStep.TENANT_CREATED,
    LeaseProvisioningStep.TENANT_CREATED: LeaseProvisioningStep.USER_CREATED,
    LeaseProvisioningStep.USER_CREATED: LeaseProvisioningStep.DATABASE_CREATED,
    LeaseProvisioningStep.DATABASE_CREATED: LeaseProvisioningStep.GRANTED,
    LeaseProvisioningStep.GRANTED: LeaseProvisioningStep.VERIFIED,
}

_NEXT_CLEANUP_STEP = {
    LeaseCleanupStep.PENDING: LeaseCleanupStep.DATABASE_DROPPED,
    LeaseCleanupStep.DATABASE_DROPPED: LeaseCleanupStep.TENANT_DROPPED,
    LeaseCleanupStep.TENANT_DROPPED: LeaseCleanupStep.RESOURCE_CONFIG_DROPPED,
    LeaseCleanupStep.RESOURCE_CONFIG_DROPPED: LeaseCleanupStep.RESIDUE_VERIFIED,
}


class CapacityAccountingError(RuntimeError):
    pass


class DBInstanceDispatcher:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: TenantProvisioningConfig,
        registry: AdapterRegistry,
        *,
        worker_id: str,
        clock: Clock = utc_now,
    ) -> None:
        self._worker = DBInstanceResourceWorker(
            session_factory,
            config,
            worker_id,
            clock,
        )
        self._session_factory = session_factory
        self._config = config
        self._registry = registry
        self._clock = clock
        self._wakeup = asyncio.Event()

    def wakeup(self) -> None:
        self._wakeup.set()

    async def run_once(self) -> bool:
        resource_id = await self._worker.claim_one()
        if resource_id is None:
            return False
        heartbeat = asyncio.create_task(self._worker.heartbeat(resource_id))
        try:
            await self._dispatch_claimed(resource_id)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _load_claimed(
        self,
        resource_id: str,
    ) -> tuple[
        DBInstanceResource,
        ProvisioningBackend | None,
        Instance | None,
    ] | None:
        async with self._session_factory() as session:
            resource = await session.get(DBInstanceResource, resource_id)
            if resource is None or resource.worker_id != self._worker.worker_id:
                return None
            backend = await session.get(
                ProvisioningBackend,
                resource.backend_id,
            )
            instance = (
                await session.get(Instance, backend.instance_id)
                if backend is not None
                else None
            )
            return resource, backend, instance

    async def _dispatch_claimed(self, resource_id: str) -> None:
        while True:
            loaded = await self._load_claimed(resource_id)
            if loaded is None:
                return
            resource, backend, instance = loaded
            cleanup = self._needs_cleanup(resource)
            if not cleanup and resource.status != DBInstanceStatus.CREATING:
                await self._release_claim(resource_id)
                return
            phase: Literal["forward", "cleanup"] = (
                "cleanup" if cleanup else "forward"
            )
            expected_status = resource.status
            expected_cleanup_step = resource.cleanup_step
            expected_forward_step = resource.provisioning_step
            try:
                if backend is None or instance is None:
                    raise RuntimeError("Provisioning backend is unavailable")
                if (
                    phase == "forward"
                    and backend.status == ProvisioningBackendStatus.DISABLED
                ):
                    await self._queue_disabled(resource_id)
                    return
                adapter = self._registry.get(
                    instance.engine,
                    instance.topology,
                )
                if phase == "cleanup":
                    await adapter.delete(resource)
                    if not await self._persist_cleanup_result(
                        resource_id,
                        expected_status=expected_status,
                        expected_step=expected_cleanup_step,
                        new_step=resource.cleanup_step,
                    ):
                        return
                else:
                    if expected_forward_step == LeaseProvisioningStep.GRANTED:
                        await adapter.verify(resource)
                    elif (
                        expected_forward_step
                        == LeaseProvisioningStep.VERIFIED
                    ):
                        await self._finish_ready(resource_id)
                        return
                    else:
                        await adapter.create(resource)
                    if not await self._persist_forward_result(
                        resource_id,
                        expected_step=expected_forward_step,
                        new_step=resource.provisioning_step,
                    ):
                        return
            except Exception as error:
                await self._record_failure(
                    resource_id,
                    error,
                    phase=phase,
                    expected_status=expected_status,
                    expected_forward_step=expected_forward_step,
                    expected_cleanup_step=expected_cleanup_step,
                )
                return

    @staticmethod
    def _needs_cleanup(resource: DBInstanceResource) -> bool:
        return resource.status == DBInstanceStatus.DELETING or (
            resource.status == DBInstanceStatus.FAILED
            and resource.cleanup_required
        )

    async def _locked_resource(
        self,
        session: AsyncSession,
        resource_id: str,
    ) -> DBInstanceResource | None:
        statement = select(DBInstanceResource).where(
            DBInstanceResource.id == resource_id
        )
        if dialect_name(session) != "sqlite":
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    async def _locked_backend(
        self,
        session: AsyncSession,
        backend_id: str,
    ) -> ProvisioningBackend | None:
        statement = select(ProvisioningBackend).where(
            ProvisioningBackend.id == backend_id
        )
        if dialect_name(session) != "sqlite":
            statement = statement.with_for_update()
        return (await session.execute(statement)).scalar_one_or_none()

    def _owns_claim(self, resource: DBInstanceResource) -> bool:
        return resource.worker_id == self._worker.worker_id

    @staticmethod
    def _clear_claim(resource: DBInstanceResource) -> None:
        resource.worker_id = None
        resource.worker_lease_until = None

    async def _queue_disabled(self, resource_id: str) -> None:
        async with self._session_factory() as session:
            async with claim_serialization(session):
                resource = await self._locked_resource(session, resource_id)
                if (
                    resource is None
                    or not self._owns_claim(resource)
                    or resource.status != DBInstanceStatus.CREATING
                ):
                    await session.rollback()
                    return
                backend = await self._locked_backend(
                    session,
                    resource.backend_id,
                )
                if (
                    backend is None
                    or backend.status != ProvisioningBackendStatus.DISABLED
                ):
                    await session.rollback()
                    return
                resource.status = DBInstanceStatus.FAILED
                resource.cleanup_required = True
                resource.failure_reason = "Provisioning backend is disabled"
                resource.retry_count = 0
                resource.next_retry_at = None
                self._clear_claim(resource)
                await session.commit()

    async def _persist_forward_result(
        self,
        resource_id: str,
        *,
        expected_step: LeaseProvisioningStep,
        new_step: LeaseProvisioningStep,
    ) -> bool:
        expected_next = _NEXT_PROVISIONING_STEP.get(expected_step)
        if new_step != expected_next:
            raise RuntimeError("Adapter returned an invalid provisioning step")
        async with self._session_factory() as session:
            async with claim_serialization(session):
                resource = await self._locked_resource(session, resource_id)
                if resource is None or not self._owns_claim(resource):
                    await session.rollback()
                    return False
                if resource.status == DBInstanceStatus.DELETING:
                    self._clear_claim(resource)
                    await session.commit()
                    return False
                if (
                    resource.status != DBInstanceStatus.CREATING
                    or resource.provisioning_step != expected_step
                ):
                    self._clear_claim(resource)
                    await session.commit()
                    return False
                backend = await self._locked_backend(
                    session,
                    resource.backend_id,
                )
                if backend is None:
                    raise RuntimeError("Provisioning backend is unavailable")
                if backend.status == ProvisioningBackendStatus.DISABLED:
                    resource.status = DBInstanceStatus.FAILED
                    resource.cleanup_required = True
                    resource.failure_reason = "Provisioning backend is disabled"
                    resource.retry_count = 0
                    resource.next_retry_at = None
                    self._clear_claim(resource)
                    await session.commit()
                    return False
                # ACTIVE and DRAINING may commit an already-accepted step.
                resource.provisioning_step = new_step
                resource.retry_count = 0
                resource.next_retry_at = None
                resource.failure_reason = None
                await session.commit()
                return True

    async def _finish_ready(self, resource_id: str) -> None:
        async with self._session_factory() as session:
            async with claim_serialization(session):
                resource = await self._locked_resource(session, resource_id)
                if (
                    resource is None
                    or not self._owns_claim(resource)
                    or resource.status != DBInstanceStatus.CREATING
                    or resource.provisioning_step
                    != LeaseProvisioningStep.VERIFIED
                ):
                    await session.rollback()
                    return
                backend = await self._locked_backend(
                    session,
                    resource.backend_id,
                )
                if (
                    backend is None
                    or backend.status == ProvisioningBackendStatus.DISABLED
                ):
                    resource.status = DBInstanceStatus.FAILED
                    resource.cleanup_required = True
                    resource.failure_reason = "Provisioning backend is disabled"
                else:
                    resource.status = DBInstanceStatus.READY
                    resource.cleanup_required = False
                    resource.failure_reason = None
                resource.retry_count = 0
                resource.next_retry_at = None
                self._clear_claim(resource)
                await session.commit()

    async def _persist_cleanup_result(
        self,
        resource_id: str,
        *,
        expected_status: DBInstanceStatus,
        expected_step: LeaseCleanupStep,
        new_step: LeaseCleanupStep,
    ) -> bool:
        expected_next = _NEXT_CLEANUP_STEP.get(expected_step)
        if new_step != expected_next and not (
            expected_step == new_step == LeaseCleanupStep.RESIDUE_VERIFIED
        ):
            raise RuntimeError("Adapter returned an invalid cleanup step")
        async with self._session_factory() as session:
            async with claim_serialization(session):
                resource = await self._locked_resource(session, resource_id)
                if (
                    resource is None
                    or not self._owns_claim(resource)
                    or resource.status != expected_status
                    or resource.cleanup_step != expected_step
                    or not self._needs_cleanup(resource)
                ):
                    await session.rollback()
                    return False
                resource.cleanup_step = new_step
                resource.retry_count = 0
                resource.next_retry_at = None
                resource.failure_reason = None
                if new_step == LeaseCleanupStep.RESIDUE_VERIFIED:
                    await self._finish_cleanup_locked(session, resource)
                    await session.commit()
                    return False
                await session.commit()
                return True

    async def _finish_cleanup_locked(
        self,
        session: AsyncSession,
        resource: DBInstanceResource,
    ) -> None:
        if resource.capacity_released_at is None:
            statement = (
                select(ProvisioningCapacity)
                .where(
                    or_(
                        (
                            ProvisioningCapacity.scope_type == "agent"
                        )
                        & (
                            ProvisioningCapacity.scope_id
                            == resource.owner_agent_id
                        ),
                        (
                            ProvisioningCapacity.scope_type == "backend"
                        )
                        & (
                            ProvisioningCapacity.scope_id
                            == resource.backend_id
                        ),
                    )
                )
                .order_by(
                    ProvisioningCapacity.scope_type,
                    ProvisioningCapacity.scope_id,
                )
            )
            if dialect_name(session) != "sqlite":
                statement = statement.with_for_update()
            capacities = (await session.execute(statement)).scalars().all()
            by_scope = {
                (capacity.scope_type, capacity.scope_id): capacity
                for capacity in capacities
            }
            expected_keys = {
                ("agent", resource.owner_agent_id),
                ("backend", resource.backend_id),
            }
            if set(by_scope) != expected_keys:
                raise CapacityAccountingError(
                    "Provisioning capacity rows are unavailable"
                )
            if any(capacity.active_count <= 0 for capacity in capacities):
                raise CapacityAccountingError(
                    "Provisioning capacity counters are inconsistent"
                )
            for capacity in capacities:
                capacity.active_count -= 1
            resource.capacity_released_at = self._clock()

        credential_statement = select(InstanceCredential).where(
            InstanceCredential.resource_id == resource.id,
            InstanceCredential.purpose == CredentialPurpose.RESOURCE_ACCESS,
        )
        if dialect_name(session) != "sqlite":
            credential_statement = credential_statement.with_for_update()
        credentials = (
            (await session.execute(credential_statement)).scalars().all()
        )
        for credential in credentials:
            credential.status = CredentialStatus.REVOKED
            credential.username_ciphertext = None
            credential.password_ciphertext = None

        resource.cleanup_required = False
        resource.retry_count = 0
        resource.next_retry_at = None
        resource.failure_reason = None
        if resource.status == DBInstanceStatus.DELETING:
            resource.status = DBInstanceStatus.DELETED
        self._clear_claim(resource)

    async def _release_claim(self, resource_id: str) -> None:
        async with self._session_factory() as session:
            async with claim_serialization(session):
                resource = await self._locked_resource(session, resource_id)
                if resource is not None and self._owns_claim(resource):
                    self._clear_claim(resource)
                    await session.commit()
                else:
                    await session.rollback()

    async def _record_failure(
        self,
        resource_id: str,
        error: Exception,
        *,
        phase: Literal["forward", "cleanup"],
        expected_status: DBInstanceStatus,
        expected_forward_step: LeaseProvisioningStep,
        expected_cleanup_step: LeaseCleanupStep,
    ) -> None:
        _log_safe_failure(
            resource_id,
            phase,
            (
                expected_forward_step.value
                if phase == "forward"
                else expected_cleanup_step.value
            ),
            error,
        )
        async with self._session_factory() as session:
            async with claim_serialization(session):
                resource = await self._locked_resource(session, resource_id)
                if resource is None or not self._owns_claim(resource):
                    await session.rollback()
                    return
                if phase == "forward":
                    if resource.status == DBInstanceStatus.DELETING:
                        resource.cleanup_required = True
                        resource.retry_count = 0
                        resource.next_retry_at = None
                        resource.failure_reason = (
                            "Provisioning step failed with "
                            f"{type(error).__name__}"
                        )
                        self._clear_claim(resource)
                        await session.commit()
                        return
                    if (
                        resource.status != expected_status
                        or resource.status != DBInstanceStatus.CREATING
                        or resource.provisioning_step
                        != expected_forward_step
                    ):
                        self._clear_claim(resource)
                        await session.commit()
                        return
                elif (
                    resource.status != expected_status
                    or resource.cleanup_step != expected_cleanup_step
                    or not self._needs_cleanup(resource)
                ):
                    self._clear_claim(resource)
                    await session.commit()
                    return
                resource.retry_count += 1
                operation = (
                    "Cleanup" if phase == "cleanup" else "Provisioning"
                )
                resource.failure_reason = (
                    f"{operation} step failed with {type(error).__name__}"
                )
                if resource.retry_count > self._config.worker_max_retries:
                    resource.next_retry_at = None
                    if phase == "cleanup":
                        resource.status = DBInstanceStatus.DELETE_FAILED
                    else:
                        resource.status = DBInstanceStatus.FAILED
                        resource.cleanup_required = True
                        resource.retry_count = 0
                else:
                    resource.next_retry_at = (
                        self._clock()
                        + self._worker.backoff(resource.retry_count)
                    )
                self._clear_claim(resource)
                await session.commit()

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            try:
                processed = await self.run_once()
            except Exception as error:
                logger.error(
                    "database instance dispatcher iteration failed with %s",
                    type(error).__name__,
                )
                processed = True
            if processed:
                continue
            self._wakeup.clear()
            wakeup = asyncio.create_task(self._wakeup.wait())
            stopped = asyncio.create_task(stop_event.wait())
            try:
                await asyncio.wait(
                    {wakeup, stopped},
                    timeout=self._config.worker_poll_interval_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (wakeup, stopped):
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
