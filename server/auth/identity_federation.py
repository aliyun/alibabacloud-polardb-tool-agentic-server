from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import OIDCConfig
from server.models import User, AuthProvider, UserRole, UserStatus
from server.models.oauth import UserExternalIdentity

logger = logging.getLogger(__name__)


class OIDCAuthenticationError(ValueError):
    pass


def _oidc_json_object(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise OIDCAuthenticationError(
            f"OIDC {label} must be valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise OIDCAuthenticationError(
            f"OIDC {label} must be a JSON object"
        )
    return payload


@dataclass
class IdPEndpoints:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: str | None = None
    jwks_uri: str | None = None


@dataclass
class UserIdentity:
    subject: str
    display_name: str | None = None
    email: str | None = None


class IdentityFederation:
    def __init__(self, config: OIDCConfig, provider_name: str = "oidc"):
        self._config = config
        self._provider_name = provider_name
        self._endpoints: IdPEndpoints | None = None

    async def discover_endpoints(self) -> IdPEndpoints:
        """Discover IdP endpoints from OIDC discovery URL or manual config."""
        if self._endpoints is not None:
            return self._endpoints

        if self._config.discovery_url:
            async with httpx.AsyncClient() as client:
                resp = await client.get(self._config.discovery_url)
                resp.raise_for_status()
                meta = _oidc_json_object(resp, "discovery metadata")
            required_endpoints: dict[str, str] = {}
            for field in (
                "issuer",
                "authorization_endpoint",
                "token_endpoint",
            ):
                value = meta.get(field)
                if not isinstance(value, str) or not value:
                    raise OIDCAuthenticationError(
                        f"OIDC discovery metadata requires {field}"
                    )
                required_endpoints[field] = value
            optional_endpoints: dict[str, str | None] = {}
            for field in ("userinfo_endpoint", "jwks_uri"):
                value = meta.get(field)
                if value is not None and (
                    not isinstance(value, str) or not value
                ):
                    raise OIDCAuthenticationError(
                        f"OIDC discovery metadata has invalid {field}"
                    )
                optional_endpoints[field] = value
            self._endpoints = IdPEndpoints(
                issuer=required_endpoints["issuer"],
                authorization_endpoint=required_endpoints[
                    "authorization_endpoint"
                ],
                token_endpoint=required_endpoints["token_endpoint"],
                userinfo_endpoint=optional_endpoints[
                    "userinfo_endpoint"
                ],
                jwks_uri=optional_endpoints["jwks_uri"],
            )
        else:
            if (
                not self._config.issuer
                or not self._config.authorization_endpoint
                or not self._config.token_endpoint
            ):
                raise ValueError(
                    "OIDC config requires either discovery_url or "
                    "manual issuer + authorization_endpoint + token_endpoint"
                )
            self._endpoints = IdPEndpoints(
                issuer=self._config.issuer,
                authorization_endpoint=self._config.authorization_endpoint,
                token_endpoint=self._config.token_endpoint,
                userinfo_endpoint=self._config.userinfo_endpoint,
                jwks_uri=self._config.jwks_uri,
            )
        return self._endpoints

    def build_authorize_url(
        self,
        redirect_uri: str,
        state: str,
        nonce: str | None = None,
        code_verifier: str | None = None,
    ) -> tuple[str, str | None]:
        """Build the IdP authorization URL for user redirect.

        Returns (authorize_url, code_verifier) — code_verifier is non-None only
        when IdP-side PKCE is enabled.
        """
        endpoints = self._endpoints
        if endpoints is None:
            raise RuntimeError("Call discover_endpoints() first")

        params = {
            "response_type": "code",
            "client_id": self._config.client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self._config.scopes),
            "state": state,
        }
        if nonce:
            params["nonce"] = nonce

        # IdP-side PKCE: generate code_verifier/code_challenge if enabled
        effective_verifier: str | None = None
        if self._config.idp_pkce:
            effective_verifier = code_verifier or secrets.token_urlsafe(43)
            challenge_bytes = hashlib.sha256(
                effective_verifier.encode("ascii")
            ).digest()
            import base64
            code_challenge = (
                base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")
            )
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"

        url = f"{endpoints.authorization_endpoint}?{urlencode(params)}"
        return url, effective_verifier

    async def exchange_code(
        self, code: str, redirect_uri: str, code_verifier: str | None = None
    ) -> dict[str, Any]:
        """Exchange IdP authorization code for tokens."""
        endpoints = await self.discover_endpoints()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                endpoints.token_endpoint,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            return _oidc_json_object(resp, "token response")

    async def _verified_id_token_claims(
        self, token_response: dict[str, Any]
    ) -> dict[str, Any]:
        endpoints = await self.discover_endpoints()
        id_token = token_response.get("id_token")
        if not id_token or not endpoints.jwks_uri:
            raise ValueError(
                "Cannot verify id_token: no id_token or jwks_uri available"
            )
        async with httpx.AsyncClient() as client:
            jwks_resp = await client.get(endpoints.jwks_uri)
            jwks_resp.raise_for_status()
            jwks = _oidc_json_object(jwks_resp, "JWKS response")
        keys = jwks.get("keys")
        if not isinstance(keys, list) or any(
            not isinstance(key, dict) for key in keys
        ):
            raise OIDCAuthenticationError(
                "OIDC JWKS keys must be an array of objects"
            )
        try:
            header = jwt.get_unverified_header(id_token)
            matching_keys = [
                key
                for key in keys
                if not header.get("kid") or key.get("kid") == header["kid"]
            ]
            if len(matching_keys) != 1:
                raise ValueError("Cannot select a unique OIDC signing key")
            signing_key = jwt.PyJWK.from_dict(matching_keys[0]).key
            claims = jwt.decode(
                id_token,
                signing_key,
                algorithms=self._config.id_token_algorithms,
                audience=self._config.client_id,
                issuer=endpoints.issuer,
            )
        except (jwt.PyJWTError, TypeError, ValueError) as exc:
            raise OIDCAuthenticationError(
                "OIDC id_token signature, issuer, or audience validation failed"
            ) from exc
        return dict(claims)

    async def extract_user_identity(
        self,
        token_response: dict[str, Any],
        *,
        expected_nonce: str | None = None,
    ) -> UserIdentity:
        """Extract user identity from IdP token response.

        Validates the ID token before using either its claims or UserInfo
        claims associated with its subject.
        """
        config = self._config
        endpoints = await self.discover_endpoints()

        id_token_claims: dict[str, Any] | None = None
        if expected_nonce is not None:
            id_token_claims = await self._verified_id_token_claims(
                token_response
            )
            nonce = id_token_claims.get("nonce")
            if (
                not isinstance(nonce, str)
                or not secrets.compare_digest(nonce, expected_nonce)
            ):
                raise ValueError("OIDC nonce validation failed")

        if endpoints.userinfo_endpoint:
            access_token = token_response.get("access_token", "")
            async with httpx.AsyncClient() as client:
                if config.userinfo_token_method == "form_post":
                    resp = await client.post(
                        endpoints.userinfo_endpoint,
                        data={"access_token": access_token},
                    )
                else:
                    resp = await client.get(
                        endpoints.userinfo_endpoint,
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
            resp.raise_for_status()
            claims = _oidc_json_object(resp, "UserInfo response")
            if id_token_claims is not None:
                id_token_subject = id_token_claims.get("sub")
                userinfo_subject = claims.get("sub")
                if (
                    not isinstance(id_token_subject, str)
                    or not isinstance(userinfo_subject, str)
                    or not secrets.compare_digest(
                        id_token_subject, userinfo_subject
                    )
                ):
                    raise OIDCAuthenticationError(
                        "OIDC UserInfo subject does not match id_token subject"
                    )
        elif "id_token" in token_response:
            claims = id_token_claims or await self._verified_id_token_claims(
                token_response
            )
        else:
            raise ValueError(
                "Cannot extract user identity: no userinfo endpoint and no id_token"
            )

        subject = claims.get(config.user_id_claim)
        if not isinstance(subject, str) or not subject:
            raise OIDCAuthenticationError(
                f"OIDC user identity claim '{config.user_id_claim}' must be a non-empty string"
            )
        display_name = claims.get(config.display_name_claim)
        if display_name is not None and not isinstance(display_name, str):
            raise OIDCAuthenticationError(
                f"OIDC display name claim '{config.display_name_claim}' must be a string or null"
            )
        email = claims.get(config.email_claim)
        if email is not None and not isinstance(email, str):
            raise OIDCAuthenticationError(
                f"OIDC email claim '{config.email_claim}' must be a string or null"
            )

        return UserIdentity(
            subject=subject,
            display_name=display_name,
            email=email,
        )

    async def find_or_create_user(
        self, session: AsyncSession, identity: UserIdentity
    ) -> User:
        """Match or create a local User via UserExternalIdentity."""
        # Look for existing mapping
        result = await session.execute(
            select(UserExternalIdentity).where(
                UserExternalIdentity.identity_provider == self._provider_name,
                UserExternalIdentity.external_subject == identity.subject,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            user_result = await session.execute(
                select(User).where(User.id == existing.user_id)
            )
            user = user_result.scalar_one_or_none()
            if user:
                return user

        # Create new user
        user = User(
            external_id=f"{self._provider_name}:{identity.subject}",
            display_name=identity.display_name or identity.subject,
            auth_provider=AuthProvider.OIDC,
            role=UserRole.MEMBER,
            status=UserStatus.ACTIVE,
        )
        session.add(user)
        await session.flush()

        # Create identity mapping
        mapping = UserExternalIdentity(
            user_id=user.id,
            identity_provider=self._provider_name,
            external_subject=identity.subject,
        )
        session.add(mapping)

        # Auto-assign to default department if configured
        from server.config import get_config
        default_dept_name = get_config().auth.default_department
        if default_dept_name:
            from server.models.department import Department
            from server.models.binding import UserDepartment
            dept = (await session.execute(
                select(Department).where(Department.name == default_dept_name)
            )).scalar_one_or_none()
            if dept:
                session.add(UserDepartment(
                    user_id=user.id, department_id=dept.id, is_primary=True,
                ))

        await session.commit()
        return user
