from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from binascii import Error as BinasciiError
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import HTMLResponse, RedirectResponse

from server.auth.builtin import authenticate_builtin
from server.auth.dependencies import get_current_user
from server.auth.identity_federation import OIDCAuthenticationError
from server.auth.jwt_manager import create_access_token
from server.auth.rate_limit import (
    AuthRateLimitExceeded,
    check_builtin_login,
)
from server.config import get_config
from server.core.audit_logger import log_audit
from server.core.crypto import decrypt, encrypt
from server.db.engine import get_session
from server.enterprise_identity.feishu_tenant_verification import (
    FeishuTenantVerificationError,
    authenticate_feishu_user,
    build_feishu_authorization_url,
    discover_feishu_tenant_key,
    feishu_tenant_verification_callback_url,
    feishu_user_login_callback_url,
)
from server.enterprise_identity.service import upsert_external_user
from server.enterprise_identity.sharepoint_auth import (
    SharePointAuthenticationError,
    authenticate_sharepoint_user,
    build_sharepoint_authorization_url,
    sharepoint_user_login_callback_url,
)
from server.models import (
    AuditStatus,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    FeishuTenantVerificationState,
    FeishuUserLoginState,
    IdentitySourceProvider,
    SharePointUserLoginState,
    User,
    UserStatus,
)
from server.models.user_refresh_token import UserRefreshToken

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _feishu_verification_error(status_code: int) -> HTMLResponse:
    return HTMLResponse(
        "<h3>Feishu tenant verification failed</h3>",
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _feishu_login_error(status_code: int) -> HTMLResponse:
    return HTMLResponse(
        "<h3>Feishu login failed</h3>",
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _sharepoint_login_error(status_code: int) -> HTMLResponse:
    return HTMLResponse(
        "<h3>SharePoint login failed</h3>",
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _utc_now_comparable(dt: datetime) -> datetime:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return now.replace(tzinfo=None)
    return now


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserInfoResponse(BaseModel):
    id: str
    external_id: str
    display_name: str
    email: str | None
    role: str
    status: str


async def _active_login_sources(
    session: AsyncSession,
    provider: IdentitySourceProvider,
) -> list[EnterpriseIdentitySource]:
    return list(
        (
            await session.execute(
                select(EnterpriseIdentitySource).where(
                    EnterpriseIdentitySource.provider == provider,
                    EnterpriseIdentitySource.status == EnterpriseIdentitySourceStatus.ACTIVE,
                    EnterpriseIdentitySource.tenant_id.is_not(None),
                )
            )
        ).scalars()
    )


def _identity_source_selection(
    *,
    login_path: str,
    provider_name: str,
    sources: list[EnterpriseIdentitySource],
) -> HTMLResponse:
    links = "".join(
        f'<li><a href="{login_path}?{urlencode({"source_id": source.id})}">{escape(source.name)}</a></li>'
        for source in sources
    )
    return HTMLResponse(
        f"<h3>Select {provider_name} identity source</h3><ul>{links}</ul>",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/feishu/login")
async def start_feishu_login(
    source_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    sources = await _active_login_sources(session, IdentitySourceProvider.FEISHU)
    if source_id:
        source = next((item for item in sources if item.id == source_id), None)
        if source is None:
            return _feishu_login_error(409)
    elif len(sources) == 1:
        source = sources[0]
    elif sources:
        return _identity_source_selection(
            login_path="/auth/feishu/login",
            provider_name="Feishu",
            sources=sources,
        )
    else:
        return _feishu_login_error(409)
    if not source.config_ciphertext:
        return _feishu_login_error(409)
    try:
        config = json.loads(decrypt(source.config_ciphertext))
        app_id = config.get("app_id") if isinstance(config, dict) else None
        if not isinstance(app_id, str) or not app_id:
            raise ValueError("Feishu source configuration is invalid")
        redirect_uri = feishu_user_login_callback_url(get_config().server.public_base_url)
    except (ValueError, TypeError):
        return _feishu_login_error(409)
    state = secrets.token_urlsafe(32)
    session.add(
        FeishuUserLoginState(
            identity_source_id=source.id,
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    await session.commit()
    return RedirectResponse(
        build_feishu_authorization_url(
            app_id=app_id,
            redirect_uri=redirect_uri,
            state=state,
        ),
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/feishu/login/callback")
async def feishu_login_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    if not code or not state:
        return _feishu_login_error(400)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    state_record = (
        await session.execute(select(FeishuUserLoginState).where(FeishuUserLoginState.state_hash == state_hash))
    ).scalar_one_or_none()
    if state_record is None or state_record.expires_at < _utc_now_comparable(state_record.expires_at):
        return _feishu_login_error(400)
    source = await session.get(EnterpriseIdentitySource, state_record.identity_source_id)
    consumed = await session.execute(
        delete(FeishuUserLoginState).where(
            FeishuUserLoginState.id == state_record.id,
            FeishuUserLoginState.state_hash == state_hash,
        )
    )
    await session.commit()
    if consumed.rowcount != 1 or source is None:  # type: ignore[attr-defined]
        return _feishu_login_error(400)
    if (
        source.provider != IdentitySourceProvider.FEISHU
        or source.status != EnterpriseIdentitySourceStatus.ACTIVE
        or source.tenant_id is None
        or not source.config_ciphertext
    ):
        return _feishu_login_error(400)
    try:
        config = json.loads(decrypt(source.config_ciphertext))
        app_id = config.get("app_id") if isinstance(config, dict) else None
        app_secret = config.get("app_secret") if isinstance(config, dict) else None
        if not isinstance(app_id, str) or not isinstance(app_secret, str):
            raise FeishuTenantVerificationError("Feishu source configuration is invalid")
        identity = await authenticate_feishu_user(
            app_id=app_id,
            app_secret=app_secret,
            code=code,
            redirect_uri=feishu_user_login_callback_url(get_config().server.public_base_url),
        )
        if not secrets.compare_digest(identity["tenant_key"], source.tenant_id):
            raise FeishuTenantVerificationError("Feishu tenant does not match source")
        user = await upsert_external_user(
            session,
            source,
            external_user_id=identity["user_id"],
            display_name=identity["display_name"],
            email=identity["email"],
        )
        if user.status != UserStatus.ACTIVE:
            raise FeishuTenantVerificationError("PAS user is disabled")
    except (FeishuTenantVerificationError, ValueError, httpx.HTTPError):
        await session.rollback()
        return _feishu_login_error(400)
    access_token = create_access_token({"sub": user.id, "role": user.role.value})
    refresh_token = _create_refresh_record(session, user.id)
    await session.commit()
    response = RedirectResponse("/dashboard", status_code=303, headers={"Cache-Control": "no-store"})
    config = get_config()
    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        max_age=config.auth.jwt.access_token_expire_minutes * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        max_age=_refresh_cookie_max_age(),
    )
    return response


def _sharepoint_source_config(source: EnterpriseIdentitySource) -> tuple[str, str, str]:
    if not source.config_ciphertext:
        raise SharePointAuthenticationError("SharePoint source configuration is missing")
    try:
        config = json.loads(decrypt(source.config_ciphertext))
    except (BinasciiError, InvalidTag, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SharePointAuthenticationError("SharePoint source configuration is invalid") from exc
    client_id = config.get("client_id") if isinstance(config, dict) else None
    client_secret = config.get("client_secret") if isinstance(config, dict) else None
    cloud = config.get("cloud", "global") if isinstance(config, dict) else None
    if (
        not isinstance(client_id, str)
        or not client_id
        or not isinstance(client_secret, str)
        or not client_secret
        or cloud not in {"global", "china"}
    ):
        raise SharePointAuthenticationError("SharePoint source configuration is invalid")
    return client_id, client_secret, cloud


@router.get("/sharepoint/login")
async def start_sharepoint_login(
    source_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    sources = await _active_login_sources(session, IdentitySourceProvider.SHAREPOINT)
    if source_id:
        source = next((item for item in sources if item.id == source_id), None)
        if source is None:
            return _sharepoint_login_error(409)
    elif len(sources) == 1:
        source = sources[0]
    elif sources:
        return _identity_source_selection(
            login_path="/auth/sharepoint/login",
            provider_name="SharePoint",
            sources=sources,
        )
    else:
        return _sharepoint_login_error(409)
    try:
        client_id, client_secret, cloud = _sharepoint_source_config(source)
        redirect_uri = sharepoint_user_login_callback_url(get_config().server.public_base_url)
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        authorization_url, code_verifier = await build_sharepoint_authorization_url(
            tenant_id=source.tenant_id or "",
            client_id=client_id,
            client_secret=client_secret,
            cloud=cloud,
            redirect_uri=redirect_uri,
            state=state,
            nonce=nonce,
        )
    except (SharePointAuthenticationError, ValueError):
        return _sharepoint_login_error(409)
    session.add(
        SharePointUserLoginState(
            identity_source_id=source.id,
            state_hash=hashlib.sha256(state.encode()).hexdigest(),
            nonce_ciphertext=encrypt(nonce),
            code_verifier_ciphertext=encrypt(code_verifier),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    )
    await session.commit()
    return RedirectResponse(
        authorization_url,
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/sharepoint/login/callback")
async def sharepoint_login_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    if not code or not state:
        return _sharepoint_login_error(400)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    state_record = (
        await session.execute(select(SharePointUserLoginState).where(SharePointUserLoginState.state_hash == state_hash))
    ).scalar_one_or_none()
    if state_record is None or state_record.expires_at < _utc_now_comparable(state_record.expires_at):
        return _sharepoint_login_error(400)
    source = await session.get(EnterpriseIdentitySource, state_record.identity_source_id)
    consumed = await session.execute(
        delete(SharePointUserLoginState).where(
            SharePointUserLoginState.id == state_record.id,
            SharePointUserLoginState.state_hash == state_hash,
        )
    )
    await session.commit()
    if consumed.rowcount != 1 or source is None:  # type: ignore[attr-defined]
        return _sharepoint_login_error(400)
    if (
        source.provider != IdentitySourceProvider.SHAREPOINT
        or source.status != EnterpriseIdentitySourceStatus.ACTIVE
        or source.tenant_id is None
    ):
        return _sharepoint_login_error(400)
    try:
        client_id, client_secret, cloud = _sharepoint_source_config(source)
        nonce = decrypt(state_record.nonce_ciphertext)
        identity = await authenticate_sharepoint_user(
            tenant_id=source.tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            cloud=cloud,
            code=code,
            redirect_uri=sharepoint_user_login_callback_url(get_config().server.public_base_url),
            code_verifier=decrypt(state_record.code_verifier_ciphertext),
            expected_nonce=nonce,
        )
        user = await upsert_external_user(
            session,
            source,
            external_user_id=identity.subject,
            display_name=identity.display_name or identity.subject,
            email=identity.email,
        )
        if user.status != UserStatus.ACTIVE:
            raise SharePointAuthenticationError("PAS user is disabled")
    except (
        OIDCAuthenticationError,
        SharePointAuthenticationError,
        ValueError,
        httpx.HTTPError,
    ):
        await session.rollback()
        return _sharepoint_login_error(400)
    access_token = create_access_token({"sub": user.id, "role": user.role.value})
    refresh_token = _create_refresh_record(session, user.id)
    await session.commit()
    response = RedirectResponse("/dashboard", status_code=303, headers={"Cache-Control": "no-store"})
    config = get_config()
    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        max_age=config.auth.jwt.access_token_expire_minutes * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        max_age=_refresh_cookie_max_age(),
    )
    return response


@router.get("/feishu/tenant-verification/callback")
async def feishu_tenant_verification_callback(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    code = request.query_params.get("code", "")
    state = request.query_params.get("state", "")
    if not code or not state:
        return _feishu_verification_error(400)

    state_hash = hashlib.sha256(state.encode()).hexdigest()
    state_record = (
        await session.execute(
            select(FeishuTenantVerificationState).where(FeishuTenantVerificationState.state_hash == state_hash)
        )
    ).scalar_one_or_none()
    if state_record is None:
        return _feishu_verification_error(400)
    if state_record.expires_at < _utc_now_comparable(state_record.expires_at):
        await session.delete(state_record)
        await session.commit()
        return _feishu_verification_error(400)

    source = await session.get(EnterpriseIdentitySource, state_record.identity_source_id)
    consumed = await session.execute(
        delete(FeishuTenantVerificationState).where(
            FeishuTenantVerificationState.id == state_record.id,
            FeishuTenantVerificationState.state_hash == state_hash,
        )
    )
    await session.commit()
    if consumed.rowcount != 1 or source is None:  # type: ignore[attr-defined]
        return _feishu_verification_error(400)
    if (
        source.provider.value != "feishu"
        or source.tenant_id is not None
        or source.status != EnterpriseIdentitySourceStatus.PENDING_TENANT_VERIFICATION
        or not source.config_ciphertext
    ):
        return _feishu_verification_error(400)

    try:
        config = json.loads(decrypt(source.config_ciphertext))
        app_id = config.get("app_id") if isinstance(config, dict) else None
        app_secret = config.get("app_secret") if isinstance(config, dict) else None
        if not isinstance(app_id, str) or not isinstance(app_secret, str):
            raise FeishuTenantVerificationError("Feishu source configuration is invalid")
        redirect_uri = feishu_tenant_verification_callback_url(get_config().server.public_base_url)
        tenant_key = await discover_feishu_tenant_key(
            app_id=app_id,
            app_secret=app_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except (FeishuTenantVerificationError, ValueError, httpx.HTTPError):
        return _feishu_verification_error(400)

    source.tenant_id = tenant_key
    source.status = EnterpriseIdentitySourceStatus.PENDING_BINDING
    source.last_error = None
    try:
        await log_audit(
            session,
            user_id=state_record.created_by_user_id,
            action="identity_source.feishu_verification_complete",
            target_type="enterprise_identity_source",
            target_id=source.id,
            status=AuditStatus.SUCCESS,
            required=True,
            commit=False,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        return _feishu_verification_error(400)
    return RedirectResponse(
        url="/users?identity_source_verified=1",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _secure_cookie(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded.split(",", 1)[0].strip() or request.url.scheme
    return scheme == "https"


def _refresh_cookie_max_age() -> int:
    return get_config().auth.jwt.refresh_token_expire_days * 86400


def _create_refresh_record(session: AsyncSession, user_id: str, family: str | None = None) -> str:
    """Create + add a UserRefreshToken, return the opaque token (caller commits)."""
    config = get_config().auth.jwt
    token = secrets.token_urlsafe(32)
    record = UserRefreshToken(
        user_id=user_id,
        token_hash=_hash_token(token),
        token_family=family or str(uuid.uuid4()),
        expires_at=datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=config.refresh_token_expire_days),
    )
    session.add(record)
    return token


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Login with builtin credentials (works in all auth modes for builtin users)."""
    config = get_config()
    try:
        await check_builtin_login(request, body.username)
    except AuthRateLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication requests.",
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    user = await authenticate_builtin(session, body.username, body.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
        )

    from server.models import UserStatus

    if user.status == UserStatus.DISABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been disabled. Contact admin.",
        )

    access_token = create_access_token({"sub": user.id, "role": user.role.value})
    refresh_token = _create_refresh_record(session, user.id)
    await session.commit()

    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        max_age=config.auth.jwt.access_token_expire_minutes * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        max_age=_refresh_cookie_max_age(),
    )

    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Rotate refresh token + issue new access token. Reuse/expiry revokes family."""
    token = request.cookies.get("refresh_token")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token.")
    token_hash = _hash_token(token)

    # Lookup including already-revoked rows (so reuse is detectable).
    result = await session.execute(select(UserRefreshToken).where(UserRefreshToken.token_hash == token_hash))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token.")

    now = datetime.now(timezone.utc)

    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    # Reuse or expiry -> revoke whole family.
    if record.revoked_at is not None or expires_at < now:
        await session.execute(
            update(UserRefreshToken)
            .where(
                UserRefreshToken.token_family == record.token_family,
                UserRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()
        logger.warning("Refresh token reuse/expiry detected, family revoked: %s", record.token_family)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token no longer valid.")

    # CAS revoke the current token.
    cas = await session.execute(
        update(UserRefreshToken)
        .where(
            UserRefreshToken.id == record.id,
            UserRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    if cas.rowcount == 0:  # type: ignore[attr-defined]
        # Concurrent consumption: revoke family.
        await session.execute(
            update(UserRefreshToken)
            .where(
                UserRefreshToken.token_family == record.token_family,
                UserRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        await session.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token no longer valid.")

    config = get_config()
    access_token = create_access_token({"sub": record.user_id})
    new_refresh = _create_refresh_record(session, record.user_id, family=record.token_family)
    await session.commit()

    response.set_cookie(
        key="session_token",
        value=access_token,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        max_age=config.auth.jwt.access_token_expire_minutes * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=new_refresh,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        max_age=_refresh_cookie_max_age(),
    )
    return TokenResponse(access_token=access_token)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Revoke refresh token (if present) and clear both cookies."""
    token = request.cookies.get("refresh_token")
    if token:
        token_hash = _hash_token(token)
        await session.execute(
            update(UserRefreshToken)
            .where(
                UserRefreshToken.token_hash == token_hash,
                UserRefreshToken.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(timezone.utc))
        )
        await session.commit()
    response.delete_cookie(
        "session_token",
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
    )
    response.delete_cookie(
        "refresh_token",
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
    )
    return {"message": "Logged out"}


@router.get("/me", response_model=UserInfoResponse)
async def get_me(user: User = Depends(get_current_user)):
    """Get current user info."""
    return UserInfoResponse(
        id=user.id,
        external_id=user.external_id,
        display_name=user.display_name,
        email=user.email,
        role=user.role.value,
        status=user.status.value,
    )


@router.get("/mode")
async def get_auth_mode():
    """Return auth mode so frontend can conditionally show password UI."""
    config = get_config()
    return {"mode": config.auth.mode}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Change own password (builtin auth mode only)."""
    from server.auth.builtin import verify_password, hash_password
    from server.models import AuthProvider

    if user.auth_provider != AuthProvider.BUILTIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password change is only available for builtin auth users.",
        )
    if not user.password_hash or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect.",
        )
    if len(body.new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 8 characters.",
        )

    user.password_hash = hash_password(body.new_password)
    await session.execute(
        update(UserRefreshToken)
        .where(
            UserRefreshToken.user_id == user.id,
            UserRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await session.commit()

    response.delete_cookie(
        "session_token",
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
    )
    response.delete_cookie(
        "refresh_token",
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
    )
    return {"message": "Password changed successfully"}
