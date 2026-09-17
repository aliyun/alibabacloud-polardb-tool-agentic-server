from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse
from starlette.types import Scope

from server.auth.oauth_provider import (
    ACCESS_TOKEN_TYPE,
    TOKEN_EXCHANGE_GRANT_TYPE,
    PASAuthProvider,
    handle_external_token_exchange,
    parse_oauth_form,
)
from server.auth.rate_limit import (
    AuthRateLimitExceeded,
    check_token_endpoint,
)
from server.config import get_config
from server.db.engine import get_session_factory

router = APIRouter(prefix="/v1/external-auth", tags=["external-auth"])

_TOKEN_BODY_LIMIT = 65_536


class ExternalTokenExchangeResponse(BaseModel):
    access_token: str
    issued_token_type: str
    token_type: str
    expires_in: int
    scope: str


class OAuthErrorResponse(BaseModel):
    error: str
    error_description: str


@router.post(
    "/token",
    response_model=ExternalTokenExchangeResponse,
    responses={
        400: {
            "model": OAuthErrorResponse,
            "description": "Invalid Token Exchange request or grant.",
        },
        401: {
            "model": OAuthErrorResponse,
            "description": "OAuth client authentication failed.",
        },
        413: {
            "model": OAuthErrorResponse,
            "description": "Request body is too large.",
        },
        429: {
            "model": OAuthErrorResponse,
            "description": "Authentication rate limit exceeded.",
        },
        503: {
            "model": OAuthErrorResponse,
            "description": "External identity provider is unavailable.",
        },
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": {
                        "type": "object",
                        "required": [
                            "grant_type",
                            "subject_token",
                            "subject_token_type",
                        ],
                        "properties": {
                            "grant_type": {
                                "type": "string",
                                "enum": [TOKEN_EXCHANGE_GRANT_TYPE],
                            },
                            "subject_token": {
                                "type": "string",
                                "format": "password",
                            },
                            "subject_token_type": {
                                "type": "string",
                                "enum": [ACCESS_TOKEN_TYPE],
                            },
                            "identity_source_id": {
                                "type": "string",
                                "description": (
                                    "Required with feishu_user_id and "
                                    "feishu_union_id when the external token "
                                    "provider is Feishu."
                                ),
                            },
                            "feishu_user_id": {
                                "type": "string",
                                "description": (
                                    "Required Feishu user_id for a Feishu "
                                    "Token Exchange request."
                                ),
                            },
                            "feishu_union_id": {
                                "type": "string",
                                "description": (
                                    "Required Feishu union_id for a Feishu "
                                    "Token Exchange request."
                                ),
                            },
                            "resource": {
                                "type": "string",
                                "format": "uri",
                            },
                            "scope": {"type": "string"},
                            "agent_id": {"type": "string"},
                            "requested_token_type": {
                                "type": "string",
                                "enum": [ACCESS_TOKEN_TYPE],
                            },
                            "client_id": {"type": "string"},
                            "client_secret": {
                                "type": "string",
                                "format": "password",
                            },
                        },
                    }
                }
            },
        }
    },
)
async def exchange_external_token(request: Request) -> JSONResponse:
    try:
        await check_token_endpoint(request.scope, request.url.path)
    except AuthRateLimitExceeded as exc:
        return JSONResponse(
            {
                "error": "rate_limit_exceeded",
                "error_description": "Too many authentication requests.",
            },
            status_code=429,
            headers={
                "Retry-After": str(exc.retry_after),
                "Cache-Control": "no-store",
            },
        )

    raw_body = await request.body()
    if len(raw_body) > _TOKEN_BODY_LIMIT:
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": (
                    "Authentication request body is too large"
                ),
            },
            status_code=413,
            headers={"Cache-Control": "no-store"},
        )
    form = parse_oauth_form(raw_body)
    if form.get("grant_type") != [TOKEN_EXCHANGE_GRANT_TYPE]:
        return JSONResponse(
            {
                "error": "unsupported_grant_type",
                "error_description": (
                    "Only OAuth Token Exchange is supported at this endpoint"
                ),
            },
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    provider = PASAuthProvider(get_session_factory(), get_config())
    return await handle_external_token_exchange(
        provider,
        cast(Scope, request.scope),
        form,
    )
