from __future__ import annotations

from typing import Any, TypedDict
from urllib.parse import urlencode

import httpx


FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.feishu.cn/open-apis/authen/v1/user_info"


class FeishuTenantVerificationError(ValueError):
    pass


class FeishuUserIdentity(TypedDict):
    tenant_key: str
    user_id: str
    display_name: str
    email: str | None


def feishu_tenant_verification_callback_url(public_base_url: str) -> str:
    normalized_base_url = public_base_url.rstrip("/")
    if not normalized_base_url:
        raise FeishuTenantVerificationError(
            "Public base URL must be configured before Feishu verification"
        )
    return f"{normalized_base_url}/auth/feishu/tenant-verification/callback"


def feishu_user_login_callback_url(public_base_url: str) -> str:
    normalized_base_url = public_base_url.rstrip("/")
    if not normalized_base_url:
        raise FeishuTenantVerificationError(
            "Public base URL must be configured before Feishu login"
        )
    return f"{normalized_base_url}/auth/feishu/login/callback"


def build_feishu_authorization_url(
    *, app_id: str, redirect_uri: str, state: str
) -> str:
    query = urlencode(
        {
            "client_id": app_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{FEISHU_AUTHORIZE_URL}?{query}"


def _json_object(response: httpx.Response, label: str) -> dict[str, Any]:
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise FeishuTenantVerificationError(
            f"Feishu {label} must be valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise FeishuTenantVerificationError(
            f"Feishu {label} must be a JSON object"
        )
    code = payload.get("code")
    if code is not None and code != 0:
        raise FeishuTenantVerificationError(f"Feishu {label} failed")
    return payload


def _response_data(payload: dict[str, Any], label: str) -> dict[str, Any]:
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise FeishuTenantVerificationError(
            f"Feishu {label} data must be a JSON object"
        )
    return data


async def discover_feishu_tenant_key(
    *, app_id: str, app_secret: str, code: str, redirect_uri: str
) -> str:
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            FEISHU_TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        token_data = _response_data(
            _json_object(token_response, "token response"), "token response"
        )
        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise FeishuTenantVerificationError(
                "Feishu user access token is missing"
            )
        user_response = await client.get(
            FEISHU_USER_INFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    user_data = _response_data(
        _json_object(user_response, "user info response"), "user info response"
    )
    tenant_key = user_data.get("tenant_key")
    if not isinstance(tenant_key, str) or not tenant_key.strip():
        raise FeishuTenantVerificationError("Feishu tenant key is missing")
    return tenant_key.strip()


async def authenticate_feishu_user(
    *, app_id: str, app_secret: str, code: str, redirect_uri: str
) -> FeishuUserIdentity:
    async with httpx.AsyncClient() as client:
        token_response = await client.post(
            FEISHU_TOKEN_URL,
            json={
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        token_data = _response_data(
            _json_object(token_response, "token response"), "token response"
        )
        access_token = token_data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise FeishuTenantVerificationError("Feishu user access token is missing")
        user_response = await client.get(
            FEISHU_USER_INFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    user_data = _response_data(
        _json_object(user_response, "user info response"), "user info response"
    )
    tenant_key = user_data.get("tenant_key")
    user_id = user_data.get("user_id")
    display_name = user_data.get("name")
    email = user_data.get("email")
    if not isinstance(tenant_key, str) or not tenant_key.strip():
        raise FeishuTenantVerificationError("Feishu tenant key is missing")
    if not isinstance(user_id, str) or not user_id.strip():
        raise FeishuTenantVerificationError("Feishu user ID is missing")
    if not isinstance(display_name, str) or not display_name.strip():
        raise FeishuTenantVerificationError("Feishu user name is missing")
    if email is not None and not isinstance(email, str):
        raise FeishuTenantVerificationError("Feishu user email is invalid")
    return {
        "tenant_key": tenant_key.strip(),
        "user_id": user_id.strip(),
        "display_name": display_name.strip(),
        "email": email,
    }
