from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models.instance import AllocationMode, Instance
from server.models.quota_counter import QuotaCounter

logger = logging.getLogger(__name__)


async def check_and_increment_quota(
    session: AsyncSession, department_id: str | None
) -> dict | None:
    global_row = await session.execute(
        select(QuotaCounter).where(QuotaCounter.scope == "global").with_for_update()
    )
    global_counter = global_row.scalar_one()
    if global_counter.max_limit is not None and global_counter.current_count >= global_counter.max_limit:
        return {
            "error": "QUOTA_EXCEEDED", "level": "global",
            "limit": global_counter.max_limit, "current": global_counter.current_count,
        }

    if department_id:
        dept_row = await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == f"dept:{department_id}").with_for_update()
        )
        dept_counter = dept_row.scalar_one_or_none()
        if dept_counter and dept_counter.max_limit is not None:
            if dept_counter.current_count >= dept_counter.max_limit:
                return {
                    "error": "QUOTA_EXCEEDED", "level": "department",
                    "limit": dept_counter.max_limit, "current": dept_counter.current_count,
                }
            dept_counter.current_count += 1

    global_counter.current_count += 1
    return None


async def decrement_quota(session: AsyncSession, instance: Instance) -> None:
    if not instance.quota_held:
        return

    global_row = await session.execute(
        select(QuotaCounter).where(QuotaCounter.scope == "global").with_for_update()
    )
    global_counter = global_row.scalar_one()
    global_counter.current_count = max(0, global_counter.current_count - 1)

    dept_id = await get_owner_department_id(session, instance.owner_user_id)
    if dept_id:
        dept_row = await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == f"dept:{dept_id}").with_for_update()
        )
        dept_counter = dept_row.scalar_one_or_none()
        if dept_counter:
            dept_counter.current_count = max(0, dept_counter.current_count - 1)

    instance.quota_held = False


async def reincrement_quota_for_retry(
    session: AsyncSession, instance: Instance
) -> dict | None:
    if instance.quota_held:
        return None

    dept_id = await get_owner_department_id(session, instance.owner_user_id)
    error = await check_and_increment_quota(session, dept_id)
    if error:
        return error

    instance.quota_held = True
    return None


async def get_owner_department_id(
    session: AsyncSession, user_id: str | None
) -> str | None:
    if user_id is None:
        return None
    from server.models.binding import UserDepartment
    row = await session.execute(
        select(UserDepartment.department_id)
        .where(UserDepartment.user_id == user_id, UserDepartment.is_primary == True)  # noqa: E712
        .limit(1)
    )
    return row.scalar_one_or_none()


async def transfer_user_department(
    session: AsyncSession, user_id: str,
    old_dept_id: str | None, new_dept_id: str | None,
) -> None:
    personal = (await session.execute(
        select(Instance).where(
            Instance.owner_user_id == user_id,
            Instance.allocation_mode.in_(
                [AllocationMode.AUTO_PROVISIONED, AllocationMode.POOLED]
            ),
            Instance.quota_held == True,  # noqa: E712
        )
    )).scalars().all()
    count = len(personal)
    if count == 0:
        return

    if old_dept_id:
        old_row = await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == f"dept:{old_dept_id}").with_for_update()
        )
        old_counter = old_row.scalar_one_or_none()
        if old_counter:
            old_counter.current_count = max(0, old_counter.current_count - count)

    if new_dept_id:
        new_row = await session.execute(
            select(QuotaCounter).where(QuotaCounter.scope == f"dept:{new_dept_id}").with_for_update()
        )
        new_counter = new_row.scalar_one_or_none()
        if new_counter:
            new_counter.current_count += count
            if new_counter.max_limit is not None and new_counter.current_count > new_counter.max_limit:
                logger.warning(
                    "department transfer causes quota overage",
                    extra={"dept_id": new_dept_id, "current": new_counter.current_count, "limit": new_counter.max_limit},
                )
