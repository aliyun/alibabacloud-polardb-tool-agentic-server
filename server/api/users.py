from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import user_manager
from server.db.engine import get_session
from server.models import Instance, InstanceStatus, User, UserRole, UserStatus

router = APIRouter(prefix="/users", tags=["users"])


class UserResponse(BaseModel):
    id: str
    external_id: str
    display_name: str
    email: str | None
    role: str
    status: str
    departments: list[dict] = []

    @classmethod
    def from_model(cls, user: User) -> "UserResponse":
        departments = []
        for m in (user.department_memberships or []):
            departments.append({
                "id": m.department_id,
                "name": m.department.name if m.department else "",
                "is_primary": m.is_primary,
            })
        return cls(
            id=user.id,
            external_id=user.external_id,
            display_name=user.display_name,
            email=user.email,
            role=user.role.value,
            status=user.status.value,
            departments=departments,
        )


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int
    offset: int
    limit: int


class UpdateUserRequest(BaseModel):
    role: str | None = None
    department_ids: list[str] | None = None
    primary_department_id: str | None = None


class CreateUserRequest(BaseModel):
    username: str
    password: str
    display_name: str | None = None
    email: str | None = None
    role: str = "member"
    department_ids: list[str] | None = None
    primary_department_id: str | None = None


@router.post("", response_model=UserResponse, status_code=201)
async def create_user(
    body: CreateUserRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Admin creates a builtin user."""
    from server.auth.builtin import hash_password
    from server.models import AuthProvider

    existing = await session.execute(
        select(User).where(User.external_id == body.username)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"User '{body.username}' already exists")

    user = User(
        external_id=body.username,
        display_name=body.display_name or body.username,
        email=body.email,
        auth_provider=AuthProvider.BUILTIN,
        password_hash=hash_password(body.password),
        role=UserRole(body.role),
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    if body.department_ids:
        await user_manager.update_user_departments(
            session, user.id, body.department_ids, body.primary_department_id
        )
        await session.refresh(user)

    return UserResponse.from_model(user)


@router.get("", response_model=UserListResponse)
async def list_users(
    search: str | None = None,
    department_id: str | None = None,
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    users, total = await user_manager.list_users(
        session, search=search, department_id=department_id, status=status, offset=offset, limit=limit
    )
    return UserListResponse(
        items=[UserResponse.from_model(u) for u in users],
        total=total, offset=offset, limit=limit,
    )


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await user_manager.get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse.from_model(user)


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    body: UpdateUserRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if body.role is not None:
        await user_manager.update_user_role(session, user_id, UserRole(body.role))
    if body.department_ids is not None:
        await user_manager.update_user_departments(
            session, user_id, body.department_ids, body.primary_department_id
        )
    user = await user_manager.get_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse.from_model(user)


@router.put("/{user_id}/disable", response_model=UserResponse)
async def disable_user(
    user_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await user_manager.set_user_status(session, user_id, UserStatus.DISABLED)
    return UserResponse.from_model(user)


@router.put("/{user_id}/enable", response_model=UserResponse)
async def enable_user(
    user_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await user_manager.set_user_status(session, user_id, UserStatus.ACTIVE)
    return UserResponse.from_model(user)


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.put("/{user_id}/reset-password")
async def reset_password(
    user_id: str,
    body: ResetPasswordRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Admin force-resets a builtin user's password."""
    from server.auth.builtin import hash_password
    from server.models import AuthProvider

    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if user.auth_provider != AuthProvider.BUILTIN:
        raise HTTPException(400, "Password reset is only available for builtin auth users.")
    if len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters.")

    user.password_hash = hash_password(body.new_password)
    await session.commit()
    return {"message": "Password reset successfully"}


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    force: bool = False,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(404, "User not found")

    owned = (await session.execute(
        select(func.count()).select_from(Instance).where(
            Instance.owner_user_id == user_id,
            Instance.status.in_([InstanceStatus.CREATING, InstanceStatus.ACTIVE, InstanceStatus.STOPPED]),
        )
    )).scalar() or 0

    if owned > 0 and not force:
        raise HTTPException(409, detail={
            "error": "USER_OWNS_INSTANCES",
            "message": f"User owns {owned} instance(s). Set force=true to delete anyway.",
            "owned_count": owned,
        })

    await session.delete(user)
    await session.commit()
