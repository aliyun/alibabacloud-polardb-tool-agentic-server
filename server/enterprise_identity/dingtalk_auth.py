"""Browser authorization and enterprise membership proof for DingTalk."""

from urllib.parse import urlencode, urlsplit
import secrets
import hashlib
import json
import httpx
from server.enterprise_identity.dingtalk import DingTalkDirectoryClient, DingTalkIdentityError


class DingTalkCallbackConfigurationError(ValueError):
    """Safe, actionable failure before creating browser authorization state."""


def dingtalk_callback_url(public_base_url: str) -> str:
    base_url = public_base_url.strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        # Accessing port validates malformed ports even when a hostname exists.
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise DingTalkCallbackConfigurationError(
            "Configure runtime_policy.external_base_url as an absolute HTTP(S) URL before DingTalk authorization."
        )
    return base_url + "/auth/dingtalk/callback"


def build_dingtalk_authorization_url(*, client_id: str, tenant_id: str, redirect_uri: str, state: str) -> str:
    return "https://login.dingtalk.com/oauth2/auth?" + urlencode(
        {
            "client_id": client_id,
            "corpId": tenant_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid corpid",
            "state": state,
            "prompt": "consent",
        }
    )


async def authenticate_dingtalk_user(
    *, client_id: str, client_secret: str, tenant_id: str, code: str, transport: httpx.AsyncBaseTransport | None = None
) -> dict:
    async with DingTalkDirectoryClient(
        tenant_id=tenant_id, client_id=client_id, client_secret=client_secret, transport=transport
    ) as client:
        token = await client.request(
            "POST",
            "https://api.dingtalk.com/v1.0/oauth2/userAccessToken",
            retry=False,
            json={
                "clientId": client_id,
                "clientSecret": client_secret,
                "code": code,
                "grantType": "authorization_code",
            },
        )
        corp = token.get("corpId")
        access_token = token.get("accessToken")
        if not isinstance(corp, str) or not secrets.compare_digest(corp, tenant_id):
            raise DingTalkIdentityError("TENANT_MISMATCH")
        if not isinstance(access_token, str) or not access_token:
            raise DingTalkIdentityError("INVALID_USER_TOKEN")
        return await client.authenticate_token(access_token)


def dingtalk_config_digest(source, config: dict) -> str:
    payload = {
        "tenant_id": source.tenant_id,
        "client_id": config.get("client_id"),
        "client_secret": config.get("client_secret"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def verified_dingtalk_config(source) -> dict:
    from server.core.crypto import decrypt

    config = json.loads(decrypt(source.config_ciphertext)) if source.config_ciphertext else {}
    if not isinstance(config, dict) or config.get("tenant_verified_digest") != dingtalk_config_digest(source, config):
        raise DingTalkIdentityError("SOURCE_NOT_VERIFIED")
    return config
