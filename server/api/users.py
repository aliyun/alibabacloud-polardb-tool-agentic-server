from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.core import user_manager
from server.db.engine import get_session
from server.enterprise_identity.service import identity_provider_key
from server.models import (
    AuthProvider,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseIdentitySource,
    Instance,
    InstanceStatus,
    User,
    UserExternalIdentity,
    UserRole,
    UserStatus,
)

router = APIRouter(prefix="/users", tags=["users"])


class UserResponse(BaseModel):
    id: str
    external_id: str
    display_name: str
    auth_provider: str
    email: str | None
    role: str
    status: str
    departments: list[dict] = []
    identity_sources: list[dict[str, str]] = []
    enterprise_identities: list[dict[str, object]] = []

    @classmethod
    def from_model(
        cls,
        user: User,
        *,
        identity_sources: list[dict[str, str]] | None = None,
        enterprise_identities: list[dict[str, object]] | None = None,
    ) -> "UserResponse":
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
            auth_provider=user.auth_provider.value,
            email=user.email,
            role=user.role.value,
            status=user.status.value,
            departments=departments,
            identity_sources=identity_sources or [],
            enterprise_identities=enterprise_identities or [],
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


async def _enterprise_sources_by_user(
    session: AsyncSession,
    user_ids: list[str],
) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {
        user_id: [] for user_id in user_ids
    }
    if not user_ids:
        return result
    sources = list(
        (
            await session.execute(
                select(EnterpriseIdentitySource).order_by(EnterpriseIdentitySource.name)
            )
        ).scalars()
    )
    users = list(
        (
            await session.execute(
                select(User.id, User.external_id, User.auth_provider).where(
                    User.id.in_(user_ids)
                )
            )
        ).all()
    )
    for source in sources:
        prefix = f"{identity_provider_key(source)}:"
        for user_id, external_id, auth_provider in users:
            if auth_provider == AuthProvider.OIDC and external_id.startswith(prefix):
                result[user_id].append(
                    {
                        "id": source.id,
                        "name": source.name,
                        "provider": source.provider.value,
                    }
                )
    return result


async def _enterprise_identities_by_user(
    session: AsyncSession,
    user_ids: list[str],
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {
        user_id: [] for user_id in user_ids
    }
    if not user_ids:
        return result

    sources = list(
        (
            await session.execute(
                select(EnterpriseIdentitySource).order_by(EnterpriseIdentitySource.name)
            )
        ).scalars()
    )
    sources_by_provider = {
        identity_provider_key(source): source for source in sources
    }
    identities = list(
        (
            await session.execute(
                select(UserExternalIdentity)
                .where(UserExternalIdentity.user_id.in_(user_ids))
                .order_by(
                    UserExternalIdentity.identity_provider,
                    UserExternalIdentity.external_subject,
                )
            )
        ).scalars()
    )
    resolved: list[tuple[str, EnterpriseIdentitySource, str]] = []
    for identity in identities:
        source = sources_by_provider.get(identity.identity_provider)
        if source is not None:
            resolved.append((identity.user_id, source, identity.external_subject))
    resolved_keys = {
        (user_id, source.id, external_user_id)
        for user_id, source, external_user_id in resolved
    }
    source_users = list(
        (
            await session.execute(
                select(User.id, User.external_id, User.auth_provider).where(
                    User.id.in_(user_ids)
                )
            )
        ).all()
    )
    for user_id, external_id, auth_provider in source_users:
        if auth_provider != AuthProvider.OIDC:
            continue
        for provider_key, source in sources_by_provider.items():
            prefix = f"{provider_key}:"
            if external_id.startswith(prefix):
                external_user_id = external_id.removeprefix(prefix)
                key = (user_id, source.id, external_user_id)
                if key not in resolved_keys:
                    resolved.append((user_id, source, external_user_id))
                break
    source_ids = {source.id for _user_id, source, _external_user_id in resolved}
    external_user_ids = {
        external_user_id for _user_id, _source, external_user_id in resolved
    }
    memberships = []
    if source_ids and external_user_ids:
        memberships = list(
            (
                await session.execute(
                    select(EnterpriseDirectoryMembership).where(
                        EnterpriseDirectoryMembership.identity_source_id.in_(source_ids),
                        EnterpriseDirectoryMembership.member_type
                        == EnterpriseDirectoryMembershipType.USER,
                        EnterpriseDirectoryMembership.external_member_id.in_(external_user_ids),
                    )
                )
            ).scalars()
        )
    group_ids = {membership.external_group_id for membership in memberships}
    groups_by_id: dict[tuple[str, str], EnterpriseDirectoryGroup] = {}
    if source_ids and group_ids:
        directory_groups = list(
            (
                await session.execute(
                    select(EnterpriseDirectoryGroup).where(
                        EnterpriseDirectoryGroup.identity_source_id.in_(source_ids),
                        EnterpriseDirectoryGroup.external_group_id.in_(group_ids),
                        EnterpriseDirectoryGroup.status
                        == EnterpriseDirectoryEntryStatus.ACTIVE,
                    )
                )
            ).scalars()
        )
        groups_by_id = {
            (group.identity_source_id, group.external_group_id): group
            for group in directory_groups
        }

    memberships_by_user: dict[tuple[str, str], list[EnterpriseDirectoryGroup]] = {}
    for membership in memberships:
        group = groups_by_id.get(
            (membership.identity_source_id, membership.external_group_id)
        )
        if group is not None:
            memberships_by_user.setdefault(
                (membership.identity_source_id, membership.external_member_id), []
            ).append(group)

    for user_id, source, external_user_id in resolved:
        departments: list[dict[str, str]] = []
        group_items: list[dict[str, str]] = []
        for group in memberships_by_user.get((source.id, external_user_id), []):
            item = {"id": group.external_group_id, "name": group.display_name}
            if group.principal_type == EnterpriseDirectoryPrincipalType.DEPARTMENT:
                departments.append(item)
            else:
                group_items.append(item)
        departments.sort(key=lambda item: (item["name"], item["id"]))
        group_items.sort(key=lambda item: (item["name"], item["id"]))
        result[user_id].append(
            {
                "id": source.id,
                "name": source.name,
                "provider": source.provider.value,
                "external_user_id": external_user_id,
                "departments": departments,
                "groups": group_items,
            }
        )
    return result


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
    identity_sources = await _enterprise_sources_by_user(
        session,
        [user.id for user in users],
    )
    enterprise_identities = await _enterprise_identities_by_user(
        session,
        [user.id for user in users],
    )
    return UserListResponse(
        items=[
            UserResponse.from_model(
                user,
                identity_sources=identity_sources[user.id],
                enterprise_identities=enterprise_identities[user.id],
            )
            for user in users
        ],
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
