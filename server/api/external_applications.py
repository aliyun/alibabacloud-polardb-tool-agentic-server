from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.api.pagination import Page
from server.auth.external_tokens import (
    ExternalTokenInvalid,
    ExternalTokenUnavailable,
)
from server.auth.oauth_provider import PASAuthProvider
from server.config import AppConfig, get_config
from server.core.audit_logger import log_audit
from server.core.external_application_service import (
    ExternalApplicationTarget,
    create_external_application,
    parse_targets,
    resource_profiles,
    rotate_external_application_secret,
    set_external_application_status,
)
from server.db.engine import get_session, get_session_factory
from server.models import (
    AuditStatus,
    OAuthExternalApplication,
    OAuthExternalApplicationAgentPolicy,
    OAuthExternalApplicationStatus,
    OAuthRegisteredClient,
    User,
)

router = APIRouter(
    prefix="/v1/external-auth/clients",
    tags=["external-auth-clients"],
)


class ResourceProfileResponse(BaseModel):
    target: ExternalApplicationTarget
    resource: str
    scope: str


class ExternalApplicationContextResponse(BaseModel):
    provider_enabled: bool
    provider_type: str
    token_endpoint: str
    compatibility_token_endpoint: str
    resources: list[ResourceProfileResponse]


class ExternalApplicationCreateRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=255)
    targets: list[ExternalApplicationTarget] = Field(min_length=1)
    agent_policy: OAuthExternalApplicationAgentPolicy = (
        OAuthExternalApplicationAgentPolicy.WORKSPACE_DEFAULT
    )
    fixed_agent_id: str | None = None
    secret_expires_at: datetime | None = None


class ExternalApplicationStatusRequest(BaseModel):
    status: OAuthExternalApplicationStatus


class ExternalApplicationRotateSecretRequest(BaseModel):
    secret_expires_at: datetime | None = None


class ExternalApplicationTestRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    subject_token: str = Field(min_length=1)
    resource: str | None = None
    agent_id: str | None = None
    identity_source_id: str | None = None
    feishu_user_id: str | None = None
    feishu_union_id: str | None = None


class ExternalApplicationResponse(BaseModel):
    client_id: str
    name: str
    provider_type: str
    targets: list[ExternalApplicationTarget]
    agent_policy: OAuthExternalApplicationAgentPolicy
    fixed_agent_id: str | None
    status: OAuthExternalApplicationStatus
    secret_expires_at: datetime | None
    secret_created_at: datetime
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime | None
    token_endpoint: str
    compatibility_token_endpoint: str
    resources: list[ResourceProfileResponse]


class ExternalApplicationSecretResponse(ExternalApplicationResponse):
    client_secret: str


class ExternalApplicationTestResponse(BaseModel):
    authenticated: Literal[True]
    expires_in: int | None
    scope: str


def _context(config: AppConfig) -> ExternalApplicationContextResponse:
    base_url = config.server.public_base_url.rstrip("/")
    try:
        profiles = [
            ResourceProfileResponse(
                target=profile.target,
                resource=profile.resource,
                scope=profile.scope,
            )
            for profile in resource_profiles(config)
        ]
    except ValueError:
        profiles = []
    return ExternalApplicationContextResponse(
        provider_enabled=config.auth.external_token_trust.enabled,
        provider_type=config.auth.external_token_trust.provider,
        token_endpoint=f"{base_url}/token" if base_url else "",
        compatibility_token_endpoint=(
            f"{base_url}/api/v1/external-auth/token" if base_url else ""
        ),
        resources=profiles,
    )


def _secret_expiration(client: OAuthRegisteredClient) -> datetime | None:
    if not client.client_secret_expires_at:
        return None
    return datetime.fromtimestamp(
        client.client_secret_expires_at,
        tz=timezone.utc,
    )


def _response(
    application: OAuthExternalApplication,
    client: OAuthRegisteredClient,
    config: AppConfig,
) -> ExternalApplicationResponse:
    context = _context(config)
    enabled_targets = set(parse_targets(application.targets))
    return ExternalApplicationResponse(
        client_id=application.client_id,
        name=client.client_name or application.client_id,
        provider_type=application.provider_type,
        targets=list(parse_targets(application.targets)),
        agent_policy=OAuthExternalApplicationAgentPolicy(
            application.agent_policy
        ),
        fixed_agent_id=application.fixed_agent_id,
        status=OAuthExternalApplicationStatus(application.status),
        secret_expires_at=_secret_expiration(client),
        secret_created_at=application.secret_created_at,
        last_used_at=application.last_used_at,
        created_at=application.created_at,
        updated_at=application.updated_at,
        token_endpoint=context.token_endpoint,
        compatibility_token_endpoint=context.compatibility_token_endpoint,
        resources=[
            profile
            for profile in context.resources
            if profile.target in enabled_targets
        ],
    )


