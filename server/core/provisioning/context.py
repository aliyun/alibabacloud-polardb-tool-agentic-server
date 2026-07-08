# server/core/provisioning/context.py
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.models import Instance


@dataclass
class ProvisioningContext:
    """Execution context shared between provisioning states."""

    instance: Instance
    session: AsyncSession
    session_factory: async_sessionmaker[AsyncSession]
    instance_id: str
    user_id: str
    start_time: float
    last_exception: BaseException | None = None
