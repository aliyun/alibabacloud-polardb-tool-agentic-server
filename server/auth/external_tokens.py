from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, cast

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server.auth.identity_federation import IdentityFederation
from server.config import AppConfig
from server.core.crypto import decrypt
from server.core.user_workspace import ensure_user_workspace
from server.enterprise_identity.feishu_tenant_verification import (
    FEISHU_USER_INFO_URL,
)
from server.enterprise_identity.service import (
    identity_provider_key,
    upsert_external_user,
)
from server.models import (
    AuthProvider,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    User,
    UserRole,
    UserStatus,
)
from server.models.oauth import UserExternalIdentity

_MAX_RESPONSE_BYTES = 1_048_576
_REQUEST_TIMEOUT_SECONDS = 10.0


class ExternalTokenError(ValueError):
    pass


class ExternalTokenInvalid(ExternalTokenError):
    pass


class ExternalTokenUnavailable(ExternalTokenError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalTokenIdentity:
    provider_type: str
    provider_key: str
    provider_fingerprint: str
    subject: str
    display_name: str | None
    email: str | None
    scopes: tuple[str, ...]
    expires_at: datetime | None
    scope_authoritative: bool = True
    requires_existing_mapping: bool = False
    identity_source_id: str | None = None


def _normalized_subject(value: object, claim: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ExternalTokenInvalid(
            f"External identity claim '{claim}' is missing or invalid"
        )
    normalized = str(value).strip()
    if not normalized:
        raise ExternalTokenInvalid(
            f"External identity claim '{claim}' is missing or invalid"
        )
    return normalized


def _optional_string(value: object, claim: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExternalTokenInvalid(
            f"External identity claim '{claim}' is invalid"
        )
    return value.strip() or None


def _scopes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(dict.fromkeys(value.split()))
    if isinstance(value, list) and all(
        isinstance(item, str) for item in value
    ):
        return tuple(dict.fromkeys(item for item in value if item))
    return ()


def _expires_at(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise ExternalTokenInvalid("External token expiration is invalid")


def _audiences(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(
        isinstance(item, str) for item in value
    ):
        return set(value)
    return set()


class ExternalTokenAuthenticator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
    ) -> None:
        self._session_factory = session_factory
        self._config = config

    @property
    def enabled(self) -> bool:
        return self._config.auth.external_token_trust.enabled

    @property
    def direct_mcp_enabled(self) -> bool:
        trust = self._config.auth.external_token_trust
        return trust.enabled and trust.direct_mcp_enabled

    def _base_fingerprint(self) -> str:
        trust = self._config.auth.external_token_trust
        material = {
            "config_digest": trust.config_digest,
            "config_revision": trust.config_revision,
            "provider": trust.provider,
            "identity_source_id": trust.identity_source_id,
            "expected_audience": trust.expected_audience,
            "introspection_endpoint": trust.introspection_endpoint,
            "userinfo_endpoint": trust.userinfo_endpoint,
            "client_id": trust.client_id,
            "introspection_auth_method": trust.introspection_auth_method,
        }
        return hashlib.sha256(
            json.dumps(
                material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    async def validate(
        self,
        subject_token: str,
        *,
        identity_source_id: str | None = None,
        feishu_user_id: str | None = None,
        feishu_union_id: str | None = None,
    ) -> ExternalTokenIdentity:
        if not self.enabled:
            raise ExternalTokenInvalid("External token trust is disabled")
        if not subject_token or len(subject_token) > 65_536:
            raise ExternalTokenInvalid("External access token is invalid")

        provider = self._config.auth.external_token_trust.provider
        if provider != "feishu" and any(
            (identity_source_id, feishu_user_id, feishu_union_id)
        ):
            raise ExternalTokenInvalid(
                "External identity context is only supported for Feishu"
            )
        if provider == "oidc_jwt":
            return await self._validate_oidc_jwt(subject_token)
        if provider == "oauth2_introspection":
            return await self._validate_introspection(subject_token)
        if provider in {"oauth2_userinfo", "buc"}:
            return await self._validate_userinfo(
                subject_token,
                buc=provider == "buc",
            )
        if provider == "feishu":
            return await self._validate_feishu(
                subject_token,
                identity_source_id=identity_source_id,
                feishu_user_id=feishu_user_id,
                feishu_union_id=feishu_union_id,
            )
        raise ExternalTokenInvalid("External token provider is unsupported")

    async def resolve_user(
        self,
        session: AsyncSession,
        identity: ExternalTokenIdentity,
    ) -> User:
        trust = self._config.auth.external_token_trust
        if identity.provider_type == "feishu":
            source = await self._load_feishu_source(
                session,
                identity.identity_source_id or trust.identity_source_id,
            )
            if identity.provider_key != identity_provider_key(source):
                raise ExternalTokenInvalid(
                    "External identity provider changed during authentication"
                )
            feishu_user = await upsert_external_user(
                session,
                source,
                external_user_id=identity.subject,
                display_name=identity.display_name or identity.subject,
                email=identity.email,
            )
            await ensure_user_workspace(session, feishu_user.id)
            return feishu_user

        mapping = await session.scalar(
            select(UserExternalIdentity).where(
                UserExternalIdentity.identity_provider
                == identity.provider_key,
                UserExternalIdentity.external_subject == identity.subject,
            )
        )
        if mapping is not None:
            mapped_user = await session.get(User, mapping.user_id)
            if mapped_user is None:
                raise ExternalTokenInvalid(
                    "External identity references a missing PAS user"
                )
            await ensure_user_workspace(session, mapped_user.id)
            return mapped_user
        if identity.requires_existing_mapping:
            raise ExternalTokenInvalid(
                "External user is not mapped to the configured identity source"
            )

        try:
            async with session.begin_nested():
                user = User(
                    external_id=(
                        f"{identity.provider_key}:{identity.subject}"
                    ),
                    display_name=(
                        identity.display_name or identity.subject
                    ),
                    email=identity.email,
                    auth_provider=AuthProvider.OIDC,
                    role=UserRole.MEMBER,
                    status=UserStatus.ACTIVE,
                )
                session.add(user)
                await session.flush()
                session.add(
                    UserExternalIdentity(
                        user_id=user.id,
                        identity_provider=identity.provider_key,
                        external_subject=identity.subject,
                    )
                )
                await session.flush()
                await ensure_user_workspace(session, user.id)
        except IntegrityError:
            mapping = await session.scalar(
                select(UserExternalIdentity).where(
                    UserExternalIdentity.identity_provider
                    == identity.provider_key,
                    UserExternalIdentity.external_subject
                    == identity.subject,
                )
            )
            if mapping is None:
                raise
            recovered_user = await session.get(User, mapping.user_id)
            if recovered_user is None:
                raise ExternalTokenInvalid(
                    "External identity references a missing PAS user"
                )
            await ensure_user_workspace(session, recovered_user.id)
            return recovered_user
        return user

    async def _json_request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=_REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            ) as client:
                response = await client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            raise ExternalTokenUnavailable(
                "External identity provider is temporarily unavailable"
            ) from exc
        if response.status_code >= 500:
            raise ExternalTokenUnavailable(
                "External identity provider is temporarily unavailable"
            )
        if response.status_code >= 400:
            raise ExternalTokenInvalid("External access token was rejected")
        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ExternalTokenInvalid(
                "External identity response is too large"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExternalTokenInvalid(
                "External identity response must be valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise ExternalTokenInvalid(
                "External identity response must be a JSON object"
            )
        return cast(dict[str, Any], payload)

    async def _validate_oidc_jwt(
        self,
        subject_token: str,
    ) -> ExternalTokenIdentity:
        oidc = self._config.auth.oidc
        federation = IdentityFederation(oidc, oidc.provider_name)
        try:
            endpoints = await federation.discover_endpoints()
        except (httpx.HTTPError, ValueError) as exc:
            raise ExternalTokenUnavailable(
                "OIDC discovery is temporarily unavailable"
            ) from exc
        if not endpoints.jwks_uri:
            raise ExternalTokenInvalid(
                "OIDC JWT validation requires a JWKS URI"
            )
        jwks = await self._json_request("GET", endpoints.jwks_uri)
        keys = jwks.get("keys")
        if not isinstance(keys, list) or any(
            not isinstance(item, dict) for item in keys
        ):
            raise ExternalTokenInvalid("OIDC JWKS response is invalid")
        try:
            header = jwt.get_unverified_header(subject_token)
            if header.get("typ") not in {"at+jwt", "application/at+jwt"}:
                raise ExternalTokenInvalid(
                    "JWT access token typ must identify an access token"
                )
            matching = [
                item
                for item in keys
                if not header.get("kid") or item.get("kid") == header["kid"]
            ]
            if len(matching) != 1:
                raise ExternalTokenInvalid(
                    "Cannot select a unique JWT access token signing key"
                )
            signing_key = jwt.PyJWK.from_dict(matching[0]).key
            audience = (
                self._config.auth.external_token_trust.expected_audience
                or oidc.client_id
            )
            claims = jwt.decode(
                subject_token,
                signing_key,
                algorithms=oidc.id_token_algorithms,
                audience=audience,
                issuer=endpoints.issuer,
                options={
                    "require": [
                        "iss",
                        "exp",
                        "aud",
                        "sub",
                        "client_id",
                        "iat",
                        "jti",
                    ]
                },
            )
        except ExternalTokenInvalid:
            raise
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise ExternalTokenInvalid(
                "JWT access token validation failed"
            ) from exc
        return self._identity_from_claims(
            claims,
            provider_type="oidc_jwt",
            provider_key=oidc.provider_name,
            subject_claim="sub",
        )

    async def _validate_introspection(
        self,
        subject_token: str,
    ) -> ExternalTokenIdentity:
        trust = self._config.auth.external_token_trust
        endpoint = trust.introspection_endpoint
        if not endpoint:
            raise ExternalTokenInvalid(
                "OAuth token introspection endpoint is not configured"
            )
        oidc = self._config.auth.oidc
        validation_client_id = trust.client_id or oidc.client_id
        validation_client_secret = trust.client_secret or oidc.client_secret
        if not validation_client_id or not validation_client_secret:
            raise ExternalTokenInvalid(
                "OAuth token introspection client credentials are not configured"
            )
        data = {
            "token": subject_token,
            "token_type_hint": "access_token",
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if trust.introspection_auth_method == "client_secret_basic":
            credentials = (
                f"{validation_client_id}:{validation_client_secret}".encode(
                    "utf-8"
                )
            )
            headers["Authorization"] = (
                "Basic " + base64.b64encode(credentials).decode("ascii")
            )
        else:
            data["client_id"] = validation_client_id
            data["client_secret"] = validation_client_secret
        claims = await self._json_request(
            "POST",
            endpoint,
            data=data,
            headers=headers,
        )
        if claims.get("active") is not True:
            raise ExternalTokenInvalid("External access token is inactive")
        expected_audience = trust.expected_audience
        if expected_audience and expected_audience not in _audiences(
            claims.get("aud")
        ):
            raise ExternalTokenInvalid(
                "External access token audience is invalid"
            )
        if trust.userinfo_endpoint:
            return await self._validate_introspection_userinfo(
                subject_token,
                claims,
            )
        return self._identity_from_claims(
            claims,
            provider_type="oauth2_introspection",
            provider_key=oidc.provider_name,
            subject_claim=oidc.user_id_claim,
        )

    async def _validate_introspection_userinfo(
        self,
        subject_token: str,
        introspection_claims: dict[str, Any],
    ) -> ExternalTokenIdentity:
        trust = self._config.auth.external_token_trust
        endpoint = trust.userinfo_endpoint
        if not endpoint:
            raise ExternalTokenInvalid(
                "External UserInfo endpoint is not configured"
            )
        async with self._session_factory() as session:
            source = await self._load_active_identity_source(
                session,
                trust.identity_source_id,
            )
            provider_key = identity_provider_key(source)
            source_id = source.id
            source_fingerprint = hashlib.sha256(
                (
                    f"{source.id}:{source.tenant_id}:"
                    f"{source.updated_at.isoformat() if source.updated_at else ''}"
                ).encode()
            ).hexdigest()
        user_info = await self._json_request(
            "GET",
            endpoint,
            headers={"Authorization": f"Bearer {subject_token}"},
        )
        introspection_subject = _normalized_subject(
            introspection_claims.get("sub"),
            "sub",
        )
        userinfo_subject = _normalized_subject(user_info.get("sub"), "sub")
        if not secrets.compare_digest(
            introspection_subject,
            userinfo_subject,
        ):
            raise ExternalTokenInvalid(
                "External UserInfo subject does not match introspection subject"
            )
        returned_source_id = user_info.get("pas_source_id")
        if (
            not isinstance(returned_source_id, str)
            or not secrets.compare_digest(returned_source_id, source_id)
        ):
            raise ExternalTokenInvalid(
                "External UserInfo identity source is invalid"
            )
        external_user_id = _normalized_subject(
            user_info.get("user_id"),
            "user_id",
        )
        _optional_string(
            user_info.get("union_id"),
            "union_id",
        )
        return ExternalTokenIdentity(
            provider_type="oauth2_introspection_userinfo",
            provider_key=provider_key,
            provider_fingerprint=hashlib.sha256(
                f"{self._base_fingerprint()}:{source_fingerprint}".encode()
            ).hexdigest(),
            subject=external_user_id,
            display_name=None,
            email=None,
            scopes=_scopes(introspection_claims.get("scope")),
            expires_at=_expires_at(introspection_claims.get("exp")),
            scope_authoritative=False,
            requires_existing_mapping=True,
        )

    async def _validate_userinfo(
        self,
        subject_token: str,
        *,
        buc: bool,
    ) -> ExternalTokenIdentity:
        oidc = self._config.auth.oidc
        trust = self._config.auth.external_token_trust
        userinfo_endpoint = trust.userinfo_endpoint
        if not userinfo_endpoint:
            federation = IdentityFederation(oidc, oidc.provider_name)
            try:
                endpoints = await federation.discover_endpoints()
            except (httpx.HTTPError, ValueError) as exc:
                raise ExternalTokenUnavailable(
                    "OAuth provider discovery is temporarily unavailable"
                ) from exc
            userinfo_endpoint = endpoints.userinfo_endpoint
        if not userinfo_endpoint:
            raise ExternalTokenInvalid(
                "OAuth UserInfo endpoint is not configured"
            )
        if buc or oidc.userinfo_token_method == "form_post":
            claims = await self._json_request(
                "POST",
                userinfo_endpoint,
                data={"access_token": subject_token},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded"
                },
            )
        elif oidc.userinfo_token_method == "query":
            claims = await self._json_request(
                "GET",
                userinfo_endpoint,
                params={"access_token": subject_token},
            )
        else:
            claims = await self._json_request(
                "GET",
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {subject_token}"},
            )
        if buc:
            if claims.get("error") or claims.get("error_code"):
                raise ExternalTokenInvalid("BUC access token was rejected")
            returned_client_id = claims.get("client_id")
            if (
                not isinstance(returned_client_id, str)
                or not secrets.compare_digest(
                    returned_client_id,
                    oidc.client_id,
                )
            ):
                raise ExternalTokenInvalid(
                    "BUC access token client ID is invalid"
                )
            if "account_id" not in claims and "accountId" in claims:
                claims = {**claims, "account_id": claims["accountId"]}
        return self._identity_from_claims(
            claims,
            provider_type="buc" if buc else "oauth2_userinfo",
            provider_key=oidc.provider_name,
            subject_claim="account_id" if buc else oidc.user_id_claim,
        )

    async def _validate_feishu(
        self,
        subject_token: str,
        *,
        identity_source_id: str | None,
        feishu_user_id: str | None,
        feishu_union_id: str | None,
    ) -> ExternalTokenIdentity:
        trust = self._config.auth.external_token_trust
        context_values = (
            identity_source_id,
            feishu_user_id,
            feishu_union_id,
        )
        if any(context_values) and not all(context_values):
            raise ExternalTokenInvalid(
                "Feishu exchange identity context is incomplete"
            )
        source_id = identity_source_id or trust.identity_source_id
        async with self._session_factory() as session:
            source = await self._load_feishu_source(
                session,
                source_id,
            )
            tenant_id = cast(str, source.tenant_id)
            provider_key = identity_provider_key(source)
            source_fingerprint = hashlib.sha256(
                (
                    f"{source.id}:{tenant_id}:"
                    f"{source.updated_at.isoformat() if source.updated_at else ''}"
                ).encode()
            ).hexdigest()
        payload = await self._json_request(
            "GET",
            FEISHU_USER_INFO_URL,
            headers={"Authorization": f"Bearer {subject_token}"},
        )
        if payload.get("code") not in {None, 0}:
            raise ExternalTokenInvalid("Feishu access token was rejected")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise ExternalTokenInvalid("Feishu user info response is invalid")
        tenant_key = data.get("tenant_key")
        if (
            not isinstance(tenant_key, str)
            or not secrets.compare_digest(tenant_key, tenant_id)
        ):
            raise ExternalTokenInvalid(
                "Feishu access token tenant is not trusted"
            )
        subject = _normalized_subject(data.get("user_id"), "user_id")
        if feishu_user_id and not secrets.compare_digest(
            subject,
            feishu_user_id,
        ):
            raise ExternalTokenInvalid(
                "Feishu access token user ID does not match exchange request"
            )
        if feishu_union_id:
            union_id = _normalized_subject(data.get("union_id"), "union_id")
            if not secrets.compare_digest(union_id, feishu_union_id):
                raise ExternalTokenInvalid(
                    "Feishu access token union ID does not match exchange request"
                )
        return ExternalTokenIdentity(
            provider_type="feishu",
            provider_key=provider_key,
            provider_fingerprint=hashlib.sha256(
                f"{self._base_fingerprint()}:{source_fingerprint}".encode()
            ).hexdigest(),
            subject=subject,
            display_name=_optional_string(data.get("name"), "name"),
            email=_optional_string(data.get("email"), "email"),
            scopes=(),
            expires_at=None,
            identity_source_id=source.id,
        )

    async def _load_feishu_source(
        self,
        session: AsyncSession,
        identity_source_id: str | None,
    ) -> EnterpriseIdentitySource:
        source = await self._load_active_identity_source(
            session,
            identity_source_id,
        )
        if (
            source.provider != IdentitySourceProvider.FEISHU
            or not source.config_ciphertext
        ):
            raise ExternalTokenInvalid(
                "Feishu identity source is unavailable"
            )
        try:
            source_config = json.loads(decrypt(source.config_ciphertext))
        except (TypeError, ValueError) as exc:
            raise ExternalTokenInvalid(
                "Feishu identity source configuration is invalid"
            ) from exc
        if not isinstance(source_config, dict) or not source_config.get(
            "app_id"
        ):
            raise ExternalTokenInvalid(
                "Feishu identity source configuration is invalid"
            )
        return source

    async def _load_active_identity_source(
        self,
        session: AsyncSession,
        identity_source_id: str | None,
    ) -> EnterpriseIdentitySource:
        if not identity_source_id:
            raise ExternalTokenInvalid(
                "Identity source is not configured"
            )
        source = await session.get(
            EnterpriseIdentitySource,
            identity_source_id,
        )
        if (
            source is None
            or source.status != EnterpriseIdentitySourceStatus.ACTIVE
            or not source.tenant_id
        ):
            raise ExternalTokenInvalid(
                "Identity source is unavailable"
            )
        return source

    def _identity_from_claims(
        self,
        claims: dict[str, Any],
        *,
        provider_type: str,
        provider_key: str,
        subject_claim: str,
    ) -> ExternalTokenIdentity:
        oidc = self._config.auth.oidc
        return ExternalTokenIdentity(
            provider_type=provider_type,
            provider_key=provider_key,
            provider_fingerprint=self._base_fingerprint(),
            subject=_normalized_subject(
                claims.get(subject_claim),
                subject_claim,
            ),
            display_name=_optional_string(
                claims.get(oidc.display_name_claim),
                oidc.display_name_claim,
            ),
            email=_optional_string(
                claims.get(oidc.email_claim),
                oidc.email_claim,
            ),
            scopes=_scopes(claims.get("scope")),
            expires_at=_expires_at(claims.get("exp")),
        )
