from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import OIDCConfig
from server.models import User, AuthProvider, UserRole, UserStatus
from server.models.oauth import UserExternalIdentity

logger = logging.getLogger(__name__)


@dataclass
class IdPEndpoints:
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
                meta = resp.json()
            self._endpoints = IdPEndpoints(
                authorization_endpoint=meta["authorization_endpoint"],
                token_endpoint=meta["token_endpoint"],
                userinfo_endpoint=meta.get("userinfo_endpoint"),
                jwks_uri=meta.get("jwks_uri"),
            )
        else:
            if not self._config.authorization_endpoint or not self._config.token_endpoint:
                raise ValueError(
                    "OIDC config requires either discovery_url or "
                    "manual authorization_endpoint + token_endpoint"
                )
            self._endpoints = IdPEndpoints(
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
            result: dict[str, Any] = resp.json()
            return result

    async def extract_user_identity(self, token_response: dict) -> UserIdentity:
        """Extract user identity from IdP token response.

        Prefers the userinfo endpoint when available; falls back to decoding
        the id_token without verification (covered by TLS to the IdP).
        """
        config = self._config
        endpoints = await self.discover_endpoints()

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
            claims = resp.json()
        elif "id_token" in token_response:
            from jose import jwt as jose_jwt

            if endpoints.jwks_uri:
                async with httpx.AsyncClient() as client:
                    jwks_resp = await client.get(endpoints.jwks_uri)
                    jwks_resp.raise_for_status()
                    jwks = jwks_resp.json()
                claims = jose_jwt.decode(
                    token_response["id_token"],
                    jwks,
                    algorithms=config.id_token_algorithms,
                    audience=config.client_id,
                    options={"verify_at_hash": False},
                )
            else:
                raise ValueError(
                    "Cannot verify id_token: no jwks_uri discovered and no "
                    "userinfo endpoint configured. Configure a userinfo "
                    "endpoint or ensure the IdP exposes jwks_uri in discovery."
                )
        else:
            raise ValueError(
                "Cannot extract user identity: no userinfo endpoint and no id_token"
            )

        subject = str(claims.get(config.user_id_claim, ""))
        if not subject:
            raise ValueError(
                f"User identity claim '{config.user_id_claim}' not found in response"
            )

        return UserIdentity(
            subject=subject,
            display_name=claims.get(config.display_name_claim),
            email=claims.get(config.email_claim),
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
