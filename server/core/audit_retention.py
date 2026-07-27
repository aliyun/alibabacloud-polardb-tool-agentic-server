from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.config import AuditConfig
from server.models import AuditLog
from server.models.base import utc_now

logger = logging.getLogger(__name__)


async def sweep_expired_audit_logs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retention_days: int,
    batch_size: int,
    now: datetime | None = None,
) -> int:
    """Delete at most one oldest-first batch of expired audit rows."""
    cutoff = (now or utc_now()) - timedelta(days=retention_days)
    async with session_factory() as session:
        ids = (
            await session.execute(
                select(AuditLog.id)
                .where(AuditLog.created_at < cutoff)
                .order_by(AuditLog.created_at, AuditLog.id)
                .limit(batch_size)
            )
        ).scalars().all()
        if not ids:
            await session.rollback()
            return 0
        result = await session.execute(
            delete(AuditLog).where(AuditLog.id.in_(ids))
        )
        await session.commit()
        return max(0, int(result.rowcount or 0))  # type: ignore[attr-defined]


async def audit_retention_loop(
    session_factory: async_sessionmaker[AsyncSession],
    config: AuditConfig,
) -> None:
    """Run one bounded sweep per configured interval until cancelled."""
    while True:
        interval = config.cleanup_interval_seconds
        if interval == 0:
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(interval)
        try:
            current_retention = config.retention_days
            current_batch_size = config.cleanup_batch_size
            deleted = await sweep_expired_audit_logs(
                session_factory,
                retention_days=current_retention,
                batch_size=current_batch_size,
            )
            if deleted:
                logger.info(
                    "audit retention batch completed",
                    extra={"deleted_count": deleted},
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("audit retention batch failed")
