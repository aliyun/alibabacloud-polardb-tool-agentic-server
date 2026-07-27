from __future__ import annotations

import logging
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import Department, UserDepartment, DepartmentInstanceBinding, User

logger = logging.getLogger(__name__)


async def list_departments(session: AsyncSession) -> Sequence[Department]:
    result = await session.execute(select(Department).order_by(Department.name))
    return result.scalars().all()


async def get_department(session: AsyncSession, department_id: str) -> Department | None:
    result = await session.execute(select(Department).where(Department.id == department_id))
    return result.scalar_one_or_none()


async def create_department(session: AsyncSession, name: str, description: str | None = None) -> Department:
    dept = Department(name=name, description=description)
    session.add(dept)
    await session.commit()
    await session.refresh(dept)
    return dept


async def update_department(session: AsyncSession, department_id: str, name: str | None = None, description: str | None = None) -> Department:
    dept = await get_department(session, department_id)
    if dept is None:
        raise ValueError("Department not found")
    if name is not None:
        dept.name = name
    if description is not None:
        dept.description = description
    await session.commit()
    await session.refresh(dept)
    return dept


async def delete_department(session: AsyncSession, department_id: str) -> None:
    dept = await get_department(session, department_id)
    if dept is None:
        raise ValueError("Department not found")

    # Check for active users
    user_count = (await session.execute(
        select(func.count()).select_from(UserDepartment).where(UserDepartment.department_id == department_id)
    )).scalar() or 0
    if user_count > 0:
        raise ValueError(f"Cannot delete department with {user_count} active user(s)")

    # Check for instance bindings
    binding_count = (await session.execute(
        select(func.count()).select_from(DepartmentInstanceBinding).where(
            DepartmentInstanceBinding.department_id == department_id
        )
    )).scalar() or 0
    if binding_count > 0:
        raise ValueError(f"Cannot delete department with {binding_count} instance binding(s)")

    await session.delete(dept)
    await session.commit()


async def list_department_users(session: AsyncSession, department_id: str) -> Sequence[User]:
    result = await session.execute(
        select(User).join(UserDepartment).where(UserDepartment.department_id == department_id)
    )
    return result.scalars().all()
