from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import TenantProvisioningConfig
from server.core.resource_write_guard import serialized_resource_write
from server.models import DBInstanceResource, DBInstanceStatus
from server.models.base import utc_now

Clock = Callable[[], datetime]
def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def dialect_name(session: AsyncSession) -> str:
    return session.get_bind().dialect.name


@asynccontextmanager
async def claim_serialization(session: AsyncSession):
    async with serialized_resource_write(session):
        yield


class DBInstanceResourceWorker:
    """Metadb claim/retry state for the global provisioning dispatcher."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: TenantProvisioningConfig,
        worker_id: str,
        clock: Clock = utc_now,
    ) -> None:
        if not worker_id or len(worker_id) > 64:
            raise ValueError("worker_id must be 1-64 characters")
        self.session_factory = session_factory
        self.config = config
        self.worker_id = worker_id
        self.clock = clock

    async def claim_one(self) -> str | None:
        now = self.clock()
        async with self.session_factory() as session:
            async with claim_serialization(session):
                statement = (
                    select(DBInstanceResource)
                    .where(
                        or_(
                            DBInstanceResource.status == DBInstanceStatus.CREATING,
                            DBInstanceResource.status == DBInstanceStatus.DELETING,
                            and_(
                                DBInstanceResource.status == DBInstanceStatus.FAILED,
                                DBInstanceResource.cleanup_required.is_(True),
                            ),
                        ),
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
                    .order_by(
                        DBInstanceResource.created_at,
                        DBInstanceResource.id,
                    )
                    .limit(1)
                )
                if dialect_name(session) != "sqlite":
                    statement = statement.with_for_update(skip_locked=True)
                resource = (await session.execute(statement)).scalar_one_or_none()
                if resource is None:
                    await session.rollback()
                    return None
                resource.worker_id = self.worker_id
                resource.worker_lease_until = now + timedelta(
                    seconds=self.config.worker_claim_ttl_seconds
                )
                await self._before_claim_commit(session, resource)
                await session.commit()
                return resource.id

    async def _before_claim_commit(
        self,
        session: AsyncSession,
        resource: DBInstanceResource,
    ) -> None:
        """Test seam for deterministic claim/mutation race coverage."""

    async def renew_claim(self, resource_id: str) -> bool:
        async with self.session_factory() as session:
            async with claim_serialization(session):
                resource = await session.get(DBInstanceResource, resource_id)
                if resource is None or resource.worker_id != self.worker_id:
                    await session.rollback()
                    return False
                resource.worker_lease_until = self.clock() + timedelta(
                    seconds=self.config.worker_claim_ttl_seconds
                )
                await session.commit()
                return True

    async def heartbeat(self, resource_id: str) -> None:
        while True:
            await asyncio.sleep(self.config.worker_claim_renew_seconds)
            if not await self.renew_claim(resource_id):
                return

    def backoff(self, retry_count: int) -> timedelta:
        seconds = min(
            self.config.worker_initial_backoff_seconds * (2 ** (retry_count - 1)),
            self.config.worker_max_backoff_seconds,
        )
        return timedelta(seconds=seconds)
