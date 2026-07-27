"""Instance provisioning orchestrator.

Coordinates cross-module operations for instance allocation and provisioning.
Provides a higher-level API that MCP tools and API routes can call instead of
directly invoking multiple core submodules (pool_manager + quota_manager +
provisioner).
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.core.pool_manager import allocate_from_pool, create_placeholder_instance
from server.core.provisioner import _launch_provisioning_task, resolve_provisioning_mode
from server.core.quota_manager import check_and_increment_quota, get_owner_department_id
from server.models import Instance, User
from server.models.user import ProvisioningMode

logger = logging.getLogger(__name__)


async def provision_personal_instance(
    user: User,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    background_tasks: set[asyncio.Task],
) -> Instance | dict:
    """Allocate or create a personal instance for the user, then launch provisioning.

    For DEDICATED users: requires primary department, checks quota, creates placeholder
    directly (skips local pool). For other modes: uses existing pool allocation path.
    """
    department_id = await get_owner_department_id(session, user.id)
    mode = resolve_provisioning_mode(user)

    if mode == ProvisioningMode.DEDICATED:
        if not department_id:
            return {
                "error": "NO_PRIMARY_DEPARTMENT",
                "message": "Dedicated user must belong to a department before provisioning.",
            }
        quota_error = await check_and_increment_quota(session, department_id)
        if quota_error:
            return quota_error
        instance = await create_placeholder_instance(user.id, session)
    else:
        result = await allocate_from_pool(user.id, department_id, session)
        if isinstance(result, dict):
            return result
        instance = result

    logger.info(
        "provisioning.started",
        extra={
            "metric": "provisioning.started",
            "user_id": user.id,
            "instance_id": instance.id,
        },
    )
    _launch_provisioning_task(instance.id, user.id, session_factory, background_tasks)
    return instance
