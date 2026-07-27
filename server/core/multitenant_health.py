from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import TenantProvisioningConfig
from server.core.adapter_registry import AdapterRegistry
from server.core.provisioning_adapter import HealthResult
from server.core.super_connection_pool import SuperConnectionPoolManager
from server.models import (
    Instance,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningBackendStatus,
)
from server.models.base import utc_now

Clock = Callable[[], datetime]
logger = logging.getLogger(__name__)


class ProvisioningHealthWorker:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: TenantProvisioningConfig,
        registry: AdapterRegistry,
        pool_manager: SuperConnectionPoolManager,
        clock: Clock = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._registry = registry
        self._pool_manager = pool_manager
        self._clock = clock

    async def run_once(self) -> int:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ProvisioningBackend, Instance)
                    .join(
                        Instance,
                        Instance.id == ProvisioningBackend.instance_id,
                    )
                    .order_by(ProvisioningBackend.id)
                )
            ).all()

        checked = 0
        for backend, instance in rows:
            if backend.status == ProvisioningBackendStatus.DISABLED:
                try:
                    await self._pool_manager.close_backend(backend.id)
                except Exception as error:
                    await self._safe_record(
                        backend.id,
                        HealthResult(False, type(error).__name__),
                    )
                continue
            checked += 1
            try:
                adapter = self._registry.get(
                    instance.engine,
                    instance.topology,
                )
                result = await adapter.health_check(backend)
            except Exception as error:
                result = HealthResult(False, type(error).__name__)
            await self._safe_record(backend.id, result)
        return checked

    async def _safe_record(
        self,
        backend_id: str,
        result: HealthResult,
    ) -> None:
        try:
            await self._record(backend_id, result)
        except Exception:
            logger.exception(
                "provisioning backend health persistence failed",
                extra={"backend_id": backend_id},
            )

    async def _record(
        self,
        backend_id: str,
        result: HealthResult,
    ) -> None:
        async with self._session_factory() as session:
            if await session.get(ProvisioningBackend, backend_id) is None:
                return
            health = await session.get(ProvisioningBackendHealth, backend_id)
            if health is None:
                health = ProvisioningBackendHealth(
                    backend_id=backend_id,
                    healthy=result.healthy,
                    checked_at=self._clock(),
                )
                session.add(health)
            health.healthy = result.healthy
            health.checked_at = self._clock()
            if result.healthy:
                health.consecutive_failures = 0
                health.error_code = None
            else:
                health.consecutive_failures = (
                    health.consecutive_failures or 0
                ) + 1
                health.error_code = result.error_code or "UnknownError"
            await session.commit()

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        stop_event = stop_event or asyncio.Event()
        while not stop_event.is_set():
            try:
                await self.run_once()
            except Exception as error:
                logger.error(
                    "provisioning health pass failed with %s",
                    type(error).__name__,
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self._config.health_check_interval_seconds,
                )
            except TimeoutError:
                continue
