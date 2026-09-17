from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from scripts.dev.mock_external_oauth_provider import (
    MockProviderConfig,
    create_app,
)


def _client() -> TestClient:
    return TestClient(
        create_app(
            MockProviderConfig(
                client_id="pas-to-provider",
                client_secret="provider-secret",
                pas_source_id="source-1",
                user_id="ou-user-1",
                union_id="on-user-1",
            )
        )
    )


def _basic(client_id: str, client_secret: str) -> str:
    encoded = base64.b64encode(
        f"{client_id}:{client_secret}".encode()
    ).decode()
    return f"Basic {encoded}"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def test_browser_oauth_login_supports_authorization_code_with_pkce() -> None:
    client = _client()
    verifier = "local-test-verifier-with-sufficient-length-1234567890"
    authorize = client.get(
        "/oauth2/authorize",
        params={
            "response_type": "code",
            "client_id": "pas-console",
            "redirect_uri": "https://pas.example.com/auth/oidc/callback",
            "state": "state-1",
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )

    assert authorize.status_code == 303
    callback = urlsplit(authorize.headers["location"])
    callback_query = parse_qs(callback.query)
    assert callback_query["state"] == ["state-1"]

    token = client.post(
        "/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": callback_query["code"][0],
            "redirect_uri": "https://pas.example.com/auth/oidc/callback",
            "client_id": "pas-console",
            "client_secret": "local-console-secret",
            "code_verifier": verifier,
        },
    )

    assert token.status_code == 200
    assert token.json()["token_type"] == "Bearer"
    assert token.headers["cache-control"] == "no-store"

    user_info = client.get(
        "/oauth2/login/user_info",
        headers={
            "Authorization": f"Bearer {token.json()['access_token']}",
        },
    )
    assert user_info.status_code == 200
    assert user_info.json() == {
        "sub": "mock-admin",
        "name": "Mock Administrator",
        "email": "mock-admin@example.com",
    }


def test_browser_oauth_login_rejects_invalid_pkce_and_code_replay() -> None:
    client = _client()
    verifier = "local-test-verifier-with-sufficient-length-1234567890"
    authorize = client.get(
        "/oauth2/authorize",
        params={
            "response_type": "code",
            "client_id": "pas-console",
            "redirect_uri": "https://pas.example.com/auth/oidc/callback",
            "state": "state-1",
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
        },
        follow_redirects=False,
    )
    code = parse_qs(urlsplit(authorize.headers["location"]).query)["code"][0]
    token_request = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://pas.example.com/auth/oidc/callback",
        "client_id": "pas-console",
        "client_secret": "local-console-secret",
    }

    invalid_pkce = client.post(
        "/oauth2/token",
        data={
            **token_request,
            "code_verifier": "wrong-verifier",
        },
    )
    accepted = client.post(
        "/oauth2/token",
        data={
            **token_request,
            "code_verifier": verifier,
        },
    )
    replay = client.post(
        "/oauth2/token",
        data={
            **token_request,
            "code_verifier": verifier,
        },
    )

    assert invalid_pkce.status_code == 400
    assert invalid_pkce.json() == {"error": "invalid_grant"}
    assert accepted.status_code == 200
    assert replay.status_code == 400
    assert replay.json() == {"error": "invalid_grant"}


def test_introspection_accepts_valid_assertion() -> None:
    response = _client().post(
        "/oauth2/introspect",
        data={
            "token": "mock-valid",
            "token_type_hint": "access_token",
        },
        headers={
            "Authorization": _basic(
                "pas-to-provider",
                "provider-secret",
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["active"] is True
    assert response.json()["sub"] == "mock-user-123"
    assert response.json()["aud"] == "polarrag"
    assert response.headers["cache-control"] == "no-store"


def test_introspection_distinguishes_inactive_and_client_failure() -> None:
    inactive = _client().post(
        "/oauth2/introspect",
        data={"token": "mock-inactive"},
        headers={
            "Authorization": _basic(
                "pas-to-provider",
                "provider-secret",
            )
        },
    )
    invalid_client = _client().post(
        "/oauth2/introspect",
        data={"token": "mock-valid"},
        headers={
            "Authorization": _basic(
                "pas-to-provider",
                "wrong-secret",
            )
        },
    )

    assert inactive.status_code == 200
    assert inactive.json() == {"active": False}
    assert invalid_client.status_code == 401
    assert invalid_client.json() == {"error": "invalid_client"}


def test_user_info_returns_pas_identity_mapping() -> None:
    response = _client().get(
        "/oauth2/user_info",
        headers={"Authorization": "Bearer mock-valid"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "sub": "mock-user-123",
        "user_id": "ou-user-1",
        "pas_source_id": "source-1",
        "union_id": "on-user-1",
    }


def test_user_info_exposes_mapping_failure_fixtures() -> None:
    subject_mismatch = _client().get(
        "/oauth2/user_info",
        headers={"Authorization": "Bearer mock-subject-mismatch"},
    )
    source_mismatch = _client().get(
        "/oauth2/user_info",
        headers={"Authorization": "Bearer mock-source-mismatch"},
    )
    missing = _client().get(
        "/oauth2/user_info",
        headers={"Authorization": "Bearer mock-userinfo-not-found"},
    )

    assert subject_mismatch.json()["sub"] == "different-mock-user"
    assert source_mismatch.json()["pas_source_id"] == "different-source-id"
    assert missing.status_code == 404
    assert missing.json() == {"error": "identity_not_found"}
