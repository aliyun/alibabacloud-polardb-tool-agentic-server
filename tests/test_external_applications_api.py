from __future__ import annotations

import base64

from mcp.shared.auth import OAuthToken
from sqlalchemy import select

from server.auth.external_tokens import ExternalTokenIdentity
from server.auth.oauth_provider import (
    ACCESS_TOKEN_TYPE,
    TOKEN_EXCHANGE_GRANT_TYPE,
)
from server.config import get_config
from server.models import AuditLog, OAuthExternalApplication

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_feishu_application_test_forwards_identity_context(
    client,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    config = get_config()
    config.server.public_base_url = "https://pas.example.com"
    config.auth.external_token_trust.enabled = True
    config.auth.external_token_trust.provider = "feishu"

    created = await http.post(
        "/api/v1/external-auth/clients",
        json={
            "name": "Feishu application",
            "targets": ["api"],
            "agent_policy": "workspace_default",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text

    captured: dict[str, str | None] = {}

    async def exchange_external_token(
        _provider,
        _client,
        subject_token,
        *,
        resource,
        scopes,
        agent_id,
        identity_source_id,
        feishu_user_id,
        feishu_union_id,
    ):
        assert subject_token == "feishu-user-token"
        assert resource == "https://pas.example.com/api/v1"
        assert scopes == []
        assert agent_id is None
        captured.update(
            identity_source_id=identity_source_id,
            feishu_user_id=feishu_user_id,
            feishu_union_id=feishu_union_id,
        )
        return OAuthToken(
            access_token="pas-token",
            expires_in=300,
            scope="polarrag",
        )

    monkeypatch.setattr(
        "server.api.external_applications.PASAuthProvider.exchange_external_token",
        exchange_external_token,
    )

    tested = await http.post(
        (
            "/api/v1/external-auth/clients/"
            f"{created.json()['client_id']}/test"
        ),
        json={
            "subject_token": "feishu-user-token",
            "resource": "https://pas.example.com/api/v1",
            "identity_source_id": "source-1",
            "feishu_user_id": "ou_123",
            "feishu_union_id": "on_456",
        },
        headers=admin_headers,
    )

    assert tested.status_code == 200, tested.text
    assert captured == {
        "identity_source_id": "source-1",
        "feishu_user_id": "ou_123",
        "feishu_union_id": "on_456",
    }


async def test_admin_creates_external_application_with_one_time_secret(
    client,
    setup,
) -> None:
    http, admin_headers, member_headers = client
    factory, _admin, _member = setup
    config = get_config()
    config.server.public_base_url = "https://pas.example.com"
    config.auth.external_token_trust.enabled = True
    config.auth.external_token_trust.provider = "oauth2_introspection"

    forbidden = await http.get(
        "/api/v1/external-auth/clients/context",
        headers=member_headers,
    )
    assert forbidden.status_code == 403

    created = await http.post(
        "/api/v1/external-auth/clients",
        json={
            "name": "Document gateway",
            "targets": ["mcp", "api"],
            "agent_policy": "workspace_default",
        },
        headers=admin_headers,
    )

    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["client_id"].startswith("pas_ext_")
    assert payload["client_secret"].startswith("pas_secret_")
    assert created.headers["cache-control"] == "no-store"
    assert payload["token_endpoint"] == "https://pas.example.com/token"
    assert payload["compatibility_token_endpoint"] == (
        "https://pas.example.com/api/v1/external-auth/token"
    )
    assert payload["resources"] == [
        {
            "target": "mcp",
            "resource": "https://pas.example.com/mcp",
            "scope": "mcp",
        },
        {
            "target": "api",
            "resource": "https://pas.example.com/api/v1",
            "scope": "polarrag",
        },
    ]

    listed = await http.get(
        "/api/v1/external-auth/clients",
        headers=admin_headers,
    )
    assert listed.status_code == 200
    assert listed.json()["items"][0]["client_id"] == payload["client_id"]
    assert "client_secret" not in listed.json()["items"][0]

    async with factory() as session:
        application = await session.get(
            OAuthExternalApplication,
            payload["client_id"],
        )
        assert application is not None
        assert application.provider_type == "oauth2_introspection"
        audits = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.target_id == payload["client_id"]
                    )
                )
            ).scalars()
        )
        assert [audit.action for audit in audits] == [
            "external_application.create"
        ]


async def test_managed_application_enforces_target_scope_and_status(
    client,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    config = get_config()
    config.server.public_base_url = "https://pas.example.com"
    config.auth.external_token_trust.enabled = True
    config.auth.external_token_trust.provider = "oauth2_userinfo"
    config.auth.external_token_trust.config_digest = "test-digest"

    validate_calls = 0

    async def validate(authenticator, token):
        nonlocal validate_calls
        validate_calls += 1
        assert token == "partner-assertion"
        return ExternalTokenIdentity(
            provider_type="oauth2_userinfo",
            provider_key="partner",
            provider_fingerprint=authenticator._base_fingerprint(),
            subject="user-1",
            display_name="Partner User",
            email=None,
            scopes=(),
            expires_at=None,
            scope_authoritative=False,
        )

    monkeypatch.setattr(
        "server.auth.external_tokens.ExternalTokenAuthenticator.validate",
        validate,
    )
    created = await http.post(
        "/api/v1/external-auth/clients",
        json={
            "name": "Partner app",
            "targets": ["mcp", "api"],
            "agent_policy": "workspace_default",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    application = created.json()
    basic = base64.b64encode(
        (
            f"{application['client_id']}:"
            f"{application['client_secret']}"
        ).encode()
    ).decode()
    headers = {"Authorization": f"Basic {basic}"}
    common = {
        "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
        "subject_token": "partner-assertion",
        "subject_token_type": ACCESS_TOKEN_TYPE,
    }

    missing_resource = await http.post(
        "/token",
        data=common,
        headers=headers,
    )
    assert missing_resource.status_code == 400
    assert missing_resource.json()["error"] == "invalid_target"
    assert validate_calls == 0

    excessive_scope = await http.post(
        "/token",
        data={
            **common,
            "resource": "https://pas.example.com/mcp",
            "scope": "mcp polarrag",
        },
        headers=headers,
    )
    assert excessive_scope.status_code == 400
    assert excessive_scope.json()["error"] == "invalid_scope"
    assert validate_calls == 0

    exchanged = await http.post(
        "/token",
        data={
            **common,
            "resource": "https://pas.example.com/mcp",
        },
        headers=headers,
    )
    assert exchanged.status_code == 200, exchanged.text
    assert exchanged.json()["scope"] == "mcp"
    assert validate_calls == 1

    disabled = await http.put(
        (
            "/api/v1/external-auth/clients/"
            f"{application['client_id']}/status"
        ),
        json={"status": "disabled"},
        headers=admin_headers,
    )
    assert disabled.status_code == 200

    rejected = await http.post(
        "/token",
        data={
            **common,
            "resource": "https://pas.example.com/mcp",
        },
        headers=headers,
    )
    assert rejected.status_code == 400
    assert rejected.json()["error"] == "unauthorized_client"
    assert validate_calls == 1
