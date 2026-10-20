from __future__ import annotations

import asyncio
import json
import hashlib
import secrets
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, exists, func, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.enterprise_user_identity import resolve_enterprise_user_ids
from server.auth.dependencies import require_admin
from server.config import get_config
from server.core.audit_logger import log_audit
from server.core.agent_knowledge_scope import resource_ids_from_json
from server.core.crypto import decrypt, encrypt
from server.core.identity_source_space import (
    bind_identity_source_to_enabled_spaces,
    identity_source_binds_all_spaces,
)
from server.db.engine import get_session
from server.enterprise_identity.service import identity_source_snapshot_is_usable
from server.enterprise_identity.providers import DIRECTORY_SYNC_ADAPTERS
from server.enterprise_identity.feishu_tenant_verification import (
    build_feishu_authorization_url,
    feishu_tenant_verification_callback_url,
)
from server.models import (
    AgentGroupAssignment,
    AgentGroupKind,
    AgentKnowledgeScope,
    AgentKnowledgePolicy,
    AgentKnowledgeScopeBinding,
    AuditStatus,
    EnterpriseDirectoryEntryStatus,
    EnterpriseDirectoryGroup,
    EnterpriseDirectoryMembership,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    EnterpriseIdentitySourceSpaceBinding,
    ExternalUserPrincipalMembership,
    EnterpriseIdentitySourceEvent,
    EnterpriseIdentitySourceEventStatus,
    FeishuTenantVerificationState,
    DingTalkLoginState,
    IdentitySourceProvider,
    KnowledgeResource,
    PolarRAGSpace,
    User,
    UserExternalIdentity,
    AuthProvider,
    UserRole,
    UserStatus,
)
from server.models.identity_source import DEFAULT_IDENTITY_SOURCE_STALE_AFTER_SECONDS
from server.enterprise_identity.principals import resolve_external_user_principals
from server.enterprise_identity.service import identity_provider_key, sync_identity_source
from server.enterprise_identity.sync import (
    schedule_identity_source_sync,
    sync_task,
)


router = APIRouter(prefix="/identity-sources", tags=["identity-sources"])
SYNC_REQUEST_TIMEOUT_SECONDS = 20.0


class IdentitySourceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    provider: Literal["feishu", "sharepoint", "dingtalk"]
    tenant_id: str | None = Field(default=None, min_length=1, max_length=255)
    app_id: str | None = Field(default=None, max_length=255)
    app_secret: str | None = Field(default=None, max_length=4096)
    cloud: Literal["global", "china"] = "global"
    client_id: str | None = Field(default=None, max_length=255)
    client_secret: str | None = Field(default=None, max_length=4096)
    bind_all_spaces: bool = True
    stale_after_seconds: int = Field(
        default=DEFAULT_IDENTITY_SOURCE_STALE_AFTER_SECONDS,
        ge=60,
        le=30 * 24 * 60 * 60,
    )

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
            raise ValueError("client_id and client_secret are required for this provider")
        return self


class IdentitySourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    app_id: str | None = Field(default=None, max_length=255)
    app_secret: str | None = Field(default=None, max_length=4096)
    cloud: Literal["global", "china"] = "global"
    client_id: str | None = Field(default=None, max_length=255)
    client_secret: str | None = Field(default=None, max_length=4096)
    stale_after_seconds: int = Field(
        default=DEFAULT_IDENTITY_SOURCE_STALE_AFTER_SECONDS,
        ge=60,
        le=30 * 24 * 60 * 60,
    )

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
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str = Field(default="polar_rag_meta", min_length=1, max_length=255)
    username: str = Field(min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=4096)
    dialect: Literal["mysql", "postgresql"] = "mysql"

    @field_validator("host", "database", "username", "password")
    @classmethod
    def validate_snapshot_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @model_validator(mode="after")
    def default_dialect_port(self) -> AclMembershipSnapshotConfig:
        if self.port is None:
            self.port = 5432 if self.dialect == "postgresql" else 3306
        return self


class ExternalPrincipalMembershipInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_type: EnterpriseDirectoryPrincipalType
    principal_id: str = Field(min_length=1, max_length=255)
    expires_at: datetime | None = None

    @field_validator("principal_id")
    @classmethod
    def validate_principal_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class ExternalUserPrincipalReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_user_id: str = Field(min_length=1, max_length=255)
    principals: list[ExternalPrincipalMembershipInput]

    @field_validator("external_user_id")
    @classmethod
    def validate_external_user_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_unique_principals(self) -> "ExternalUserPrincipalReplacement":
        seen: dict[tuple[EnterpriseDirectoryPrincipalType, str], datetime | None] = {}
        deduplicated: list[ExternalPrincipalMembershipInput] = []
        for principal in self.principals:
            key = (principal.principal_type, principal.principal_id)
            metadata = principal.expires_at
            if key in seen:
                if seen[key] != metadata:
                    raise ValueError("duplicate principal has conflicting metadata")
                continue
            seen[key] = metadata
            deduplicated.append(principal)
        self.principals = deduplicated
        return self


class ExternalUserPrincipalMembershipBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    users: list[ExternalUserPrincipalReplacement] = Field(max_length=100)

    @model_validator(mode="after")
    def validate_batch(self) -> "ExternalUserPrincipalMembershipBatch":
        user_ids = [item.external_user_id for item in self.users]
        if len(user_ids) != len(set(user_ids)):
            raise ValueError("external_user_id must be unique within a batch")
        if sum(len(item.principals) for item in self.users) > 50_000:
            raise ValueError("a batch cannot contain more than 50000 memberships")
        return self


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
    return isinstance(config, dict) and isinstance(config.get("acl_membership_snapshot"), dict)


