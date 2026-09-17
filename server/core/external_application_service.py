from __future__ import annotations

import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import AppConfig
from server.core.crypto import encrypt
from server.models import (
    Agent,
    AgentStatus,
    OAuthExternalApplication,
    OAuthExternalApplicationAgentPolicy,
    OAuthExternalApplicationStatus,
    OAuthRegisteredClient,
)

TOKEN_EXCHANGE_GRANT_TYPE = (
    "urn:ietf:params:oauth:grant-type:token-exchange"
)
ExternalApplicationTarget = Literal["mcp", "api"]
ALLOWED_TARGETS = frozenset({"mcp", "api"})


class ExternalApplicationUnauthorized(ValueError):
    pass


@dataclass(frozen=True)
class ResourceProfile:
    target: ExternalApplicationTarget
    resource: str
    scope: str


@dataclass(frozen=True)
class ManagedExchangePolicy:
    resource: str
    scopes: tuple[str, ...]
    agent_id: str | None


def resource_profiles(config: AppConfig) -> tuple[ResourceProfile, ...]:
    base_url = config.server.public_base_url.rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "Configure runtime_policy.external_base_url before creating "
            "an external application"
        )
    return (
        ResourceProfile(
            target="mcp",
            resource=f"{base_url}/mcp",
            scope="mcp",
        ),
        ResourceProfile(
            target="api",
            resource=f"{base_url}/api/v1",
            scope="polarrag",
        ),
    )


def parse_targets(value: str) -> tuple[ExternalApplicationTarget, ...]:
    try:
        values = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("External application targets are invalid") from exc
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(item, str) for item in values)
        or not set(values).issubset(ALLOWED_TARGETS)
    ):
        raise ValueError("External application targets are invalid")
    return tuple(dict.fromkeys(values))  # type: ignore[return-value]


async def _validate_fixed_agent(
    session: AsyncSession,
    *,
    agent_policy: OAuthExternalApplicationAgentPolicy,
    fixed_agent_id: str | None,
) -> None:
    if agent_policy == OAuthExternalApplicationAgentPolicy.FIXED:
        if not fixed_agent_id:
            raise ValueError("fixed_agent_id is required for fixed policy")
        agent = await session.scalar(
            select(Agent).where(
                Agent.id == fixed_agent_id,
                Agent.status == AgentStatus.ACTIVE,
            )
        )
        if agent is None:
            raise ValueError("Fixed Agent is unavailable")
    elif fixed_agent_id is not None:
        raise ValueError("fixed_agent_id is only valid for fixed policy")


async def create_external_application(
    session: AsyncSession,
    config: AppConfig,
    *,
    name: str,
    created_by: str,
    targets: list[ExternalApplicationTarget],
    agent_policy: OAuthExternalApplicationAgentPolicy,
    fixed_agent_id: str | None,
    secret_expires_at: datetime | None,
) -> tuple[OAuthExternalApplication, str]:
    trust = config.auth.external_token_trust
    if not trust.enabled:
        raise ValueError(
            "Configure and activate external token trust before creating "
            "an external application"
        )
    profiles = resource_profiles(config)
    normalized_targets = tuple(dict.fromkeys(targets))
    if not normalized_targets or not set(normalized_targets).issubset(
        ALLOWED_TARGETS
    ):
        raise ValueError("Select at least one valid target")
    await _validate_fixed_agent(
        session,
        agent_policy=agent_policy,
        fixed_agent_id=fixed_agent_id,
    )

    now = datetime.now(timezone.utc)
    if secret_expires_at is not None:
        if secret_expires_at.tzinfo is None:
            secret_expires_at = secret_expires_at.replace(
                tzinfo=timezone.utc
            )
        if secret_expires_at <= now:
            raise ValueError("Secret expiration must be in the future")

    selected_profiles = [
        profile for profile in profiles if profile.target in normalized_targets
    ]
    client_id = f"pas_ext_{uuid.uuid4().hex}"
    client_secret = f"pas_secret_{secrets.token_urlsafe(32)}"
    session.add(
        OAuthRegisteredClient(
            client_id=client_id,
            client_secret_enc=encrypt(client_secret),
            client_id_issued_at=int(time.time()),
            client_secret_expires_at=(
                int(secret_expires_at.timestamp())
                if secret_expires_at is not None
                else None
            ),
            redirect_uris="[]",
            grant_types=json.dumps([TOKEN_EXCHANGE_GRANT_TYPE]),
            response_types="[]",
            token_endpoint_auth_method="client_secret_basic",
            scope=" ".join(profile.scope for profile in selected_profiles),
            client_name=name,
        )
    )
    application = OAuthExternalApplication(
        client_id=client_id,
        created_by=created_by,
        provider_type=trust.provider,
        targets=json.dumps(normalized_targets),
        agent_policy=agent_policy.value,
        fixed_agent_id=fixed_agent_id,
        status=OAuthExternalApplicationStatus.ACTIVE.value,
        secret_created_at=now,
    )
    session.add(application)
    await session.flush()
    return application, client_secret


