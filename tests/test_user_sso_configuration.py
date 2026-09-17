from __future__ import annotations

import base64
import os
import socket
from unittest.mock import AsyncMock, MagicMock, call, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from server.auth.identity_federation import IdentityFederation, UserIdentity
from server.auth.router import router as auth_router
from server.configuration.external_validation import (
    AlibabaCloudExternalValidator,
    ExternalValidationError,
    ExternalValidationResult,
)
from server.configuration.types import (
    ConfigAction,
    ConfigActor,
    ConfigCommand,
)
from server.db import engine as engine_mod
from server.models import (
    OIDCLoginState,
    User,
    UserExternalIdentity,
    UserRole,
)
from tests._configuration_helpers import create_config_context


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode(),
    )
    yield
    engine_mod.reset_engine()


def _response(url: str, payload: dict[str, object]) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("GET", url),
    )


async def test_user_sso_external_validation_checks_discovery_and_jwks() -> None:
    discovery_url = "https://idp.example.com/.well-known/openid-configuration"
    jwks_url = "https://idp.example.com/jwks"
    client = AsyncMock()
    client.get.side_effect = [
        _response(
            discovery_url,
            {
                "issuer": "https://idp.example.com",
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token",
                "userinfo_endpoint": "https://idp.example.com/userinfo",
                "jwks_uri": jwks_url,
            },
        ),
        _response(jwks_url, {"keys": [{"kid": "key-1", "kty": "RSA"}]}),
    ]
    client_context = MagicMock()
    client_context.__aenter__ = AsyncMock(return_value=client)
    client_context.__aexit__ = AsyncMock(return_value=None)
    validator = AlibabaCloudExternalValidator()

    with (
        patch(
            "server.configuration.external_validation.httpx.AsyncClient",
            return_value=client_context,
        ),
        patch.object(
            validator,
            "_require_safe_oidc_url",
            new=AsyncMock(side_effect=lambda value: value),
        ) as require_safe,
    ):
        result = await validator.validate(
            "user_sso",
            {
                "discovery_url": discovery_url,
                "client_id": "pas",
                "client_secret": "secret",
            },
        )

    assert result.status == "PASSED"
    assert [check.service for check in result.checks] == [
        "oidc_discovery",
        "oidc_jwks",
    ]
    assert require_safe.await_count == 6


async def test_user_sso_external_validation_uses_private_endpoint_policy() -> None:
    validator = AlibabaCloudExternalValidator()

    with (
        patch.object(
            validator,
            "_require_safe_oidc_url",
            new=AsyncMock(side_effect=lambda value: value),
        ),
        patch.object(
            validator,
            "_require_safe_external_token_url",
            new=AsyncMock(side_effect=lambda value: value),
        ) as require_external_safe,
        patch.object(
            validator,
            "_get_oidc_json",
            new=AsyncMock(return_value={"keys": [{"kid": "key-1"}]}),
        ),
    ):
        result = await validator.validate(
            "user_sso",
            {
                "issuer": "https://idp.example.com",
                "authorization_endpoint": "https://idp.example.com/authorize",
                "token_endpoint": "https://idp.example.com/token",
                "jwks_uri": "https://idp.example.com/jwks",
                "client_id": "pas",
                "client_secret": "secret",
                "external_token_trust": {
                    "enabled": True,
                    "provider": "oauth2_introspection",
                    "introspection_endpoint": (
                        "http://winner.internal/oauth2/introspect"
                    ),
                    "userinfo_endpoint": (
                        "http://winner.internal/oauth2/user_info"
                    ),
                },
            },
        )

    assert result.status == "PASSED"
    require_external_safe.assert_has_awaits(
        [
            call("http://winner.internal/oauth2/introspect"),
            call("http://winner.internal/oauth2/user_info"),
        ]
    )


async def test_token_exchange_only_validation_skips_browser_oidc_endpoints() -> None:
    validator = AlibabaCloudExternalValidator()

    with (
        patch.object(
            validator,
            "_require_safe_oidc_url",
            new=AsyncMock(),
        ) as require_browser_safe,
        patch.object(
            validator,
            "_require_safe_external_token_url",
            new=AsyncMock(side_effect=lambda value: value),
        ) as require_external_safe,
        patch.object(
            validator,
            "_get_oidc_json",
            new=AsyncMock(return_value={"keys": [{"kid": "legacy"}]}),
        ) as get_oidc_json,
    ):
        result = await validator.validate(
            "user_sso",
            {
                "browser_login_enabled": False,
                "jwks_uri": "https://idp.example/legacy-jwks",
                "external_token_trust": {
                    "enabled": True,
                    "provider": "oauth2_introspection",
                    "introspection_endpoint": "https://idp.example/introspect",
                    "client_id": "pas-token-validator",
                    "client_secret": "secret",
                },
            },
        )

    assert result.status == "PASSED"
    require_browser_safe.assert_not_awaited()
    require_external_safe.assert_awaited_once_with(
        "https://idp.example/introspect"
    )
    get_oidc_json.assert_not_awaited()


