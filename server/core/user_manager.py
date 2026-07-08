from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import User, UserDepartment, UserRole, UserStatus

logger = logging.getLogger(__name__)


async def list_users(
    session: AsyncSession,
    *,
    search: str | None = None,
    department_id: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> tuple[Sequence[User], int]:
    """List users with optional filters. Returns (users, total_count)."""
    query = select(User)
    count_query = select(func.count()).select_from(User)

    if search:
        pattern = f"%{search}%"
        filter_clause = or_(
            User.display_name.ilike(pattern),
            User.email.ilike(pattern),
            User.external_id.ilike(pattern),
        )
        query = query.where(filter_clause)
        count_query = count_query.where(filter_clause)

    if department_id:
        query = query.join(UserDepartment).where(UserDepartment.department_id == department_id)
        count_query = select(func.count()).select_from(User).join(UserDepartment).where(
            UserDepartment.department_id == department_id
        )

    if status:
        query = query.where(User.status == status)
        count_query = count_query.where(User.status == status)

    total = (await session.execute(count_query)).scalar() or 0
    result = await session.execute(query.offset(offset).limit(limit).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return users, total


async def get_user(session: AsyncSession, user_id: str) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def update_user_role(session: AsyncSession, user_id: str, role: UserRole) -> User:
    user = await get_user(session, user_id)
    if user is None:
        raise ValueError("User not found")
    user.role = role
    await session.commit()
    await session.refresh(user)
    return user


async def set_user_status(session: AsyncSession, user_id: str, status: UserStatus) -> User:
    user = await get_user(session, user_id)
    if user is None:
        raise ValueError("User not found")
    user.status = status
    await session.commit()
    await session.refresh(user)
    return user


async def update_user_departments(
    session: AsyncSession, user_id: str, department_ids: list[str], primary_department_id: str | None = None
) -> User:
    """Replace user's department memberships."""
    user = await get_user(session, user_id)
    if user is None:
        raise ValueError("User not found")

    # Remove existing memberships
    for membership in list(user.department_memberships):
        await session.delete(membership)
    await session.flush()

    # Add new memberships
    for dept_id in department_ids:
        ud = UserDepartment(
            user_id=user_id,
            department_id=dept_id,
            is_primary=(dept_id == primary_department_id),
        )
        session.add(ud)

    await session.commit()
    await session.refresh(user)
    return user