async def rotate_external_application_secret(
    session: AsyncSession,
    application: OAuthExternalApplication,
    *,
    secret_expires_at: datetime | None,
) -> str:
    client = await session.get(OAuthRegisteredClient, application.client_id)
    if client is None:
        raise ValueError("OAuth client is unavailable")
    now = datetime.now(timezone.utc)
    if secret_expires_at is not None:
        if secret_expires_at.tzinfo is None:
            secret_expires_at = secret_expires_at.replace(
                tzinfo=timezone.utc
            )
        if secret_expires_at <= now:
            raise ValueError("Secret expiration must be in the future")
    plaintext = f"pas_secret_{secrets.token_urlsafe(32)}"
    client.client_secret_enc = encrypt(plaintext)
    client.client_secret_expires_at = (
        int(secret_expires_at.timestamp())
        if secret_expires_at is not None
        else None
    )
    application.secret_created_at = now
    await session.flush()
    return plaintext


def set_external_application_status(
    application: OAuthExternalApplication,
    status: OAuthExternalApplicationStatus,
) -> None:
    application.status = status.value
    application.disabled_at = (
        datetime.now(timezone.utc)
        if status == OAuthExternalApplicationStatus.DISABLED
        else None
    )


async def resolve_managed_exchange_policy(
    session: AsyncSession,
    config: AppConfig,
    *,
    client_id: str,
    resource: str | None,
    scopes: list[str],
    agent_id: str | None,
) -> ManagedExchangePolicy | None:
    application = await session.get(OAuthExternalApplication, client_id)
    if application is None:
        return None
    if application.status != OAuthExternalApplicationStatus.ACTIVE.value:
        raise ExternalApplicationUnauthorized(
            "External application is disabled"
        )
    if application.provider_type != config.auth.external_token_trust.provider:
        raise ExternalApplicationUnauthorized(
            "External application must be revalidated after Provider changes"
        )

    profiles = {
        profile.target: profile for profile in resource_profiles(config)
    }
    targets = parse_targets(application.targets)
    selected: ResourceProfile | None = None
    if resource:
        normalized = resource.rstrip("/")
        selected = next(
            (
                profiles[target]
                for target in targets
                if profiles[target].resource.rstrip("/") == normalized
            ),
            None,
        )
        if selected is None:
            raise ValueError(
                "Requested resource is not enabled for this application"
            )
    elif len(targets) == 1:
        selected = profiles[targets[0]]
    else:
        raise ValueError(
            "resource is required when an application enables multiple targets"
        )

    requested_scopes = tuple(dict.fromkeys(scopes))
    if requested_scopes and requested_scopes != (selected.scope,):
        raise ValueError(
            f"Requested scope must be exactly '{selected.scope}'"
        )

    policy = OAuthExternalApplicationAgentPolicy(
        application.agent_policy
    )
    if policy == OAuthExternalApplicationAgentPolicy.WORKSPACE_DEFAULT:
        if agent_id is not None:
            raise ValueError(
                "agent_id is not accepted by workspace_default policy"
            )
        effective_agent_id = None
    elif policy == OAuthExternalApplicationAgentPolicy.FIXED:
        if agent_id is not None and agent_id != application.fixed_agent_id:
            raise ValueError(
                "agent_id does not match the application's fixed Agent"
            )
        effective_agent_id = application.fixed_agent_id
    else:
        effective_agent_id = agent_id

    return ManagedExchangePolicy(
        resource=selected.resource,
        scopes=(selected.scope,),
        agent_id=effective_agent_id,
    )


def mark_external_application_used(
    application: OAuthExternalApplication,
) -> None:
    application.last_used_at = datetime.now(timezone.utc)