async def test_token_exchange_only_activation_skips_browser_login_test() -> None:
    context = await create_config_context()
    try:
        runtime = await context.repository.get_module("runtime_policy")
        assert runtime is not None and runtime.effective is not None
        await context.repository.compare_and_set_module(
            "runtime_policy",
            expected_revision=runtime.revision,
            document=runtime.model_copy(
                update={
                    "effective": runtime.effective.model_copy(
                        update={
                            "config": {
                                **runtime.effective.config,
                                "external_base_url": "https://pas.example.com",
                            }
                        }
                    )
                }
            ),
        )
        context.service.external_validator.validate = AsyncMock(
            return_value=ExternalValidationResult(status="PASSED")
        )
        async with context.repository.session_factory() as session:
            admin = User(
                external_id="token-exchange-admin",
                display_name="Token Exchange Admin",
                role=UserRole.ADMIN,
            )
            session.add(admin)
            await session.commit()
        actor = ConfigActor(
            scope=f"admin:{admin.id}",
            actor_type="admin",
        )
        initial = await context.service.describe_internal("user_sso")
        saved = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="user_sso",
                expected_revision=initial.revision,
                config={
                    "browser_login_enabled": False,
                    "external_token_trust": {
                        "enabled": True,
                        "provider": "oauth2_introspection",
                        "introspection_endpoint": (
                            "https://idp.example/introspect"
                        ),
                        "client_id": "pas-token-validator",
                        "client_secret": "secret",
                    },
                },
            ),
            actor,
        )
        validated = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.VALIDATE,
                module="user_sso",
                expected_revision=saved.module["revision"],
            ),
            actor,
        )
        assert validated.validation is not None

        activated = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.ACTIVATE,
                module="user_sso",
                expected_revision=validated.module["revision"],
                validation_id=validated.validation["validation_id"],
                idempotency_key="activate-token-exchange-only",
            ),
            actor,
        )

        assert activated.module is not None
        assert activated.module["workflow_state"] == "ACTIVE"
    finally:
        await context.close()


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("http://localhost:19090/oauth2/introspect", "127.0.0.1"),
        ("http://127.0.0.1:19090/oauth2/user_info", "127.0.0.1"),
        ("http://[::1]:19090/oauth2/token", "::1"),
    ],
)
async def test_local_sso_dev_validator_accepts_exact_loopback_http_urls(
    monkeypatch,
    url: str,
    address: str,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 19090),
            )
        ],
    )
    validator = AlibabaCloudExternalValidator(
        allow_insecure_loopback_urls=True,
    )

    assert await validator._require_safe_oidc_url(url) == url
    assert validator._oidc_network(url) == "loopback"


async def test_validator_rejects_loopback_http_without_local_sso_dev_mode() -> None:
    validator = AlibabaCloudExternalValidator()

    with pytest.raises(
        ExternalValidationError,
        match="public HTTPS",
    ):
        await validator._require_safe_oidc_url(
            "http://127.0.0.1:19090/oauth2/introspect"
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:19090/oauth2/introspect",
        "http://127.0.0.2:19090/oauth2/introspect",
        "http://192.168.1.20:19090/oauth2/introspect",
        "http://localhost.example.com:19090/oauth2/introspect",
        "http://user@localhost:19090/oauth2/introspect",
        "http://localhost:19090/oauth2/introspect#fragment",
    ],
)
async def test_local_sso_dev_validator_rejects_unsafe_http_urls(
    url: str,
) -> None:
    validator = AlibabaCloudExternalValidator(
        allow_insecure_loopback_urls=True,
    )

    with pytest.raises(ExternalValidationError, match="public HTTPS"):
        await validator._require_safe_oidc_url(url)


async def test_local_sso_dev_validator_rejects_spoofed_localhost_resolution(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                ("192.0.2.10", 19090),
            )
        ],
    )
    validator = AlibabaCloudExternalValidator(
        allow_insecure_loopback_urls=True,
    )

    with pytest.raises(
        ExternalValidationError,
        match="loopback addresses",
    ):
        await validator._require_safe_oidc_url(
            "http://localhost:19090/oauth2/introspect"
        )


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("http://winner.internal/oauth2/introspect", "10.20.30.40"),
        ("http://winner.internal/oauth2/user_info", "172.20.30.40"),
        ("http://winner.internal/oauth2/introspect", "100.64.10.20"),
        ("http://winner.internal/oauth2/user_info", "fd00::20"),
        ("https://winner.internal/oauth2/introspect", "192.168.10.20"),
    ],
)
async def test_external_token_validator_accepts_private_provider_urls(
    monkeypatch,
    url: str,
    address: str,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 80),
            )
        ],
    )
    validator = AlibabaCloudExternalValidator()

    assert await validator._require_safe_external_token_url(url) == url