async def _load(
    session: AsyncSession,
    client_id: str,
) -> tuple[OAuthExternalApplication, OAuthRegisteredClient]:
    row = (
        await session.execute(
            select(OAuthExternalApplication, OAuthRegisteredClient)
            .join(
                OAuthRegisteredClient,
                OAuthRegisteredClient.client_id
                == OAuthExternalApplication.client_id,
            )
            .where(OAuthExternalApplication.client_id == client_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="External application not found",
        )
    return row[0], row[1]


async def _audit(
    session: AsyncSession,
    admin: User,
    *,
    action: str,
    client_id: str,
) -> None:
    await log_audit(
        session,
        user_id=admin.id,
        action=action,
        status=AuditStatus.SUCCESS,
        user_name=admin.display_name,
        target_type="oauth_external_application",
        target_id=client_id,
        required=True,
        commit=False,
    )


@router.get("/context", response_model=ExternalApplicationContextResponse)
async def get_external_application_context(
    _admin: User = Depends(require_admin),
) -> ExternalApplicationContextResponse:
    return _context(get_config())


@router.get("", response_model=Page[ExternalApplicationResponse])
async def list_external_applications(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None, max_length=255),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Page[ExternalApplicationResponse]:
    config = get_config()
    filters = []
    if search and search.strip():
        pattern = f"%{search.strip()}%"
        filters.append(
            or_(
                OAuthExternalApplication.client_id.ilike(pattern),
                OAuthRegisteredClient.client_name.ilike(pattern),
            )
        )
    total = (
        await session.scalar(
            select(func.count(OAuthExternalApplication.client_id))
            .join(
                OAuthRegisteredClient,
                OAuthRegisteredClient.client_id
                == OAuthExternalApplication.client_id,
            )
            .where(*filters)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(OAuthExternalApplication, OAuthRegisteredClient)
            .join(
                OAuthRegisteredClient,
                OAuthRegisteredClient.client_id
                == OAuthExternalApplication.client_id,
            )
            .where(*filters)
            .order_by(OAuthExternalApplication.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    return Page(
        items=[
            _response(application, client, config)
            for application, client in rows
        ],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.post(
    "",
    response_model=ExternalApplicationSecretResponse,
    status_code=201,
)
async def create_external_application_client(
    body: ExternalApplicationCreateRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    config = get_config()
    try:
        application, secret = await create_external_application(
            session,
            config,
            name=body.name,
            created_by=admin.id,
            targets=body.targets,
            agent_policy=body.agent_policy,
            fixed_agent_id=body.fixed_agent_id,
            secret_expires_at=body.secret_expires_at,
        )
        await _audit(
            session,
            admin,
            action="external_application.create",
            client_id=application.client_id,
        )
        await session.commit()
        application, client = await _load(session, application.client_id)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response = ExternalApplicationSecretResponse(
        **_response(application, client, config).model_dump(),
        client_secret=secret,
    )
    return Response(
        content=response.model_dump_json(),
        status_code=201,
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/{client_id}",
    response_model=ExternalApplicationResponse,
)
async def get_external_application(
    client_id: str,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ExternalApplicationResponse:
    application, client = await _load(session, client_id)
    return _response(application, client, get_config())


@router.post(
    "/{client_id}/rotate-secret",
    response_model=ExternalApplicationSecretResponse,
)
async def rotate_external_application_client_secret(
    client_id: str,
    body: ExternalApplicationRotateSecretRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    application, _client = await _load(session, client_id)
    try:
        secret = await rotate_external_application_secret(
            session,
            application,
            secret_expires_at=body.secret_expires_at,
        )
        await _audit(
            session,
            admin,
            action="external_application.rotate_secret",
            client_id=client_id,
        )
        await session.commit()
        application, client = await _load(session, client_id)
    except ValueError as exc:
        await session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    response = ExternalApplicationSecretResponse(
        **_response(application, client, get_config()).model_dump(),
        client_secret=secret,
    )
    return Response(
        content=response.model_dump_json(),
        media_type="application/json",
        headers={"Cache-Control": "no-store"},
    )


@router.put(
    "/{client_id}/status",
    response_model=ExternalApplicationResponse,
)
async def update_external_application_status(
    client_id: str,
    body: ExternalApplicationStatusRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ExternalApplicationResponse:
    application, client = await _load(session, client_id)
    set_external_application_status(application, body.status)
    await _audit(
        session,
        admin,
        action=f"external_application.{body.status.value}",
        client_id=client_id,
    )
    await session.commit()
    return _response(application, client, get_config())


@router.post(
    "/{client_id}/test",
    response_model=ExternalApplicationTestResponse,
)
async def test_external_application(
    client_id: str,
    body: ExternalApplicationTestRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ExternalApplicationTestResponse:
    application, _client = await _load(session, client_id)
    provider = PASAuthProvider(get_session_factory(), get_config())
    client = await provider.get_client(client_id)
    if client is None:
        raise HTTPException(status_code=409, detail="OAuth client unavailable")
    try:
        token = await provider.exchange_external_token(
            client,
            body.subject_token,
            resource=body.resource,
            scopes=[],
            agent_id=body.agent_id,
            identity_source_id=body.identity_source_id,
            feishu_user_id=body.feishu_user_id,
            feishu_union_id=body.feishu_union_id,
        )
    except ExternalTokenUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="External identity provider is unavailable",
        ) from exc
    except (ExternalTokenInvalid, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit(
        session,
        admin,
        action="external_application.test",
        client_id=application.client_id,
    )
    await session.commit()
    return ExternalApplicationTestResponse(
        authenticated=True,
        expires_in=token.expires_in,
        scope=token.scope or "",
    )
