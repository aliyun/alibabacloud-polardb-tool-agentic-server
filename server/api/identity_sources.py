from __future__ import annotations

import json
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.config import get_config
from server.core.audit_logger import log_audit
from server.core.crypto import decrypt, encrypt
from server.db.engine import get_session
from server.enterprise_identity.feishu_tenant_verification import (
    build_feishu_authorization_url,
    feishu_tenant_verification_callback_url,
)
from server.models import (
    AuditStatus,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    EnterpriseIdentitySourceSpaceBinding,
    FeishuTenantVerificationState,
    IdentitySourceProvider,
    PolarRAGSpace,
    User,
    UserExternalIdentity,
    AuthProvider,
    UserRole,
    UserStatus,
)
from server.enterprise_identity.service import identity_provider_key, sync_identity_source


router = APIRouter(prefix="/identity-sources", tags=["identity-sources"])


class IdentitySourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    provider: Literal["feishu", "sharepoint"]
    tenant_id: str | None = Field(default=None, min_length=1, max_length=255)
    app_id: str | None = Field(default=None, max_length=255)
    app_secret: str | None = Field(default=None, max_length=4096)
    cloud: Literal["global", "china"] = "global"
    client_id: str | None = Field(default=None, max_length=255)
    client_secret: str | None = Field(default=None, max_length=4096)
    stale_after_seconds: int = Field(default=1800, ge=60, le=86400)

    @field_validator("name", "tenant_id", "app_id", "client_id")
    @classmethod
    def validate_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("app_secret", "client_secret")
    @classmethod
    def validate_secret(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def validate_provider_credentials(self) -> "IdentitySourceCreate":
        if self.provider == "feishu":
            if not self.app_id or not self.app_secret:
                raise ValueError("app_id and app_secret are required for Feishu")
            if self.tenant_id is not None:
                raise ValueError("tenant_id is discovered during Feishu verification")
        elif not self.tenant_id or not self.client_id or not self.client_secret:
            raise ValueError(
                "client_id and client_secret are required for SharePoint"
            )
        return self


class IdentitySourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    app_id: str | None = Field(default=None, max_length=255)
    app_secret: str | None = Field(default=None, max_length=4096)
    cloud: Literal["global", "china"] = "global"
    client_id: str | None = Field(default=None, max_length=255)
    client_secret: str | None = Field(default=None, max_length=4096)

    @field_validator("name", "app_id", "client_id")
    @classmethod
    def validate_nonblank(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("app_secret", "client_secret")
    @classmethod
    def validate_secret(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must not be blank")
        return value


class AclMembershipSnapshotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=3306, ge=1, le=65535)
    database: str = Field(default="polar_rag_meta", min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=4096)

    @field_validator("host", "database", "username", "password")
    @classmethod
    def validate_snapshot_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class EnterpriseIdentityMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_source_id: str = Field(min_length=1, max_length=36)
    external_user_id: str = Field(min_length=1, max_length=255)

    @field_validator("identity_source_id", "external_user_id")
    @classmethod
    def validate_mapping_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


def _acl_membership_snapshot_configured(source: EnterpriseIdentitySource) -> bool:
    if not source.config_ciphertext:
        return False
    try:
        config = json.loads(decrypt(source.config_ciphertext))
    except Exception:
        return False
    return isinstance(config, dict) and isinstance(
        config.get("acl_membership_snapshot"), dict
    )


def _sharepoint_cloud(source: EnterpriseIdentitySource) -> str | None:
    if source.provider != IdentitySourceProvider.SHAREPOINT or not source.config_ciphertext:
        return None
    try:
        config = json.loads(decrypt(source.config_ciphertext))
    except Exception:
        return None
    cloud = config.get("cloud") if isinstance(config, dict) else None
    return cloud if cloud in {"global", "china"} else "global"


async def _identity_principals(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    external_user_id: str,
) -> list[dict[str, str]]:
    principals: set[tuple[str, str, str]] = {
        (source.provider.value, "user", external_user_id)
    }
    frontier = {external_user_id}
    member_type = EnterpriseDirectoryMembershipType.USER
    visited_groups: set[str] = set()
    for _depth in range(8):
        if not frontier:
            break
        rows = (
            await session.execute(
                select(
                    EnterpriseDirectoryGroup.external_group_id,
                    EnterpriseDirectoryGroup.principal_type,
                )
                .select_from(EnterpriseDirectoryMembership)
                .join(
                    EnterpriseDirectoryGroup,
                    (
                        EnterpriseDirectoryGroup.identity_source_id
                        == EnterpriseDirectoryMembership.identity_source_id
                    )
                    & (
                        EnterpriseDirectoryGroup.external_group_id
                        == EnterpriseDirectoryMembership.external_group_id
                    ),
                )
                .where(
                    EnterpriseDirectoryMembership.identity_source_id == source.id,
                    EnterpriseDirectoryMembership.member_type == member_type,
                    EnterpriseDirectoryMembership.external_member_id.in_(frontier),
                    EnterpriseDirectoryGroup.status == EnterpriseDirectoryEntryStatus.ACTIVE,
                )
            )
        ).all()
        groups = {
            (str(group_id), principal_type.value)
            for group_id, principal_type in rows
            if str(group_id) not in visited_groups
        }
        principals.update(
            (source.provider.value, principal_type, group_id)
            for group_id, principal_type in groups
        )
        frontier = {group_id for group_id, _principal_type in groups}
        visited_groups.update(frontier)
        member_type = EnterpriseDirectoryMembershipType.GROUP
    return [
        {"provider": provider, "type": principal_type, "id": principal_id}
        for provider, principal_type, principal_id in sorted(principals)
    ]


async def _identity_mapping_response(
    session: AsyncSession,
    identity: UserExternalIdentity,
    source: EnterpriseIdentitySource,
) -> dict[str, object]:
    directory_user = (
        await session.execute(
            select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                EnterpriseDirectoryUser.external_user_id == identity.external_subject,
            )
        )
    ).scalar_one_or_none()
    if directory_user is None:
        raise HTTPException(status_code=409, detail="Enterprise identity is no longer synchronized")
    user = await session.get(User, identity.user_id)
    if user is None:
        raise HTTPException(status_code=409, detail="Enterprise identity references a missing PAS user")
    default_external_id = f"{identity_provider_key(source)}:{identity.external_subject}"
    return {
        "id": identity.id,
        "identity_source_id": source.id,
        "source_name": source.name,
        "provider": source.provider.value,
        "external_user_id": identity.external_subject,
        "display_name": directory_user.display_name,
        "email": directory_user.email,
        "status": directory_user.status.value,
        "mapping_mode": (
            "synced" if user.external_id == default_external_id else "pas_managed"
        ),
        "native_principal_id": user.external_id,
        "principals": await _identity_principals(
            session,
            source,
            identity.external_subject,
        ),
    }


async def _source_for_mapping(
    session: AsyncSession,
    identity_source_id: str,
    external_user_id: str,
) -> tuple[EnterpriseIdentitySource, EnterpriseDirectoryUser]:
    source = await session.get(EnterpriseIdentitySource, identity_source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    directory_user = (
        await session.execute(
            select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                EnterpriseDirectoryUser.external_user_id == external_user_id,
                EnterpriseDirectoryUser.status == EnterpriseDirectoryEntryStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if directory_user is None:
        raise HTTPException(status_code=404, detail="Synchronized enterprise user not found")
    return source, directory_user


async def _default_source_user(
    session: AsyncSession,
    source: EnterpriseIdentitySource,
    directory_user: EnterpriseDirectoryUser,
) -> User:
    external_id = f"{identity_provider_key(source)}:{directory_user.external_user_id}"
    user = (
        await session.execute(select(User).where(User.external_id == external_id))
    ).scalar_one_or_none()
    if user is not None:
        return user
    user = User(
        external_id=external_id,
        display_name=directory_user.display_name,
        email=directory_user.email,
        auth_provider=AuthProvider.OIDC,
        role=UserRole.MEMBER,
        status=UserStatus.ACTIVE,
    )
    session.add(user)
    await session.flush()
    return user


def _source_response(
    source: EnterpriseIdentitySource,
    *,
    space_bindings: list[str],
) -> dict:
    response: dict[str, object] = {
        "id": source.id,
        "name": source.name,
        "provider": source.provider.value,
        "tenant_id": source.tenant_id,
        "status": source.status.value,
        "last_synced_at": source.last_synced_at,
        "last_error": source.last_error,
        "sync_supported": source.provider in {
            IdentitySourceProvider.FEISHU,
            IdentitySourceProvider.SHAREPOINT,
        },
        "acl_membership_snapshot_configured": _acl_membership_snapshot_configured(source),
        "space_bindings": space_bindings,
    }
    cloud = _sharepoint_cloud(source)
    if cloud is not None:
        response["cloud"] = cloud
    return response


async def _commit_with_audit(
    session: AsyncSession,
    *,
    user_id: str,
    action: str,
    target_type: str,
    target_id: str,
) -> None:
    await log_audit(
        session,
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        status=AuditStatus.SUCCESS,
        required=True,
        commit=False,
    )
    await session.commit()


@router.post("", status_code=201)
async def create_identity_source(
    body: IdentitySourceCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = EnterpriseIdentitySource.create(
        name=body.name,
        provider=IdentitySourceProvider(body.provider),
        tenant_id=body.tenant_id,
    )
    if source.provider == IdentitySourceProvider.FEISHU and source.tenant_id is None:
        source.status = EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION
    source.stale_after_seconds = body.stale_after_seconds
    source.config_ciphertext = encrypt(
        json.dumps(
            (
                {"app_id": body.app_id, "app_secret": body.app_secret}
                if source.provider == IdentitySourceProvider.FEISHU
                else {
                    "cloud": body.cloud,
                    "client_id": body.client_id,
                    "client_secret": body.client_secret,
                }
            )
        )
    )
    session.add(source)
    try:
        await session.flush()
        await _commit_with_audit(
            session,
            user_id=admin.id,
            action="identity_source.create",
            target_type="enterprise_identity_source",
            target_id=source.id,
        )
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail="Identity source already exists") from exc
    return _source_response(source, space_bindings=[])


def _feishu_verification_callback_url() -> str:
    try:
        return feishu_tenant_verification_callback_url(
            get_config().server.public_base_url
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{source_id}/feishu-verification")
async def start_feishu_tenant_verification(
    source_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    if (
        source.provider != IdentitySourceProvider.FEISHU
        or source.status
        != EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION
        or source.tenant_id is not None
    ):
        raise HTTPException(
            status_code=409,
            detail="Feishu tenant verification is not available for this source",
        )
    if not source.config_ciphertext:
        raise HTTPException(status_code=409, detail="Identity source configuration is missing")
    try:
        config = json.loads(decrypt(source.config_ciphertext))
    except Exception:
        raise HTTPException(status_code=409, detail="Identity source configuration is invalid") from None
    app_id = config.get("app_id") if isinstance(config, dict) else None
    if not isinstance(app_id, str) or not app_id:
        raise HTTPException(status_code=409, detail="Identity source configuration is invalid")

    state = secrets.token_urlsafe(32)
    state_record = FeishuTenantVerificationState(
        identity_source_id=source.id,
        created_by_user_id=admin.id,
        state_hash=hashlib.sha256(state.encode()).hexdigest(),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    session.add(state_record)
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.feishu_verification_start",
        target_type="enterprise_identity_source",
        target_id=source.id,
    )
    callback_url = _feishu_verification_callback_url()
    return {
        "authorization_url": build_feishu_authorization_url(
            app_id=app_id,
            redirect_uri=callback_url,
            state=state,
        )
    }


@router.get("")
async def list_identity_sources(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    sources = list(
        (
            await session.execute(
                select(EnterpriseIdentitySource).order_by(EnterpriseIdentitySource.name)
            )
        ).scalars()
    )
    bindings = list(
        (
            await session.execute(
                select(EnterpriseIdentitySourceSpaceBinding).where(
                    EnterpriseIdentitySourceSpaceBinding.identity_source_id.in_(
                        [source.id for source in sources] or [""]
                    )
                )
            )
        ).scalars()
    )
    bindings_by_source: dict[str, list[str]] = {}
    for binding in bindings:
        bindings_by_source.setdefault(binding.identity_source_id, []).append(
            binding.knowledge_space_id
        )
    return {
        "items": [
            _source_response(
                source,
                space_bindings=sorted(bindings_by_source.get(source.id, [])),
            )
            for source in sources
        ]
    }


@router.post("/{source_id}/sync")
async def sync_source_now(
    source_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    if source.provider not in {
        IdentitySourceProvider.FEISHU,
        IdentitySourceProvider.SHAREPOINT,
    }:
        raise HTTPException(status_code=409, detail="Identity source synchronization is not supported")
    if source.tenant_id is None or source.config_ciphertext is None:
        raise HTTPException(status_code=409, detail="Identity source is not ready for synchronization")
    try:
        await sync_identity_source(session, source)
    except Exception:
        await session.commit()
        raise HTTPException(
            status_code=502,
            detail="Identity source synchronization failed",
        ) from None
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.sync",
        target_type="enterprise_identity_source",
        target_id=source.id,
    )
    bindings = list(
        (
            await session.execute(
                select(EnterpriseIdentitySourceSpaceBinding.knowledge_space_id).where(
                    EnterpriseIdentitySourceSpaceBinding.identity_source_id == source.id
                )
            )
        ).scalars()
    )
    return _source_response(source, space_bindings=sorted(bindings))


@router.put("/{source_id}")
async def update_identity_source(
    source_id: str,
    body: IdentitySourceUpdate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    if source.provider == IdentitySourceProvider.FEISHU:
        if not body.app_id or not body.app_secret:
            raise HTTPException(
                status_code=422,
                detail="app_id and app_secret are required for Feishu",
            )
        config = {"app_id": body.app_id, "app_secret": body.app_secret}
        source.tenant_id = None
        source.status = EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION
    else:
        if not body.client_id or not body.client_secret:
            raise HTTPException(
                status_code=422,
                detail="client_id and client_secret are required for SharePoint",
            )
        config = {
            "cloud": body.cloud,
            "client_id": body.client_id,
            "client_secret": body.client_secret,
        }
        source.status = EnterpriseIdentitySourceStatus.PENDING_BINDING
    source.name = body.name
    source.config_ciphertext = encrypt(json.dumps(config))
    source.last_synced_at = None
    source.last_error = None
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.update",
        target_type="enterprise_identity_source",
        target_id=source.id,
    )
    bindings = list(
        (
            await session.execute(
                select(EnterpriseIdentitySourceSpaceBinding.knowledge_space_id).where(
                    EnterpriseIdentitySourceSpaceBinding.identity_source_id == source.id
                )
            )
        ).scalars()
    )
    return _source_response(source, space_bindings=sorted(bindings))


@router.delete("/{source_id}", status_code=204)
async def delete_identity_source(
    source_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    await session.delete(source)
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.delete",
        target_type="enterprise_identity_source",
        target_id=source_id,
    )


@router.get("/users/{user_id}/identities")
async def list_user_enterprise_identities(
    user_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if await session.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    sources = list(
        (
            await session.execute(
                select(EnterpriseIdentitySource).order_by(EnterpriseIdentitySource.name)
            )
        ).scalars()
    )
    items: list[dict[str, object]] = []
    for source in sources:
        identities = list(
            (
                await session.execute(
                    select(UserExternalIdentity).where(
                        UserExternalIdentity.user_id == user_id,
                        UserExternalIdentity.identity_provider
                        == identity_provider_key(source),
                    )
                )
            ).scalars()
        )
        for identity in identities:
            items.append(await _identity_mapping_response(session, identity, source))
    return {"items": items}


@router.post("/users/{user_id}/identities", status_code=201)
async def create_user_enterprise_identity(
    user_id: str,
    body: EnterpriseIdentityMappingRequest,
    response: Response,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    source, directory_user = await _source_for_mapping(
        session,
        body.identity_source_id,
        body.external_user_id,
    )
    identity = (
        await session.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.identity_provider == identity_provider_key(source),
                UserExternalIdentity.external_subject == directory_user.external_user_id,
            )
        )
    ).scalar_one_or_none()
    if identity is None:
        default_user = await _default_source_user(session, source, directory_user)
        identity = UserExternalIdentity(
            user_id=default_user.id,
            identity_provider=identity_provider_key(source),
            external_subject=directory_user.external_user_id,
        )
        session.add(identity)
        await session.flush()
    if identity.user_id != user.id:
        current_user = await session.get(User, identity.user_id)
        expected_external_id = (
            f"{identity_provider_key(source)}:{directory_user.external_user_id}"
        )
        if current_user is None or current_user.external_id != expected_external_id:
            raise HTTPException(
                status_code=409,
                detail="Enterprise identity is already mapped to another PAS user",
            )
        identity.user_id = user.id
    else:
        response.status_code = 200
    await session.flush()
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.user_mapping_create",
        target_type="user_external_identity",
        target_id=identity.id,
    )
    return await _identity_mapping_response(session, identity, source)


@router.put("/users/{user_id}/identities/{identity_id}")
async def update_user_enterprise_identity(
    user_id: str,
    identity_id: str,
    body: EnterpriseIdentityMappingRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    current_identity = await session.get(UserExternalIdentity, identity_id)
    user = await session.get(User, user_id)
    if current_identity is None or current_identity.user_id != user_id or user is None:
        raise HTTPException(status_code=404, detail="Enterprise identity not found")
    current_source = next(
        (
            source
            for source in (
                await session.execute(select(EnterpriseIdentitySource))
            ).scalars()
            if current_identity.identity_provider == identity_provider_key(source)
        ),
        None,
    )
    if current_source is None:
        raise HTTPException(status_code=409, detail="Enterprise identity source is unavailable")
    if user.external_id == (
        f"{identity_provider_key(current_source)}:{current_identity.external_subject}"
    ):
        raise HTTPException(
            status_code=409,
            detail="Synchronized enterprise identity cannot be edited",
        )
    source, directory_user = await _source_for_mapping(
        session,
        body.identity_source_id,
        body.external_user_id,
    )
    replacement = (
        await session.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.identity_provider == identity_provider_key(source),
                UserExternalIdentity.external_subject == directory_user.external_user_id,
            )
        )
    ).scalar_one_or_none()
    if replacement is None:
        default_user = await _default_source_user(session, source, directory_user)
        replacement = UserExternalIdentity(
            user_id=default_user.id,
            identity_provider=identity_provider_key(source),
            external_subject=directory_user.external_user_id,
        )
        session.add(replacement)
        await session.flush()
    if replacement.id == current_identity.id:
        return await _identity_mapping_response(session, replacement, source)
    if replacement.user_id != user.id:
        mapped_user = await session.get(User, replacement.user_id)
        expected_external_id = (
            f"{identity_provider_key(source)}:{directory_user.external_user_id}"
        )
        if mapped_user is None or mapped_user.external_id != expected_external_id:
            raise HTTPException(
                status_code=409,
                detail="Enterprise identity is already mapped to another PAS user",
            )
        replacement.user_id = user.id
    current_directory_user = (
        await session.execute(
            select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.identity_source_id == current_source.id,
                EnterpriseDirectoryUser.external_user_id == current_identity.external_subject,
            )
        )
    ).scalar_one_or_none()
    if current_directory_user is None:
        raise HTTPException(status_code=409, detail="Enterprise identity is no longer synchronized")
    current_identity.user_id = (
        await _default_source_user(session, current_source, current_directory_user)
    ).id
    await session.flush()
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.user_mapping_update",
        target_type="user_external_identity",
        target_id=replacement.id,
    )
    return await _identity_mapping_response(session, replacement, source)


@router.delete("/users/{user_id}/identities/{identity_id}", status_code=204)
async def delete_user_enterprise_identity(
    user_id: str,
    identity_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    identity = await session.get(UserExternalIdentity, identity_id)
    if identity is None or identity.user_id != user_id:
        raise HTTPException(status_code=404, detail="Enterprise identity not found")
    source = next(
        (
            source
            for source in (
                await session.execute(select(EnterpriseIdentitySource))
            ).scalars()
            if identity.identity_provider == identity_provider_key(source)
        ),
        None,
    )
    if source is None:
        raise HTTPException(status_code=409, detail="Enterprise identity source is unavailable")
    directory_user = (
        await session.execute(
            select(EnterpriseDirectoryUser).where(
                EnterpriseDirectoryUser.identity_source_id == source.id,
                EnterpriseDirectoryUser.external_user_id == identity.external_subject,
            )
        )
    ).scalar_one_or_none()
    user = await session.get(User, user_id)
    if directory_user is None or user is None:
        raise HTTPException(status_code=409, detail="Enterprise identity is no longer synchronized")
    expected_external_id = f"{identity_provider_key(source)}:{identity.external_subject}"
    if user.external_id == expected_external_id:
        raise HTTPException(
            status_code=409,
            detail="Synchronized enterprise identity cannot be removed",
        )
    identity.user_id = (await _default_source_user(session, source, directory_user)).id
    await session.flush()
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.user_mapping_delete",
        target_type="user_external_identity",
        target_id=identity.id,
    )


@router.get("/{source_id}/directory")
async def list_identity_source_directory(
    source_id: str,
    entry_type: Literal["users", "groups", "all"] = Query("all"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    search: str | None = None,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    from server.models import EnterpriseDirectoryGroup, EnterpriseDirectoryUser

    query = search.strip() if search else None

    user_filters = [EnterpriseDirectoryUser.identity_source_id == source.id]
    group_filters = [EnterpriseDirectoryGroup.identity_source_id == source.id]
    if query:
        pattern = f"%{query}%"
        user_filters.append(
            or_(
                EnterpriseDirectoryUser.display_name.ilike(pattern),
                EnterpriseDirectoryUser.external_user_id.ilike(pattern),
                EnterpriseDirectoryUser.email.ilike(pattern),
            )
        )
        group_filters.append(
            or_(
                EnterpriseDirectoryGroup.display_name.ilike(pattern),
                EnterpriseDirectoryGroup.external_group_id.ilike(pattern),
            )
        )

    users: list[EnterpriseDirectoryUser] = []
    groups: list[EnterpriseDirectoryGroup] = []
    total: int | None = None
    if entry_type in {"users", "all"}:
        user_query = select(EnterpriseDirectoryUser).where(*user_filters)
        users = list(
            (
                await session.execute(
                    user_query
                    .order_by(EnterpriseDirectoryUser.display_name, EnterpriseDirectoryUser.external_user_id)
                    .offset(offset)
                    .limit(limit)
                )
            ).scalars()
        )
        user_total = await session.scalar(select(func.count()).select_from(user_query.subquery()))
        if entry_type == "users":
            total = int(user_total or 0)
    if entry_type in {"groups", "all"}:
        group_query = select(EnterpriseDirectoryGroup).where(*group_filters)
        groups = list(
            (
                await session.execute(
                    group_query
                    .order_by(EnterpriseDirectoryGroup.display_name, EnterpriseDirectoryGroup.external_group_id)
                    .offset(offset)
                    .limit(limit)
                )
            ).scalars()
        )
        group_total = await session.scalar(select(func.count()).select_from(group_query.subquery()))
        if entry_type == "groups":
            total = int(group_total or 0)
    identities: list[UserExternalIdentity] = []
    if users:
        identities = list(
            (
                await session.execute(
                    select(UserExternalIdentity).where(
                        UserExternalIdentity.identity_provider
                        == identity_provider_key(source),
                        UserExternalIdentity.external_subject.in_(
                            [user.external_user_id for user in users]
                        ),
                    )
                )
            ).scalars()
        )
    pas_user_by_external_subject = {
        identity.external_subject: identity.user_id for identity in identities
    }
    return {
        "users": [
            {
                "id": user.id,
                "pas_user_id": pas_user_by_external_subject.get(
                    user.external_user_id
                ),
                "external_user_id": user.external_user_id,
                "display_name": user.display_name,
                "email": user.email,
                "status": user.status.value,
            }
            for user in users
        ],
        "groups": [
            {
                "id": group.id,
                "external_group_id": group.external_group_id,
                "display_name": group.display_name,
                "principal_type": group.principal_type.value,
                "status": group.status.value,
            }
            for group in groups
        ],
        "total": total,
    }


@router.get("/spaces")
async def list_enabled_identity_source_spaces(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    rows = list(
        (
            await session.execute(
                select(PolarRAGSpace)
                .where(PolarRAGSpace.enabled.is_(True))
                .order_by(PolarRAGSpace.name, PolarRAGSpace.knowledge_space_id)
            )
        ).scalars()
    )
    return {
        "items": [
            {
                "knowledge_space_id": space.knowledge_space_id,
                "polarrag_instance_id": space.polarrag_instance_id,
                "name": space.name,
                "identity_domain": space.identity_domain,
            }
            for space in rows
        ]
    }


@router.post("/{source_id}/spaces/{knowledge_space_id}", status_code=201)
async def bind_identity_source_space(
    source_id: str,
    knowledge_space_id: str,
    response: Response,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    space = await session.get(PolarRAGSpace, knowledge_space_id)
    if space is None or not space.enabled:
        raise HTTPException(status_code=404, detail="Enabled PolarRAG Space not found")
    binding = (
        await session.execute(
            select(EnterpriseIdentitySourceSpaceBinding).where(
                EnterpriseIdentitySourceSpaceBinding.identity_source_id == source.id,
                EnterpriseIdentitySourceSpaceBinding.knowledge_space_id == space.knowledge_space_id,
            )
        )
    ).scalar_one_or_none()
    if binding is not None:
        response.status_code = 200
        return {
            "id": binding.id,
            "identity_source_id": binding.identity_source_id,
            "knowledge_space_id": binding.knowledge_space_id,
        }
    binding = EnterpriseIdentitySourceSpaceBinding(
        identity_source_id=source.id,
        knowledge_space_id=space.knowledge_space_id,
    )
    session.add(binding)
    await session.flush()
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.space_bind",
        target_type="enterprise_identity_source_space_binding",
        target_id=binding.id,
    )
    return {
        "id": binding.id,
        "identity_source_id": binding.identity_source_id,
        "knowledge_space_id": binding.knowledge_space_id,
    }


@router.delete("/{source_id}/spaces/{knowledge_space_id}", status_code=204)
async def unbind_identity_source_space(
    source_id: str,
    knowledge_space_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    binding = (
        await session.execute(
            select(EnterpriseIdentitySourceSpaceBinding).where(
                EnterpriseIdentitySourceSpaceBinding.identity_source_id == source.id,
                EnterpriseIdentitySourceSpaceBinding.knowledge_space_id == knowledge_space_id,
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        raise HTTPException(status_code=404, detail="Identity source Space binding not found")
    await session.delete(binding)
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.space_unbind",
        target_type="enterprise_identity_source_space_binding",
        target_id=binding.id,
    )


@router.put("/{source_id}/acl-membership-snapshot")
async def configure_feishu_acl_membership_snapshot(
    source_id: str,
    body: AclMembershipSnapshotConfig,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    if source.provider != IdentitySourceProvider.FEISHU or source.tenant_id is None:
        raise HTTPException(
            status_code=409,
            detail="Feishu tenant verification must complete before configuring ACL membership",
        )
    if not source.config_ciphertext:
        raise HTTPException(status_code=409, detail="Identity source configuration is missing")
    try:
        config = json.loads(decrypt(source.config_ciphertext))
    except Exception:
        raise HTTPException(status_code=409, detail="Identity source configuration is invalid") from None
    if not isinstance(config, dict):
        raise HTTPException(status_code=409, detail="Identity source configuration is invalid")
    config["acl_membership_snapshot"] = body.model_dump()
    source.config_ciphertext = encrypt(json.dumps(config))
    source.status = EnterpriseIdentitySourceStatus.PENDING_BINDING
    source.last_error = None
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.acl_membership_snapshot_configure",
        target_type="enterprise_identity_source",
        target_id=source.id,
    )
    return _source_response(source, space_bindings=[])