@pytest.mark.parametrize(
    ("url", "address", "message"),
    [
        (
            "http://provider.example.com/introspect",
            "8.8.8.8",
            "trusted private network",
        ),
        (
            "http://winner.internal/introspect",
            "169.254.169.254",
            "link-local",
        ),
        (
            "https://winner.internal/introspect",
            "127.0.0.1",
            "localhost development mode",
        ),
    ],
)
async def test_external_token_validator_rejects_unsafe_provider_urls(
    monkeypatch,
    url: str,
    address: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, 80),
            )
        ],
    )
    validator = AlibabaCloudExternalValidator()

    with pytest.raises(ExternalValidationError, match=message):
        await validator._require_safe_external_token_url(url)


async def test_user_sso_browser_test_is_consumed_by_atomic_activation() -> None:
    context = await create_config_context()
    try:
        runtime = await context.repository.get_module("runtime_policy")
        assert runtime is not None and runtime.effective is not None
        await context.repository.compare_and_set_module(
            "runtime_policy",
            expected_revision=runtime.revision,
            document=runtime.model_copy(
                update={
                    "effective": runtime.effective.model_copy(
                        update={
                            "config": {
                                **runtime.effective.config,
                                "external_base_url": "https://pas.example.com",
                            }
                        }
                    )
                }
            ),
        )
        async with context.repository.session_factory() as session:
            admin = User(
                external_id="admin",
                display_name="Administrator",
                role=UserRole.ADMIN,
            )
            session.add(admin)
            await session.commit()

        actor = ConfigActor(
            scope=f"admin:{admin.id}",
            actor_type="admin",
        )
        initial = await context.service.describe_internal("user_sso")
        saved = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.SAVE_DRAFT,
                module="user_sso",
                expected_revision=initial.revision,
                config={
                    "issuer": "https://idp.example.com",
                    "authorization_endpoint": (
                        "https://idp.example.com/authorize"
                    ),
                    "token_endpoint": "https://idp.example.com/token",
                    "userinfo_endpoint": "https://idp.example.com/userinfo",
                    "jwks_uri": "https://idp.example.com/jwks",
                    "client_id": "pas-console",
                    "client_secret": "secret",
                    "provider_name": "enterprise",
                    "idp_pkce": True,
                },
            ),
            actor,
        )
        assert saved.module is not None
        validated = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.VALIDATE,
                module="user_sso",
                expected_revision=saved.module["revision"],
            ),
            actor,
        )
        assert validated.module is not None
        assert validated.validation is not None
        validation_id = validated.validation["validation_id"]

        engine_mod._engine = context.engine
        engine_mod._session_factory = context.repository.session_factory
        app = FastAPI()
        app.state.config_service = context.service
        app.include_router(auth_router)

        with (
            patch.object(
                IdentityFederation,
                "discover_endpoints",
                new=AsyncMock(),
            ),
            patch.object(
                IdentityFederation,
                "build_authorize_url",
                side_effect=lambda _redirect_uri, state, **_kwargs: (
                    f"https://idp.example.com/authorize?state={state}",
                    "idp-verifier",
                ),
            ),
            patch.object(
                IdentityFederation,
                "exchange_code",
                new=AsyncMock(
                    return_value={
                        "access_token": "idp-access",
                        "id_token": "idp-id",
                    }
                ),
            ),
            patch.object(
                IdentityFederation,
                "extract_user_identity",
                new=AsyncMock(
                    return_value=UserIdentity(subject="admin-subject")
                ),
            ),
        ):
            started = await context.service.start_user_sso_test(actor)
            state = parse_qs(
                urlsplit(started["authorize_url"]).query
            )["state"][0]
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="https://pas.example.com",
            ) as client:
                callback = await client.get(
                    "/auth/oidc/callback",
                    params={"code": "code", "state": state},
                )
            assert callback.status_code == 200

        status = await context.service.describe_user_sso_test(
            started["id"],
            actor,
        )
        assert status["status"] == "passed"
        activated = await context.service.execute(
            ConfigCommand(
                action=ConfigAction.ACTIVATE,
                module="user_sso",
                expected_revision=validated.module["revision"],
                validation_id=validation_id,
                sso_test_id=started["id"],
                idempotency_key="activate-user-sso",
            ),
            actor,
        )
        assert activated.module is not None
        assert activated.module["workflow_state"] == "ACTIVE"

        async with context.repository.session_factory() as session:
            state_row = await session.get(OIDCLoginState, started["id"])
            assert state_row is not None
            assert state_row.status == "consumed"
            mapping = await session.scalar(
                select(UserExternalIdentity).where(
                    UserExternalIdentity.user_id == admin.id,
                    UserExternalIdentity.identity_provider == "enterprise",
                    UserExternalIdentity.external_subject == "admin-subject",
                )
            )
            assert mapping is not None
    finally:
        await context.close()
