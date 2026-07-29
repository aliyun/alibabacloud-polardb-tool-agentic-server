from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import urlparse, urlunparse

import jwt
from jwt import PyJWTError
from pydantic import AnyUrl
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from server.auth.jwt_manager import _load_keys, get_public_key
from server.auth.principal import (
    PrincipalKind,
    agent_subject,
    parse_subject,
    user_subject,
)
from server.config import AppConfig
from server.core.agent_token_service import hash_agent_token
from server.core.crypto import decrypt, encrypt
from server.models import Agent, AgentAPIToken, AgentStatus
from server.models.oauth import (
    OAuthAuthorizationCode,
    OAuthDeniedJTI,
    OAuthPendingAuth,
    OAuthRefreshToken,
    OAuthRegisteredClient,
)

logger = logging.getLogger(__name__)

# ─── JTI Deny-list Cache ──────────────────────────────────────────────
# In-memory cache of revoked JWT IDs to short-circuit DB lookups when
# validating access tokens. Backed by an async lock for concurrency safety.

_jti_deny_cache: dict[str, float] = {}
_jti_deny_lock = asyncio.Lock()
_JTI_CACHE_MAX = 4096
_agent_usage_tasks: set[asyncio.Task[None]] = set()


async def _record_agent_token_use(
    session_factory: async_sessionmaker[AsyncSession],
    token_id: str,
    used_at: datetime,
) -> None:
    """Best-effort CAS update; authentication never depends on telemetry."""
    try:
        async with session_factory() as session:
            await session.execute(
                update(AgentAPIToken)
                .where(
                    AgentAPIToken.id == token_id,
                    (
                        AgentAPIToken.last_used_at.is_(None)
                        | (
                            AgentAPIToken.last_used_at
                            <= used_at - timedelta(minutes=5)
                        )
                    ),
                )
                .values(last_used_at=used_at)
            )
            await session.commit()
    except Exception:
        logger.warning(
            "agent token usage telemetry update failed",
            extra={"action": "agent_token_usage_telemetry"},
        )


def _schedule_agent_token_use(
    session_factory: async_sessionmaker[AsyncSession],
    token_id: str,
    used_at: datetime,
) -> None:
    task = asyncio.create_task(
        _record_agent_token_use(session_factory, token_id, used_at)
    )
    _agent_usage_tasks.add(task)

    def finish(completed: asyncio.Task[None]) -> None:
        _agent_usage_tasks.discard(completed)
        try:
            completed.result()
        except Exception:
            logger.warning(
                "agent token usage telemetry task failed",
                extra={"action": "agent_token_usage_telemetry"},
            )

    task.add_done_callback(finish)


async def _jti_is_denied_cached(jti: str) -> bool | None:
    """Check in-memory cache. Returns True/False if cached, None if miss."""
    async with _jti_deny_lock:
        exp = _jti_deny_cache.get(jti)
        if exp is None:
            return None
        if time.time() > exp:
            _jti_deny_cache.pop(jti, None)
            return None
        return True


async def _jti_cache_deny(jti: str, expires_at: float) -> None:
    async with _jti_deny_lock:
        if len(_jti_deny_cache) >= _JTI_CACHE_MAX:
            now = time.time()
            expired = [k for k, v in _jti_deny_cache.items() if now > v]
            for k in expired:
                del _jti_deny_cache[k]
        _jti_deny_cache[jti] = expires_at


def _normalize_resource_url(url: str) -> str:
    """Normalize a resource URL per RFC 8707 for comparison."""
    parsed = urlparse(url)
    return urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        parsed.path.rstrip("/"),
        parsed.params,
        parsed.query,
        "",
    ))