def _acl_membership_snapshot_public_config(source: EnterpriseIdentitySource) -> dict[str, object] | None:
    if not source.config_ciphertext:
        return None
    try:
        config = json.loads(decrypt(source.config_ciphertext))
        snapshot = config.get("acl_membership_snapshot") if isinstance(config, dict) else None
        if not isinstance(snapshot, dict):
            return None
        return {key: snapshot[key] for key in ("dialect", "host", "port", "database", "username") if key in snapshot}
    except Exception:
        return None


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
    principals: set[tuple[str, str, str]] = {(source.provider.value, "user", external_user_id)}
    principals.update(
        (source.provider.value, principal_type.value, principal_id)
        for principal_type, principal_id in await resolve_external_user_principals(
            session,
            source.id,
            external_user_id,
        )
    )
    frontier = {external_user_id}
    member_type = EnterpriseDirectoryMembershipType.USER
    visited_groups: set[str] = set()
    while frontier:
        rows = (
            await session.execute(
                select(
                    EnterpriseDirectoryGroup.external_group_id,
                    EnterpriseDirectoryGroup.principal_type,
                )
                .select_from(EnterpriseDirectoryMembership)
                .join(
                    EnterpriseDirectoryGroup,
                    (EnterpriseDirectoryGroup.identity_source_id == EnterpriseDirectoryMembership.identity_source_id)
                    & (EnterpriseDirectoryGroup.external_group_id == EnterpriseDirectoryMembership.external_group_id),
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
        principals.update((source.provider.value, principal_type, group_id) for group_id, principal_type in groups)
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
        "mapping_mode": ("synced" if user.external_id == default_external_id else "pas_managed"),
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
    user = (await session.execute(select(User).where(User.external_id == external_id))).scalar_one_or_none()
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
    sync_warning = None
    if source.sync_warning_json:
        try:
            decoded_warning = json.loads(source.sync_warning_json)
            if isinstance(decoded_warning, dict):
                sync_warning = decoded_warning
        except ValueError:
            pass
    response: dict[str, object] = {
        "id": source.id,
        "name": source.name,
        "provider": source.provider.value,
        "tenant_id": source.tenant_id,
        "status": source.status.value,
        "snapshot_usable": identity_source_snapshot_is_usable(source),
        "last_synced_at": source.last_synced_at,
        "stale_after_seconds": source.stale_after_seconds,
        "last_error": source.last_error,
        "full_sync_in_progress": source.full_sync_requested_at is not None
        or (source.sync_worker_id is not None and source.sync_kind != "groups"),
        "group_sync_in_progress": source.sync_worker_id is not None and source.sync_kind == "groups",
        "last_group_synced_at": source.last_group_synced_at,
        "last_group_error": source.last_group_error,
        "sync_warning": sync_warning,
        "sync_supported": source.provider in DIRECTORY_SYNC_ADAPTERS,
        "event_subscription": (
            {
                "status": (
                    source.event_listener_status.value if source.event_listener_status is not None else "connecting"
                ),
                "fallback_after_seconds": 300 if source.provider == IdentitySourceProvider.DINGTALK else get_config().enterprise_identity_sync.feishu_fallback_after_seconds,
            }
            if source.provider in {IdentitySourceProvider.FEISHU, IdentitySourceProvider.DINGTALK}
            else None
        ),
        "acl_membership_snapshot_configured": _acl_membership_snapshot_configured(source),
        "acl_membership_snapshot": _acl_membership_snapshot_public_config(source),
        "space_bindings": space_bindings,
    }
    if source.provider == IdentitySourceProvider.DINGTALK:
        response.update(permission_observed_at=source.permission_observed_at, permission_error=source.permission_error,
                        supported_principal_types=["user", "department", "domain"])
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
    if source.provider == IdentitySourceProvider.DINGTALK:
        source.status = EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION
    source.stale_after_seconds = min(body.stale_after_seconds, 900) if source.provider == IdentitySourceProvider.DINGTALK else body.stale_after_seconds
    provider_config = (
        {"app_id": body.app_id, "app_secret": body.app_secret}
        if source.provider == IdentitySourceProvider.FEISHU
        else {
            "cloud": body.cloud,
            "client_id": body.client_id,
            "client_secret": body.client_secret,
        }
    )
    if not body.bind_all_spaces:
        provider_config["bind_all_spaces"] = False
    source.config_ciphertext = encrypt(json.dumps(provider_config))
    session.add(source)
    try:
        await session.flush()
        space_bindings = await bind_identity_source_to_enabled_spaces(session, source) if body.bind_all_spaces else []
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
    return _source_response(source, space_bindings=sorted(space_bindings))


def _feishu_verification_callback_url() -> str:
    try:
        return feishu_tenant_verification_callback_url(get_config().server.public_base_url)
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
        or source.status != EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION
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
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    filters: list[Any] = []
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                EnterpriseIdentitySource.name.ilike(pattern),
            )
        )
    total = await session.scalar(select(func.count(EnterpriseIdentitySource.id)).where(*filters)) or 0
    sources = list(
        (
            await session.execute(
                select(EnterpriseIdentitySource)
                .where(*filters)
                .order_by(EnterpriseIdentitySource.name, EnterpriseIdentitySource.id)
                .offset(offset)
                .limit(limit)
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
        bindings_by_source.setdefault(binding.identity_source_id, []).append(binding.knowledge_space_id)
    return {
        "items": [
            _source_response(
                source,
                space_bindings=sorted(bindings_by_source.get(source.id, [])),
            )
            for source in sources
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/{source_id}/sync")
async def sync_source_now(
    source_id: str,
    request: Request,
    response: Response,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    if source.provider not in DIRECTORY_SYNC_ADAPTERS:
        raise HTTPException(status_code=409, detail="Identity source synchronization is not supported")
    if source.provider == IdentitySourceProvider.DINGTALK:
        from server.enterprise_identity.dingtalk_auth import verified_dingtalk_config
        try:
            verified_dingtalk_config(source)
        except (ValueError, TypeError):
            raise HTTPException(status_code=409, detail="Verify DingTalk enterprise before synchronization") from None
    if source.tenant_id is None or source.config_ciphertext is None:
        raise HTTPException(status_code=409, detail="Identity source is not ready for synchronization")
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        from server.db.engine import get_session_factory

        session_factory = get_session_factory()
    if source.provider == IdentitySourceProvider.FEISHU and source.sync_kind == "groups":
        source.full_sync_requested_at = datetime.now(UTC)
        await session.commit()
    started = await schedule_identity_source_sync(
        session_factory,
        source.id,
        background_tasks=getattr(request.app.state, "background_tasks", None),
        sync_callable=sync_identity_source,
    )
    if source.provider == IdentitySourceProvider.FEISHU:
        await session.refresh(source)
        if started:
            await _commit_with_audit(
                session,
                user_id=admin.id,
                action="identity_source.sync_started",
                target_type="enterprise_identity_source",
                target_id=source.id,
            )
        response.status_code = 202
        return _source_response(source, space_bindings=[])
    if not started:
        response.status_code = 202
        return _source_response(source, space_bindings=[])
    task = sync_task(source.id)
    assert task is not None
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=SYNC_REQUEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await session.refresh(source)
        source.last_error = "SYNC_IN_PROGRESS"
        await _commit_with_audit(
            session,
            user_id=admin.id,
            action="identity_source.sync_started",
            target_type="enterprise_identity_source",
            target_id=source.id,
        )
        response.status_code = 202
        return _source_response(source, space_bindings=[])
    await session.refresh(source)
    if source.status == EnterpriseIdentitySourceStatus.STALE:
        raise HTTPException(status_code=502, detail="Identity source synchronization failed")
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
                detail="client_id and client_secret are required for this provider",
            )
        config = {
            "cloud": body.cloud,
            "client_id": body.client_id,
            "client_secret": body.client_secret,
        }
        source.status = EnterpriseIdentitySourceStatus.PENDING_BINDING
    if not identity_source_binds_all_spaces(source):
        config["bind_all_spaces"] = False
    source.name = body.name
    if source.provider == IdentitySourceProvider.DINGTALK:
        source.status = EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION
    source.stale_after_seconds = min(body.stale_after_seconds, 900) if source.provider == IdentitySourceProvider.DINGTALK else body.stale_after_seconds
    source.config_ciphertext = encrypt(json.dumps(config))
    source.last_synced_at = None
    source.last_group_synced_at = None
    source.last_group_error = None
    source.group_next_retry_at = None
    source.full_sync_requested_at = None
    source.last_error = None
    source.event_listener_worker_id = None
    source.event_listener_lease_until = None
    source.event_listener_retry_count = 0
    source.event_unavailable_since = None
    source.event_listener_status = None
    if source.provider == IdentitySourceProvider.DINGTALK:
        # Pending notifications belong to the old credential/visibility epoch.
        # Preserve their dedup IDs, but let the verified full scan rebuild facts.
        await session.execute(update(EnterpriseIdentitySourceEvent).where(
            EnterpriseIdentitySourceEvent.identity_source_id == source.id,
            EnterpriseIdentitySourceEvent.status != EnterpriseIdentitySourceEventStatus.PROCESSED,
        ).values(status=EnterpriseIdentitySourceEventStatus.PROCESSED, next_retry_at=None))
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
    source_scope_ids = select(AgentKnowledgeScope.id).where(AgentKnowledgeScope.identity_source_id == source.id)
    source_group_ids = select(AgentGroupAssignment.id).where(AgentGroupAssignment.identity_source_id == source.id)
    await session.execute(
        delete(AgentKnowledgeScopeBinding).where(
            or_(
                AgentKnowledgeScopeBinding.scope_id.in_(source_scope_ids),
                AgentKnowledgeScopeBinding.group_assignment_id.in_(source_group_ids),
            )
        )
    )
    await session.execute(delete(AgentKnowledgeScope).where(AgentKnowledgeScope.identity_source_id == source.id))
    await session.execute(
        delete(ExternalUserPrincipalMembership).where(ExternalUserPrincipalMembership.identity_source_id == source.id)
    )
    await session.execute(
        delete(EnterpriseIdentitySourceEvent).where(EnterpriseIdentitySourceEvent.identity_source_id == source.id)
    )
    await session.execute(delete(DingTalkLoginState).where(DingTalkLoginState.identity_source_id == source.id))
    await session.delete(source)
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.delete",
        target_type="enterprise_identity_source",
        target_id=source_id,
    )


@router.get("/enterprise-users/identities")
async def list_enterprise_user_identities(
    identity_source_id: str = Query(min_length=1, max_length=36),
    external_user_id: str = Query(min_length=1, max_length=255),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user_id = (await resolve_enterprise_user_ids(session, identity_source_id, [external_user_id]))[0]
    return await list_user_enterprise_identities(user_id, offset, limit, search, admin, session)


@router.post("/enterprise-users/identities", status_code=201)
async def create_enterprise_user_identity(
    body: EnterpriseIdentityMappingRequest,
    response: Response,
    identity_source_id: str = Query(min_length=1, max_length=36),
    external_user_id: str = Query(min_length=1, max_length=255),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user_id = (await resolve_enterprise_user_ids(session, identity_source_id, [external_user_id]))[0]
    return await create_user_enterprise_identity(user_id, body, response, admin, session)


@router.put("/enterprise-users/identities/{identity_id}")
async def update_enterprise_user_identity(
    identity_id: str,
    body: EnterpriseIdentityMappingRequest,
    identity_source_id: str = Query(min_length=1, max_length=36),
    external_user_id: str = Query(min_length=1, max_length=255),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user_id = (await resolve_enterprise_user_ids(session, identity_source_id, [external_user_id]))[0]
    return await update_user_enterprise_identity(user_id, identity_id, body, admin, session)


@router.delete("/enterprise-users/identities/{identity_id}", status_code=204)
async def delete_enterprise_user_identity(
    identity_id: str,
    identity_source_id: str = Query(min_length=1, max_length=36),
    external_user_id: str = Query(min_length=1, max_length=255),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user_id = (await resolve_enterprise_user_ids(session, identity_source_id, [external_user_id]))[0]
    return await delete_user_enterprise_identity(user_id, identity_id, admin, session)


@router.get("/users/{user_id}/identities")
async def list_user_enterprise_identities(
    user_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    sources = list(
        (await session.execute(select(EnterpriseIdentitySource).order_by(EnterpriseIdentitySource.name))).scalars()
    )
    sources_by_provider = {identity_provider_key(source): source for source in sources}
    provider_keys = list(sources_by_provider)
    if not provider_keys:
        return {"items": [], "total": 0, "offset": offset, "limit": limit}
    filters: list[Any] = [
        UserExternalIdentity.identity_provider.in_(provider_keys),
        UserExternalIdentity.user_id == user_id,
    ]
    if search and search.strip():
        filters.append(UserExternalIdentity.external_subject.ilike(f"%{search.strip()}%"))
    total = await session.scalar(select(func.count(UserExternalIdentity.id)).where(*filters)) or 0
    identities = list(
        (
            await session.execute(
                select(UserExternalIdentity)
                .where(*filters)
                .order_by(
                    UserExternalIdentity.identity_provider,
                    UserExternalIdentity.external_subject,
                    UserExternalIdentity.id,
                )
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )
    items = [
        await _identity_mapping_response(
            session,
            identity,
            sources_by_provider[identity.identity_provider],
        )
        for identity in identities
    ]
    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
    }


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
        expected_external_id = f"{identity_provider_key(source)}:{directory_user.external_user_id}"
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
            for source in (await session.execute(select(EnterpriseIdentitySource))).scalars()
            if current_identity.identity_provider == identity_provider_key(source)
        ),
        None,
    )
    if current_source is None:
        raise HTTPException(status_code=409, detail="Enterprise identity source is unavailable")
    if user.external_id == (f"{identity_provider_key(current_source)}:{current_identity.external_subject}"):
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
        expected_external_id = f"{identity_provider_key(source)}:{directory_user.external_user_id}"
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
    current_identity.user_id = (await _default_source_user(session, current_source, current_directory_user)).id
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
            for source in (await session.execute(select(EnterpriseIdentitySource))).scalars()
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
                    user_query.order_by(EnterpriseDirectoryUser.display_name, EnterpriseDirectoryUser.external_user_id)
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
                    group_query.order_by(
                        EnterpriseDirectoryGroup.display_name, EnterpriseDirectoryGroup.external_group_id
                    )
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
                        UserExternalIdentity.identity_provider == identity_provider_key(source),
                        UserExternalIdentity.external_subject.in_([user.external_user_id for user in users]),
                    )
                )
            ).scalars()
        )
    pas_user_by_external_subject = {identity.external_subject: identity.user_id for identity in identities}
    return {
        "users": [
            {
                "id": user.id,
                "pas_user_id": pas_user_by_external_subject.get(user.external_user_id),
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
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    identity_source_id: str | None = Query(default=None, max_length=36),
    agent_id: str | None = Query(default=None, max_length=36),
    binding_status: Literal["bound", "unbound"] | None = Query(default=None),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    filters: list[Any] = [PolarRAGSpace.enabled.is_(True)]
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                PolarRAGSpace.name.ilike(pattern),
                PolarRAGSpace.identity_domain.ilike(pattern),
                PolarRAGSpace.space_id.ilike(pattern),
            )
        )
    binding_exists = None
    if identity_source_id:
        binding_exists = exists(
            select(EnterpriseIdentitySourceSpaceBinding.id).where(
                EnterpriseIdentitySourceSpaceBinding.identity_source_id == identity_source_id,
                EnterpriseIdentitySourceSpaceBinding.knowledge_space_id == PolarRAGSpace.knowledge_space_id,
            )
        )
        if binding_status == "bound":
            filters.append(binding_exists)
        elif binding_status == "unbound":
            filters.append(~binding_exists)
    total = await session.scalar(select(func.count(PolarRAGSpace.knowledge_space_id)).where(*filters)) or 0
    rows = list(
        (
            await session.execute(
                select(PolarRAGSpace)
                .where(*filters)
                .order_by(PolarRAGSpace.name, PolarRAGSpace.knowledge_space_id)
                .offset(offset)
                .limit(limit)
            )
        ).scalars()
    )
    bound_space_ids: set[str] = set()
    if identity_source_id and rows:
        bound_space_ids = set(
            (
                await session.execute(
                    select(EnterpriseIdentitySourceSpaceBinding.knowledge_space_id).where(
                        EnterpriseIdentitySourceSpaceBinding.identity_source_id == identity_source_id,
                        EnterpriseIdentitySourceSpaceBinding.knowledge_space_id.in_(
                            [space.knowledge_space_id for space in rows]
                        ),
                    )
                )
            ).scalars()
        )
    items = [
        {
            "knowledge_space_id": space.knowledge_space_id,
            "polarrag_instance_id": space.polarrag_instance_id,
            "name": space.name,
            "identity_domain": space.identity_domain,
        }
        for space in rows
    ]
    if identity_source_id:
        for item in items:
            item["is_bound"] = item["knowledge_space_id"] in bound_space_ids
    response: dict[str, Any] = {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
    }
    if identity_source_id and agent_id:
        response["all_users_bound"] = bool(
            await session.scalar(
                select(func.count(AgentGroupAssignment.id)).where(
                    AgentGroupAssignment.agent_id == agent_id,
                    AgentGroupAssignment.group_kind == AgentGroupKind.IDENTITY_SOURCE_ALL,
                    AgentGroupAssignment.identity_source_id == identity_source_id,
                )
            )
        )
    return response


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
    from server.core.identity_source_space import ensure_identity_domain_tenant
    await ensure_identity_domain_tenant(session, source, space)
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


async def _cascade_source_space_knowledge_bindings(
    session: AsyncSession,
    *,
    source_id: str,
    knowledge_space_id: str,
) -> None:
    resource_ids = set(
        (
            await session.execute(
                select(KnowledgeResource.id).where(KnowledgeResource.knowledge_space_id == knowledge_space_id)
            )
        ).scalars()
    )
    if not resource_ids:
        return
    has_other_source = bool(
        await session.scalar(
            select(func.count(EnterpriseIdentitySourceSpaceBinding.id)).where(
                EnterpriseIdentitySourceSpaceBinding.knowledge_space_id == knowledge_space_id,
                EnterpriseIdentitySourceSpaceBinding.identity_source_id != source_id,
            )
        )
    )
    source_assignment_ids = set(
        (
            await session.execute(
                select(AgentGroupAssignment.id).where(AgentGroupAssignment.identity_source_id == source_id)
            )
        ).scalars()
    )
    for policy in await session.scalars(
        select(AgentKnowledgePolicy).where(
            AgentKnowledgePolicy.knowledge_resource_id.in_(resource_ids),
        )
    ):
        if not has_other_source:
            policy.select_all = False
            policy.excluded_subjects_json = "[]"
        else:
            policy.excluded_subjects_json = json.dumps(
                [
                    key
                    for key in json.loads(policy.excluded_subjects_json)
                    if not (key[0] in {"GROUP", "IDENTITY_SOURCE"} and key[1] == source_id)
                ],
                separators=(",", ":"),
            )
    scopes = list((await session.execute(select(AgentKnowledgeScope))).scalars())
    for scope in scopes:
        current_resources = resource_ids_from_json(scope.knowledge_resource_ids_json)
        if not current_resources.intersection(resource_ids):
            continue
        bindings = list(
            (
                await session.execute(
                    select(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.scope_id == scope.id)
                )
            ).scalars()
        )
        affected = (
            bindings
            if not has_other_source
            else [
                row
                for row in bindings
                if scope.identity_source_id == source_id or row.group_assignment_id in source_assignment_ids
            ]
        )
        if not affected:
            continue
        remaining_resources = current_resources - resource_ids
        if len(affected) == len(bindings):
            if remaining_resources:
                scope.knowledge_resource_ids_json = json.dumps(
                    sorted(remaining_resources),
                    separators=(",", ":"),
                )
            else:
                for row in bindings:
                    await session.delete(row)
                await session.delete(scope)
            continue
        replacement: AgentKnowledgeScope | None = None
        if remaining_resources:
            replacement = AgentKnowledgeScope(
                agent_id=scope.agent_id,
                name=scope.name,
                origin=scope.origin,
                identity_source_id=scope.identity_source_id,
                external_scope_id=scope.external_scope_id,
                knowledge_resource_ids_json=json.dumps(
                    sorted(remaining_resources),
                    separators=(",", ":"),
                ),
                created_by_user_id=scope.created_by_user_id,
            )
            session.add(replacement)
            await session.flush()
        for row in affected:
            if replacement is None:
                await session.delete(row)
            else:
                row.scope_id = replacement.id


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
    await _cascade_source_space_knowledge_bindings(
        session,
        source_id=source.id,
        knowledge_space_id=knowledge_space_id,
    )
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
    if source.provider not in {IdentitySourceProvider.FEISHU, IdentitySourceProvider.DINGTALK} or source.tenant_id is None:
        raise HTTPException(
            status_code=409,
            detail="Feishu tenant verification must complete before configuring ACL membership",
        )
    local_membership_exists = (
        await session.execute(
            select(ExternalUserPrincipalMembership.id)
            .where(ExternalUserPrincipalMembership.identity_source_id == source.id)
            .limit(1)
        )
    ).first()
    if local_membership_exists is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACL_MEMBERSHIP_BACKEND_CONFLICT",
                "message": "Local principal memberships are already configured",
            },
        )
    if not source.config_ciphertext:
        raise HTTPException(status_code=409, detail="Identity source configuration is missing")
    try:
        config = json.loads(decrypt(source.config_ciphertext))
    except Exception:
        raise HTTPException(status_code=409, detail="Identity source configuration is invalid") from None
    if not isinstance(config, dict):
        raise HTTPException(status_code=409, detail="Identity source configuration is invalid")
    previous = config.get("acl_membership_snapshot")
    password = body.password or (previous.get("password") if isinstance(previous, dict) else None)
    if not isinstance(password, str) or not password:
        raise HTTPException(status_code=422, detail="Password is required for a new permission data source connection")
    config["acl_membership_snapshot"] = {**body.model_dump(exclude_none=True), "password": password}
    source.config_ciphertext = encrypt(json.dumps(config))
    from server.enterprise_identity.etl_permissions import configuration_saved

    await configuration_saved(session, source)
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.acl_membership_snapshot_configure",
        target_type="enterprise_identity_source",
        target_id=source.id,
    )
    return _source_response(source, space_bindings=[])


@router.put("/{source_id}/user-principal-memberships")
async def replace_external_user_principal_memberships(
    source_id: str,
    body: ExternalUserPrincipalMembershipBatch,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    if _acl_membership_snapshot_configured(source):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ACL_MEMBERSHIP_BACKEND_CONFLICT",
                "message": "Direct ACL membership snapshot is already configured",
            },
        )

    external_user_ids = [item.external_user_id for item in body.users]
    existing_rows = (
        list(
            (
                await session.execute(
                    select(ExternalUserPrincipalMembership).where(
                        ExternalUserPrincipalMembership.identity_source_id == source.id,
                        ExternalUserPrincipalMembership.external_user_id.in_(external_user_ids),
                    )
                )
            ).scalars()
        )
        if external_user_ids
        else []
    )
    existing = {(row.external_user_id, row.principal_type, row.principal_id): row for row in existing_rows}
    desired = {
        (item.external_user_id, principal.principal_type, principal.principal_id): principal
        for item in body.users
        for principal in item.principals
    }
    connection = await session.connection()
    has_prerelease_metadata_column = await connection.run_sync(
        lambda sync_connection: (
            "source_subject_type"
            in {
                column["name"]
                for column in inspect(sync_connection).get_columns("identity_source_user_principal_snapshot_entries")
            }
        )
    )
    for key, stale_row in existing.items():
        if key not in desired:
            await session.delete(stale_row)
    current = datetime.now(UTC)
    for key, principal in desired.items():
        existing_row = existing.get(key)
        if existing_row is None:
            existing_row = ExternalUserPrincipalMembership.create(
                identity_source_id=source.id,
                external_user_id=key[0],
                principal_type=key[1],
                principal_id=key[2],
                expires_at=principal.expires_at,
            )
            if has_prerelease_metadata_column:
                await session.execute(
                    text(
                        "INSERT INTO identity_source_user_principal_snapshot_entries "
                        "(id, identity_source_id, external_user_id, principal_type, "
                        "principal_id, source_subject_type, expires_at, created_at) "
                        "VALUES (:id, :identity_source_id, :external_user_id, "
                        ":principal_type, :principal_id, :source_subject_type, "
                        ":expires_at, :created_at)"
                    ),
                    {
                        "id": existing_row.id,
                        "identity_source_id": source.id,
                        "external_user_id": key[0],
                        "principal_type": key[1].value,
                        "principal_id": key[2],
                        "source_subject_type": "local_snapshot",
                        "expires_at": principal.expires_at,
                        "created_at": current,
                    },
                )
                continue
            session.add(existing_row)
        else:
            existing_row.expires_at = principal.expires_at
            existing_row.updated_at = current
    await _commit_with_audit(
        session,
        user_id=admin.id,
        action="identity_source.user_principal_memberships_replace",
        target_type="enterprise_identity_source",
        target_id=source.id,
    )
    return {
        "replaced_users": len(body.users),
        "membership_count": len(desired),
    }


@router.get("/{source_id}/etl-permissions")
async def get_etl_permissions_status(source_id: str, _admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    from server.enterprise_identity.etl_permissions import status
    if await session.get(EnterpriseIdentitySource, source_id) is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    return await status(session, source_id)


@router.post("/{source_id}/etl-permissions/{operation}", status_code=202)
async def trigger_etl_permissions(source_id: str, operation: Literal["verify", "sync"], background_tasks: BackgroundTasks, admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    from server.enterprise_identity.etl_permissions import claim, run, status
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Identity source not found")
    owner = await claim(session, source, operation)
    await _commit_with_audit(session, user_id=admin.id, action=f"identity_source.etl_permissions_{operation}", target_type="enterprise_identity_source", target_id=source.id)
    background_tasks.add_task(run, source_id, owner)
    return await status(session, source_id)


@router.post("/{source_id}/dingtalk-verification")
async def start_dingtalk_verification(source_id: str, request: Request, response: Response,
                                      admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    from server.auth.router import begin_dingtalk_authorization, set_dingtalk_state_cookie
    from server.enterprise_identity.dingtalk_auth import DingTalkCallbackConfigurationError
    source = await session.get(EnterpriseIdentitySource, source_id)
    if source is None:
        raise HTTPException(404, "Identity source not found")
    if source.provider != IdentitySourceProvider.DINGTALK or source.status != EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION:
        raise HTTPException(409, "DingTalk source is not pending verification")
    try:
        url, browser = await begin_dingtalk_authorization(session, source, request, purpose="verify", admin_id=admin.id)
        await _commit_with_audit(session, user_id=admin.id, action="identity_source.dingtalk_verification_start",
                                 target_type="enterprise_identity_source", target_id=source.id)
    except DingTalkCallbackConfigurationError as exc:
        await session.rollback()
        raise HTTPException(409, str(exc), headers={"Cache-Control": "no-store"}) from None
    except (ValueError, TypeError):
        await session.rollback()
        raise HTTPException(409, "DingTalk configuration is invalid") from None
    set_dingtalk_state_cookie(response, browser, request)
    response.headers["Cache-Control"] = "no-store"
    return {"identity_source_id": source.id, "authorization_url": url}