class PASAuthProvider:
    """MCP OAuth authorization server provider backed by PolarDB/SQLite."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
    ):
        self._session_factory = session_factory
        self._config = config

    # ─── Client Registration & Lookup ─────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Look up a registered OAuth client by ``client_id``.

        Returns the full client information (with decrypted secret) or
        ``None`` if the client is unknown or its secret cannot be decrypted.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(OAuthRegisteredClient).where(
                    OAuthRegisteredClient.client_id == client_id
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None

            secret = None
            if row.client_secret_enc:
                try:
                    secret = decrypt(row.client_secret_enc)
                except Exception:
                    logger.warning(
                        "Failed to decrypt client secret for %s", client_id
                    )
                    return None

            return OAuthClientInformationFull(
                client_id=row.client_id,
                client_secret=secret,
                client_id_issued_at=row.client_id_issued_at,
                client_secret_expires_at=row.client_secret_expires_at,
                redirect_uris=(
                    json.loads(row.redirect_uris) if row.redirect_uris else []
                ),
                grant_types=(
                    json.loads(row.grant_types)
                    if row.grant_types
                    else ["authorization_code", "refresh_token"]
                ),
                response_types=(
                    json.loads(row.response_types)
                    if row.response_types
                    else ["code"]
                ),
                token_endpoint_auth_method=cast(Any, row.token_endpoint_auth_method),
                scope=row.scope,
                client_name=row.client_name,
            )

    async def register_client(
        self, client_info: OAuthClientInformationFull
    ) -> None:
        """Persist a newly registered OAuth client (DCR endpoint).

        The client secret, if present, is encrypted at rest.
        """
        async with self._session_factory() as session:
            secret_enc = None
            if client_info.client_secret:
                secret_enc = encrypt(client_info.client_secret)

            row = OAuthRegisteredClient(
                client_id=client_info.client_id,
                client_secret_enc=secret_enc,
                client_id_issued_at=client_info.client_id_issued_at,
                client_secret_expires_at=client_info.client_secret_expires_at,
                redirect_uris=json.dumps(
                    [str(u) for u in (client_info.redirect_uris or [])]
                ),
                grant_types=json.dumps(client_info.grant_types),
                response_types=json.dumps(client_info.response_types),
                token_endpoint_auth_method=client_info.token_endpoint_auth_method,
                scope=client_info.scope,
                client_name=client_info.client_name,
            )
            session.add(row)
            await session.commit()

    # ─── Authorization Flow ───────────────────────────────────────────

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Begin an authorization request and return the next-step URL.

        Validates the requested ``resource`` (RFC 8707), persists a pending
        authorization session, and returns either the local login URL
        (builtin mode) or the SSO redirect URL (OIDC mode).
        """
        resource_url = f"{self._config.server.public_base_url}/mcp"

        if params.resource is not None:
            if _normalize_resource_url(str(params.resource)) != _normalize_resource_url(resource_url):
                raise AuthorizeError(
                    error="invalid_request",
                    error_description=f"Invalid resource. Expected: {resource_url}",
                )
        else:
            logger.warning(
                "authorize() called without resource parameter, defaulting to %s",
                resource_url,
            )

        # Store the effective resource (always non-null)
        effective_resource = resource_url

        # Generate idp_state upfront (used by OIDC mode for callback correlation)
        idp_state = str(uuid.uuid4())

        async with self._session_factory() as session:
            pending = OAuthPendingAuth(
                client_id=client.client_id,
                redirect_uri=str(params.redirect_uri),
                code_challenge=params.code_challenge,
                code_challenge_method="S256",
                resource=effective_resource,
                scopes=json.dumps(params.scopes or []),
                state=params.state,
                idp_state=idp_state,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
            session.add(pending)
            await session.commit()
            await session.refresh(pending)
            session_id = pending.session_id

        if self._config.auth.mode == "builtin":
            return f"/mcp-auth/login?session_id={session_id}"

        # OIDC mode: redirect to external Identity Provider
        from server.auth.identity_federation import IdentityFederation

        federation = IdentityFederation(
            self._config.auth.oidc,
            provider_name=self._config.auth.oidc.provider_name,
        )
        await federation.discover_endpoints()

        callback_url = (
            self._config.auth.oidc.redirect_uri
            or f"{self._config.server.public_base_url}/auth/oidc/callback"
        )
        authorize_url, idp_code_verifier = federation.build_authorize_url(
            redirect_uri=callback_url,
            state=idp_state,
        )

        # Store authorize_url (and code_verifier if IdP PKCE is enabled)
        async with self._session_factory() as session:
            from sqlalchemy import update as sa_update
            values: dict[str, str | None] = {
                "idp_authorize_url": authorize_url,
            }
            if idp_code_verifier:
                values["idp_code_verifier_enc"] = encrypt(idp_code_verifier)
            await session.execute(
                sa_update(OAuthPendingAuth)
                .where(OAuthPendingAuth.session_id == session_id)
                .values(**values)
            )
            await session.commit()

        return f"/mcp-auth/sso-redirect?session_id={session_id}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        """Resolve an opaque authorization code to its stored grant.

        Detects replay (already-consumed codes) and proactively revokes
        any refresh tokens minted from the same code.
        """
        code_hash = hashlib.sha256(authorization_code.encode()).hexdigest()

        async with self._session_factory() as session:
            result = await session.execute(
                select(OAuthAuthorizationCode).where(
                    OAuthAuthorizationCode.code_hash == code_hash
                )
            )
            row = result.scalar_one_or_none()

            if row is None:
                return None

            if row.consumed_at is not None:
                # Replay attack detected — revoke all refresh tokens tied to this code
                logger.warning(
                    "Replay attack detected: authorization code %s already consumed",
                    code_hash[:8],
                )
                await session.execute(
                    update(OAuthRefreshToken)
                    .where(OAuthRefreshToken.code_id == code_hash)
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                await session.commit()
                return None

            # SQLite strips tzinfo from stored datetimes.  When read back
            # the naive datetime must be treated as UTC, otherwise
            # .timestamp() assumes local time and the expiry is wrong.
            expires_dt = row.expires_at
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)

            return AuthorizationCode(
                code=authorization_code,
                scopes=json.loads(row.scopes),
                expires_at=expires_dt.timestamp(),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=AnyUrl(row.redirect_uri),
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
                subject=user_subject(row.user_id),
            )

    # ─── Token Issuance & Refresh ─────────────────────────────────────

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Exchange an authorization code for an access + refresh token pair.

        Atomically marks the code consumed (CAS) to prevent replay; on
        replay, revokes the entire refresh-token family bound to the code.
        """
        code_hash = hashlib.sha256(authorization_code.code.encode()).hexdigest()
        config = self._config
        subject = authorization_code.subject
        if not isinstance(subject, str):
            raise ValueError("Authorization code subject must be a User")
        principal = parse_subject(subject)
        if principal.kind != PrincipalKind.USER:
            raise ValueError("Authorization code subject must be a User")

        # 1. Atomically mark code as consumed (CAS on consumed_at)
        async with self._session_factory() as session:
            result = await session.execute(
                update(OAuthAuthorizationCode)
                .where(
                    OAuthAuthorizationCode.code_hash == code_hash,
                    OAuthAuthorizationCode.consumed_at.is_(None),
                )
                .values(consumed_at=datetime.now(timezone.utc))
            )
            await session.commit()
            if result.rowcount == 0:  # type: ignore[attr-defined]
                # Already consumed — replay attack; revoke refresh token family
                logger.warning(
                    "Authorization code replay detected during exchange: %s",
                    code_hash[:8],
                )
                await session.execute(
                    update(OAuthRefreshToken)
                    .where(OAuthRefreshToken.code_id == code_hash)
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                await session.commit()
                raise ValueError("Authorization code already consumed")

        # 2. Sign access token JWT
        private_key, _ = _load_keys()
        jti = str(uuid.uuid4())
        now = int(time.time())
        access_payload = {
            "sub": authorization_code.subject,
            "aud": authorization_code.resource
            or f"{config.server.public_base_url}/mcp",
            "jti": jti,
            "iat": now,
            "exp": now + config.auth.jwt.access_token_expire_minutes * 60,
            "type": "access",
            "client_id": authorization_code.client_id,
            "scope": " ".join(authorization_code.scopes),
        }
        access_token = jwt.encode(access_payload, private_key, algorithm="RS256")

        # 3. Generate refresh token
        refresh_token = secrets.token_urlsafe(32)

        # 4. Store refresh token in DB
        async with self._session_factory() as session:
            rt = OAuthRefreshToken(
                token_hash=hashlib.sha256(refresh_token.encode()).hexdigest(),
                client_id=authorization_code.client_id,
                user_id=principal.id,
                code_id=code_hash,
                token_family=str(uuid.uuid4()),
                scopes=json.dumps(authorization_code.scopes),
                resource=authorization_code.resource,
                expires_at=datetime.now(timezone.utc)
                + timedelta(days=config.auth.jwt.refresh_token_expire_days),
            )
            session.add(rt)
            await session.commit()

        # 5. Return OAuthToken
        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=config.auth.jwt.access_token_expire_minutes * 60,
            scope=" ".join(authorization_code.scopes),
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """Resolve a refresh token string to its stored grant.

        Detects token reuse (already-revoked tokens) and revokes the entire
        token family on suspicion of compromise.
        """
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()

        async with self._session_factory() as session:
            result = await session.execute(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.token_hash == token_hash
                )
            )
            row = result.scalar_one_or_none()

            if row is None:
                return None

            if row.revoked_at is not None:
                # Reuse detection: revoke ALL tokens in the same family
                logger.warning(
                    "Refresh token reuse detected, revoking token family %s",
                    row.token_family,
                )
                await session.execute(
                    update(OAuthRefreshToken)
                    .where(
                        OAuthRefreshToken.token_family == row.token_family,
                        OAuthRefreshToken.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                await session.commit()
                return None

            # SQLite strips tzinfo — treat naive as UTC (same as auth codes)
            rt_expires = row.expires_at
            if rt_expires is not None and rt_expires.tzinfo is None:
                rt_expires = rt_expires.replace(tzinfo=timezone.utc)

            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=json.loads(row.scopes),
                expires_at=(
                    int(rt_expires.timestamp()) if rt_expires else None
                ),
                subject=user_subject(row.user_id),
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate a refresh token and issue a fresh access token.

        Atomically revokes the old token (CAS); on reuse, revokes the
        whole token family. The new refresh token inherits the family id.
        """
        old_hash = hashlib.sha256(refresh_token.token.encode()).hexdigest()
        config = self._config

        # 1. Atomically revoke old refresh token (CAS on revoked_at)
        async with self._session_factory() as session:
            cas = await session.execute(
                update(OAuthRefreshToken)
                .where(
                    OAuthRefreshToken.token_hash == old_hash,
                    OAuthRefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(timezone.utc))
            )
            await session.commit()

            if cas.rowcount == 0:  # type: ignore[attr-defined]
                logger.warning(
                    "Refresh token reuse detected during exchange: %s",
                    old_hash[:8],
                )
                family_result = await session.execute(
                    select(OAuthRefreshToken.token_family).where(
                        OAuthRefreshToken.token_hash == old_hash
                    )
                )
                family_row = family_result.scalar_one_or_none()
                if family_row:
                    await session.execute(
                        update(OAuthRefreshToken)
                        .where(
                            OAuthRefreshToken.token_family == family_row,
                            OAuthRefreshToken.revoked_at.is_(None),
                        )
                        .values(revoked_at=datetime.now(timezone.utc))
                    )
                    await session.commit()
                raise ValueError("Refresh token already consumed")

            row = await session.execute(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.token_hash == old_hash
                )
            )
            old_row = row.scalar_one_or_none()
            if old_row is None:
                raise ValueError("Refresh token not found")
            token_family = old_row.token_family
            code_id = old_row.code_id
            user_id = old_row.user_id
            resource = old_row.resource

        # 2. Generate new refresh token
        new_refresh_token = secrets.token_urlsafe(32)

        # 3. Store new refresh token with same token_family
        async with self._session_factory() as session:
            rt = OAuthRefreshToken(
                token_hash=hashlib.sha256(new_refresh_token.encode()).hexdigest(),
                client_id=refresh_token.client_id,
                user_id=user_id,
                code_id=code_id,
                token_family=token_family,
                scopes=json.dumps(scopes if scopes else refresh_token.scopes),
                resource=resource,
                expires_at=datetime.now(timezone.utc)
                + timedelta(days=config.auth.jwt.refresh_token_expire_days),
            )
            session.add(rt)
            await session.commit()

        # 4. Sign new access JWT
        effective_scopes = scopes if scopes else refresh_token.scopes
        private_key, _ = _load_keys()
        jti = str(uuid.uuid4())
        now = int(time.time())
        access_payload = {
            "sub": refresh_token.subject,
            "aud": resource or f"{config.server.public_base_url}/mcp",
            "jti": jti,
            "iat": now,
            "exp": now + config.auth.jwt.access_token_expire_minutes * 60,
            "type": "access",
            "client_id": refresh_token.client_id,
            "scope": " ".join(effective_scopes),
        }
        access_token = jwt.encode(access_payload, private_key, algorithm="RS256")

        # 5. Return OAuthToken
        return OAuthToken(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=config.auth.jwt.access_token_expire_minutes * 60,
            scope=" ".join(effective_scopes),
        )

    # ─── Token Validation & Revocation ────────────────────────────────

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Verify a bearer access token and return its decoded grant.

        Agent tokens are opaque credentials resolved by hash. Other tokens
        follow the existing JWT signature, audience, and JTI deny-list path.
        Returns ``None`` for any validation failure.
        """
        if token.startswith("pas_agent_"):
            return await self._load_agent_access_token(token)

        try:
            payload = jwt.decode(
                token,
                get_public_key(),
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
        except PyJWTError:
            return None

        if payload.get("type") != "access":
            return None

        expected_aud = _normalize_resource_url(
            f"{self._config.server.public_base_url}/mcp"
        )
        token_aud = payload.get("aud", "")
        if _normalize_resource_url(token_aud) != expected_aud:
            return None

        jti = payload.get("jti")
        if jti:
            cached = await _jti_is_denied_cached(jti)
            if cached is True:
                return None
            if cached is None:
                async with self._session_factory() as session:
                    result = await session.execute(
                        select(OAuthDeniedJTI).where(OAuthDeniedJTI.jti == jti)
                    )
                    row = result.scalar_one_or_none()
                    if row is not None:
                        await _jti_cache_deny(jti, row.expires_at.timestamp())
                        return None

        return AccessToken(
            token=token,
            client_id=payload.get("client_id", ""),
            scopes=payload.get("scope", "").split(),
            expires_at=payload.get("exp"),
            resource=payload.get("aud"),
            subject=payload.get("sub"),
        )

    async def _load_agent_access_token(self, token: str) -> AccessToken | None:
        token_hash = hash_agent_token(token)
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentAPIToken, Agent)
                .join(Agent, Agent.id == AgentAPIToken.agent_id)
                .where(AgentAPIToken.token_hash == token_hash)
            )
            match = result.one_or_none()
            if match is None:
                return None
            row, agent = match
            if (
                agent.status != AgentStatus.ACTIVE
                or row.revoked_at is not None
            ):
                return None

            now = datetime.now(timezone.utc)
            expires_at = row.expires_at
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= now:
                    return None

            token_id = row.id
            access_token = AccessToken(
                token=token,
                client_id="agent-token",
                scopes=[],
                expires_at=int(expires_at.timestamp()) if expires_at else None,
                subject=agent_subject(row.agent_id),
            )
        _schedule_agent_token_use(self._session_factory, token_id, now)
        return access_token

    async def revoke_token(
        self, token: AccessToken | RefreshToken
    ) -> None:
        """Revoke an access or refresh token (RFC 7009).

        Access tokens are denied via JTI (cache + DB) until their natural
        expiry. Refresh tokens revoke the entire token family.
        """
        if isinstance(token, AccessToken):
            # Decode JWT to get jti and exp
            try:
                payload = jwt.decode(
                    token.token,
                    get_public_key(),
                    algorithms=["RS256"],
                    options={"verify_aud": False},
                )
            except PyJWTError:
                return
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                await _jti_cache_deny(jti, float(exp))
                async with self._session_factory() as session:
                    denied = OAuthDeniedJTI(
                        jti=jti,
                        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
                    )
                    session.add(denied)
                    await session.commit()
        elif isinstance(token, RefreshToken):
            token_hash = hashlib.sha256(token.token.encode()).hexdigest()
            async with self._session_factory() as session:
                result = await session.execute(
                    select(OAuthRefreshToken).where(
                        OAuthRefreshToken.token_hash == token_hash
                    )
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    # Revoke all tokens in the same family
                    await session.execute(
                        update(OAuthRefreshToken)
                        .where(
                            OAuthRefreshToken.token_family == row.token_family,
                            OAuthRefreshToken.revoked_at.is_(None),
                        )
                        .values(revoked_at=datetime.now(timezone.utc))
                    )
                    await session.commit()
