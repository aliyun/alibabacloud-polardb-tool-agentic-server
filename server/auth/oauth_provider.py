from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import secrets
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from urllib.parse import (
    parse_qs,
    parse_qsl,
    unquote,
    urlencode,
    urlparse,
    urlunparse,
)

import jwt
from jwt import PyJWTError
from pydantic import AnyUrl, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
)
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

from server.auth.jwt_manager import _load_keys, get_public_key
from server.auth.external_tokens import (
    ExternalTokenAuthenticator,
    ExternalTokenInvalid,
    ExternalTokenUnavailable,
)
from server.auth.principal import (
    InvalidPrincipalSubject,
    PrincipalKind,
    agent_subject,
    parse_subject,
    user_subject,
)
from server.config import AppConfig
from server.core import agent_user_token_service, personal_token_service
from server.auth.personal_access import is_personal_access, request_mcp_mode
from server.core.agent_token_service import hash_agent_token
from server.core.crypto import decrypt, encrypt
from server.core.external_application_service import (
    ExternalApplicationUnauthorized,
    mark_external_application_used,
    resolve_managed_exchange_policy,
)
from server.core.user_workspace import resolve_authorized_agent
from server.models import (
    Agent,
    AgentAPIToken,
    AgentStatus,
    AgentUserToken,
    AuthProvider,
    User,
    UserStatus,
)
from server.models.oauth import (
    ExternalTokenSession,
    OAuthAuthorizationCode,
    OAuthDeniedJTI,
    OAuthExternalApplication,
    OAuthPendingAuth,
    OAuthRefreshToken,
    OAuthRegisteredClient,
)

logger = logging.getLogger(__name__)

_raw_registration_redirect_uris: ContextVar[tuple[str, ...] | None] = (
    ContextVar("raw_registration_redirect_uris", default=None)
)
_raw_authorize_redirect_uri: ContextVar[str | None] = ContextVar(
    "raw_authorize_redirect_uri", default=None
)
_AUTH_ENDPOINT_BODY_LIMIT = 65_536
TOKEN_EXCHANGE_GRANT_TYPE = (
    "urn:ietf:params:oauth:grant-type:token-exchange"
)
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


def _invalid_auth_request(
    description: str,
    *,
    status_code: int = 400,
) -> JSONResponse:
    return JSONResponse(
        {
            "error": "invalid_request",
            "error_description": description,
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _is_http_loopback_redirect(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "http"
        and hostname is not None
        and hostname.lower() in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


def _redirect_uri_matches(registered: str, requested: str) -> bool:
    if registered == requested:
        return True
    if not (
        _is_http_loopback_redirect(registered)
        and _is_http_loopback_redirect(requested)
    ):
        return False
    registered_url = urlparse(registered)
    requested_url = urlparse(requested)
    return (
        registered_url.scheme.lower() == requested_url.scheme.lower()
        and (registered_url.hostname or "").lower()
        == (requested_url.hostname or "").lower()
        and registered_url.path == requested_url.path
        and registered_url.params == requested_url.params
        and registered_url.query == requested_url.query
    )


def _basic_client_id(scope: Scope) -> str | None:
    credentials = _basic_client_credentials(scope)
    return credentials[0] if credentials is not None else None


def _basic_client_credentials(
    scope: Scope,
) -> tuple[str, str] | None:
    values = [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == b"authorization"
    ]
    if len(values) != 1:
        return None
    try:
        authorization = values[0].decode("latin-1")
        if not authorization.startswith("Basic "):
            return None
        decoded = base64.b64decode(
            authorization[6:],
            validate=True,
        ).decode("utf-8")
        if ":" not in decoded:
            return None
        client_id, client_secret = decoded.split(":", 1)
        return unquote(client_id), unquote(client_secret)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None


def _with_content_length(
    scope: Scope,
    body: bytes,
) -> Scope:
    updated = dict(scope)
    headers = [
        (name, value)
        for name, value in scope.get("headers", [])
        if name.lower() != b"content-length"
    ]
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    updated["headers"] = headers
    return cast(Scope, updated)


class OAuthRedirectURIExactMatchMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        session_factory: async_sessionmaker[AsyncSession],
        provider: "PASAuthProvider",
    ) -> None:
        self._app = app
        self._session_factory = session_factory
        self._provider = provider

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        registration_token = None
        registration_response = None
        authorize_token = None
        effective_receive = receive
        if scope["type"] == "http":
            path = scope.get("path", "")
            method = scope.get("method", "")
            if method == "POST" and path in {
                "/register",
                "/token",
                "/revoke",
            }:
                for name, value in scope.get("headers", []):
                    if name.lower() != b"content-length":
                        continue
                    try:
                        declared_length = int(value)
                    except ValueError:
                        continue
                    if declared_length > _AUTH_ENDPOINT_BODY_LIMIT:
                        response = _invalid_auth_request(
                            "Authentication request body is too large",
                            status_code=413,
                        )
                        await response(scope, receive, send)
                        return
                body_parts: list[bytes] = []
                body_size = 0
                while True:
                    message = await receive()
                    if message["type"] == "http.request":
                        body = message.get("body", b"")
                        body_size += len(body)
                        if body_size > _AUTH_ENDPOINT_BODY_LIMIT:
                            response = _invalid_auth_request(
                                "Authentication request body is too large",
                                status_code=413,
                            )
                            await response(scope, receive, send)
                            return
                        body_parts.append(body)
                        if not message.get("more_body", False):
                            break
                    else:
                        break
                raw_body = b"".join(body_parts)
                if path == "/register":
                    try:
                        payload = json.loads(raw_body)
                    except (TypeError, ValueError):
                        payload = None
                    if isinstance(payload, dict):
                        values = payload.get("redirect_uris")
                        if (
                            isinstance(values, list)
                            and all(isinstance(value, str) for value in values)
                        ):
                            registration_token = (
                                _raw_registration_redirect_uris.set(
                                    tuple(values)
                                )
                            )
                        if payload.get("grant_types") == [
                            TOKEN_EXCHANGE_GRANT_TYPE
                        ]:
                            registration_response = (
                                await _register_token_exchange_client(
                                    self._provider,
                                    payload,
                                )
                            )
                else:
                    form_pairs = parse_qsl(
                        raw_body.decode("latin-1"),
                        keep_blank_values=True,
                    )
                    client_ids = [
                        value
                        for name, value in form_pairs
                        if name == "client_id"
                    ]
                    basic_client_id = _basic_client_id(scope)
                    if basic_client_id is not None:
                        if len(client_ids) > 1 or (
                            client_ids
                            and client_ids[0] != basic_client_id
                        ):
                            response = _invalid_auth_request(
                                "Client ID does not match Basic authentication"
                            )
                            await response(scope, receive, send)
                            return
                        if not client_ids:
                            form_pairs.append(
                                ("client_id", basic_client_id)
                            )
                            raw_body = urlencode(form_pairs).encode()
                            scope = _with_content_length(scope, raw_body)
                    form: dict[str, list[str]] = {}
                    for name, value in form_pairs:
                        form.setdefault(name, []).append(value)
                    grant_types = form.get("grant_type", [])
                    codes = form.get("code", [])
                    redirect_uris = form.get("redirect_uri", [])
                    if "authorization_code" in grant_types and (
                        grant_types != ["authorization_code"]
                        or len(codes) != 1
                        or len(redirect_uris) != 1
                    ):
                        response = _invalid_auth_request(
                            "Authorization code request parameters "
                            "must appear exactly once"
                        )
                        await response(scope, receive, send)
                        return
                    if (
                        grant_types == ["authorization_code"]
                        and len(codes) == 1
                        and len(redirect_uris) == 1
                    ):
                        code_hash = hashlib.sha256(
                            codes[0].encode()
                        ).hexdigest()
                        async with self._session_factory() as session:
                            stored_redirect_uri = await session.scalar(
                                select(
                                    OAuthAuthorizationCode.redirect_uri
                                ).where(
                                    OAuthAuthorizationCode.code_hash
                                    == code_hash
                                )
                            )
                        if (
                            stored_redirect_uri is not None
                            and redirect_uris[0] != stored_redirect_uri
                        ):
                            response = JSONResponse(
                                {
                                    "error": "invalid_grant",
                                    "error_description": (
                                        "Redirect URI does not match the "
                                        "authorization request"
                                    ),
                                },
                                status_code=400,
                                headers={"Cache-Control": "no-store"},
                            )
                            await response(scope, receive, send)
                            return
                replay_messages: list[Message] = [
                    {
                        "type": "http.request",
                        "body": raw_body,
                        "more_body": False,
                    }
                ]
                message_index = 0

                async def replay_receive() -> Message:
                    nonlocal message_index
                    if message_index < len(replay_messages):
                        message = replay_messages[message_index]
                        message_index += 1
                        return message
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }

                effective_receive = replay_receive
            elif method == "GET" and path == "/authorize":
                query = parse_qs(
                    scope.get("query_string", b"").decode("latin-1"),
                    keep_blank_values=True,
                )
                values = query.get("redirect_uri", [])
                if len(values) == 1:
                    authorize_token = _raw_authorize_redirect_uri.set(
                        values[0]
                    )
                    client_ids = query.get("client_id", [])
                    if len(client_ids) == 1:
                        async with self._session_factory() as session:
                            stored = await session.scalar(
                                select(
                                    OAuthRegisteredClient.redirect_uris
                                ).where(
                                    OAuthRegisteredClient.client_id
                                    == client_ids[0]
                                )
                            )
                        if stored is not None:
                            try:
                                registered = set(json.loads(stored))
                            except (TypeError, ValueError):
                                registered = set()
                            matched_redirect_uri = next(
                                (
                                    registered_uri
                                    for registered_uri in registered
                                    if _redirect_uri_matches(
                                        registered_uri,
                                        values[0],
                                    )
                                ),
                                None,
                            )
                            if matched_redirect_uri is None:
                                response = JSONResponse(
                                    {
                                        "error": "invalid_request",
                                        "error_description": (
                                            "Redirect URI is not registered "
                                            "for this client"
                                        ),
                                    },
                                    status_code=400,
                                    headers={
                                        "Cache-Control": "no-store"
                                    },
                                )
                                await response(scope, receive, send)
                                _raw_authorize_redirect_uri.reset(
                                    authorize_token
                                )
                                return
                            if matched_redirect_uri != values[0]:
                                query_pairs = parse_qsl(
                                    scope.get(
                                        "query_string",
                                        b"",
                                    ).decode("latin-1"),
                                    keep_blank_values=True,
                                )
                                normalized_pairs = [
                                    (
                                        name,
                                        matched_redirect_uri
                                        if name == "redirect_uri"
                                        else value,
                                    )
                                    for name, value in query_pairs
                                ]
                                scope = cast(Scope, dict(scope))
                                scope["query_string"] = urlencode(
                                    normalized_pairs
                                ).encode("ascii")
        try:
            if registration_response is not None:
                await registration_response(scope, receive, send)
                return
            await self._app(scope, effective_receive, send)
        finally:
            if authorize_token is not None:
                _raw_authorize_redirect_uri.reset(authorize_token)
            if registration_token is not None:
                _raw_registration_redirect_uris.reset(registration_token)


class OAuthMetadataCompatibilityMiddleware:
    """Add OAuth metadata fields not exposed by the current MCP SDK."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "GET"
            or scope.get("path")
            != "/.well-known/oauth-authorization-server"
        ):
            await self._app(scope, receive, send)
            return

        messages: list[Message] = []

        async def capture_send(message: Message) -> None:
            messages.append(message)

        await self._app(scope, receive, capture_send)
        start = next(
            (
                message
                for message in messages
                if message["type"] == "http.response.start"
            ),
            None,
        )
        if start is None or start["status"] != 200:
            for message in messages:
                await send(message)
            return

        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        try:
            metadata = json.loads(body)
        except (TypeError, ValueError):
            for message in messages:
                await send(message)
            return
        if not isinstance(metadata, dict):
            for message in messages:
                await send(message)
            return

        auth_methods = [
            "none",
            "client_secret_basic",
            "client_secret_post",
        ]
        metadata["token_endpoint_auth_methods_supported"] = auth_methods
        if "revocation_endpoint" in metadata:
            metadata["revocation_endpoint_auth_methods_supported"] = (
                auth_methods
            )
        grant_types = metadata.setdefault("grant_types_supported", [])
        if TOKEN_EXCHANGE_GRANT_TYPE not in grant_types:
            grant_types.append(TOKEN_EXCHANGE_GRANT_TYPE)
        metadata["authorization_response_iss_parameter_supported"] = True
        encoded = json.dumps(
            metadata,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = [
            (name, value)
            for name, value in start.get("headers", [])
            if name.lower() != b"content-length"
        ]
        headers.append(
            (b"content-length", str(len(encoded)).encode("ascii"))
        )
        await send(
            {
                "type": "http.response.start",
                "status": start["status"],
                "headers": headers,
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": encoded,
                "more_body": False,
            }
        )


def _oauth_error(
    error: str,
    description: str,
    *,
    status_code: int = 400,
    authenticate: bool = False,
) -> JSONResponse:
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if authenticate:
        headers["WWW-Authenticate"] = 'Basic realm="token"'
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers=headers,
    )


async def _register_token_exchange_client(
    provider: "PASAuthProvider",
    payload: dict[str, Any],
) -> JSONResponse:
    token_exchange_payload = dict(payload)
    token_exchange_payload.setdefault("redirect_uris", None)
    token_exchange_payload.setdefault("response_types", [])
    try:
        metadata = OAuthClientMetadata.model_validate(
            token_exchange_payload
        )
    except ValidationError as exc:
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": str(exc),
            },
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    requested_scopes = set((metadata.scope or "").split())
    if not requested_scopes or not requested_scopes.issubset(
        {"mcp", "polarrag"}
    ):
        return JSONResponse(
            {
                "error": "invalid_client_metadata",
                "error_description": "Requested scopes are not valid",
            },
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    auth_method = (
        metadata.token_endpoint_auth_method or "client_secret_post"
    )
    client_secret = (
        None if auth_method == "none" else secrets.token_hex(32)
    )
    issued_at = int(time.time())
    client_info = OAuthClientInformationFull(
        client_id=str(uuid.uuid4()),
        client_id_issued_at=issued_at,
        client_secret=client_secret,
        client_secret_expires_at=None,
        redirect_uris=metadata.redirect_uris,
        token_endpoint_auth_method=auth_method,
        grant_types=metadata.grant_types,
        response_types=metadata.response_types,
        client_name=metadata.client_name,
        client_uri=metadata.client_uri,
        logo_uri=metadata.logo_uri,
        scope=metadata.scope,
        contacts=metadata.contacts,
        tos_uri=metadata.tos_uri,
        policy_uri=metadata.policy_uri,
        jwks_uri=metadata.jwks_uri,
        jwks=metadata.jwks,
        software_id=metadata.software_id,
        software_version=metadata.software_version,
    )
    try:
        await provider.register_client(client_info)
    except RegistrationError as exc:
        return JSONResponse(
            {
                "error": exc.error,
                "error_description": exc.error_description,
            },
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    return JSONResponse(
        client_info.model_dump(mode="json", exclude_none=True),
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


def parse_oauth_form(raw_body: bytes) -> dict[str, list[str]]:
    form: dict[str, list[str]] = {}
    for name, value in parse_qsl(
        raw_body.decode("latin-1"),
        keep_blank_values=True,
    ):
        form.setdefault(name, []).append(value)
    return form


async def handle_external_token_exchange(
    provider: "PASAuthProvider",
    scope: Scope,
    form: dict[str, list[str]],
) -> JSONResponse:
    def one(name: str, *, required: bool = False) -> str | None:
        values = form.get(name, [])
        if len(values) > 1 or (required and len(values) != 1):
            raise ValueError(
                f"Token exchange parameter '{name}' must appear exactly once"
            )
        if not values:
            return None
        if required and not values[0]:
            raise ValueError(
                f"Token exchange parameter '{name}' is required"
            )
        return values[0]

    try:
        form_client_id = one("client_id")
        subject_token = one("subject_token", required=True)
        subject_token_type = one("subject_token_type", required=True)
        resource = one("resource")
        scope_value = one("scope")
        requested_token_type = one("requested_token_type")
        actor_token = one("actor_token")
        client_secret = one("client_secret")
        agent_id = one("agent_id")
        identity_source_id = one("identity_source_id")
        feishu_user_id = one("feishu_user_id")
        feishu_union_id = one("feishu_union_id")
    except ValueError as exc:
        return _oauth_error("invalid_request", str(exc))

    if subject_token_type != ACCESS_TOKEN_TYPE:
        return _oauth_error(
            "invalid_request",
            "Only OAuth access tokens are accepted as subject_token",
        )
    if requested_token_type not in {None, "", ACCESS_TOKEN_TYPE}:
        return _oauth_error(
            "invalid_request",
            "Only PAS access tokens may be requested",
        )
    if actor_token is not None:
        return _oauth_error(
            "invalid_request",
            "Actor token exchange is not supported",
        )

    basic = _basic_client_credentials(scope)
    if (
        form_client_id
        and basic is not None
        and form_client_id != basic[0]
    ):
        return _oauth_error(
            "invalid_client",
            "OAuth client authentication failed",
            status_code=401,
            authenticate=True,
        )
    client_id = form_client_id or (
        basic[0] if basic is not None else None
    )
    if not client_id:
        return _oauth_error(
            "invalid_client",
            "OAuth client authentication failed",
            status_code=401,
            authenticate=True,
        )

    client = await provider.get_client(client_id)
    if client is None:
        return _oauth_error(
            "invalid_client",
            "OAuth client authentication failed",
            status_code=401,
            authenticate=True,
        )
    if TOKEN_EXCHANGE_GRANT_TYPE not in set(client.grant_types or []):
        return _oauth_error(
            "unauthorized_client",
            "OAuth client is not registered for Token Exchange",
        )
    method = client.token_endpoint_auth_method or "client_secret_basic"
    if method == "none":
        valid_client = basic is None and client_secret in {None, ""}
    elif method == "client_secret_basic":
        valid_client = (
            basic is not None
            and basic[0] == client.client_id
            and client_secret is None
            and client.client_secret is not None
            and secrets.compare_digest(
                basic[1],
                client.client_secret,
            )
        )
    elif method == "client_secret_post":
        valid_client = (
            basic is None
            and client_secret is not None
            and client.client_secret is not None
            and secrets.compare_digest(
                client_secret,
                client.client_secret,
            )
        )
    else:
        valid_client = False
    secret_expires = client.client_secret_expires_at
    if (
        secret_expires
        and secret_expires > 0
        and secret_expires <= int(time.time())
    ):
        valid_client = False
    if not valid_client:
        return _oauth_error(
            "invalid_client",
            "OAuth client authentication failed",
            status_code=401,
            authenticate=True,
        )

    try:
        token = await provider.exchange_external_token(
            client,
            cast(str, subject_token),
            resource=resource,
            scopes=scope_value.split() if scope_value else [],
            agent_id=agent_id,
            identity_source_id=identity_source_id,
            feishu_user_id=feishu_user_id,
            feishu_union_id=feishu_union_id,
        )
    except ExternalApplicationUnauthorized as exc:
        return _oauth_error("unauthorized_client", str(exc))
    except ExternalTokenUnavailable:
        return _oauth_error(
            "temporarily_unavailable",
            "External identity provider is temporarily unavailable",
            status_code=503,
        )
    except ExternalTokenInvalid as exc:
        return _oauth_error("invalid_grant", str(exc))
    except ValueError as exc:
        description = str(exc)
        error = (
            "invalid_target"
            if "resource" in description.lower()
            or "agent" in description.lower()
            else "invalid_scope"
            if "scope" in description.lower()
            else "invalid_request"
        )
        return _oauth_error(error, description)

    payload = {
        "access_token": token.access_token,
        "issued_token_type": ACCESS_TOKEN_TYPE,
        "token_type": "Bearer",
        "expires_in": token.expires_in,
        "scope": token.scope or "",
    }
    if token.refresh_token:
        payload["refresh_token"] = token.refresh_token
    return JSONResponse(
        payload,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )


class OAuthTokenExchangeMiddleware:
    """Handle RFC 8693 requests before the MCP SDK token endpoint."""

    def __init__(self, app: ASGIApp, provider: "PASAuthProvider") -> None:
        self._app = app
        self._provider = provider

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/token"
        ):
            await self._app(scope, receive, send)
            return

        message = await receive()
        raw_body = message.get("body", b"")
        form = parse_oauth_form(raw_body)
        grant_types = form.get("grant_type", [])
        if grant_types != [TOKEN_EXCHANGE_GRANT_TYPE]:
            replayed = False

            async def replay_receive() -> Message:
                nonlocal replayed
                if replayed:
                    return {
                        "type": "http.request",
                        "body": b"",
                        "more_body": False,
                    }
                replayed = True
                return {
                    "type": "http.request",
                    "body": raw_body,
                    "more_body": False,
                }

            try:
                await self._app(scope, replay_receive, send)
            except ExternalTokenUnavailable:
                response = _oauth_error(
                    "temporarily_unavailable",
                    "External identity provider is temporarily unavailable",
                    status_code=503,
                )
                await response(scope, receive, send)
            return

        response = await handle_external_token_exchange(
            self._provider,
            scope,
            form,
        )
        await response(scope, receive, send)


# ─── JTI Deny-list Cache ──────────────────────────────────────────────
# In-memory cache of revoked JWT IDs to short-circuit DB lookups when
# validating access tokens. Backed by an async lock for concurrency safety.

_jti_deny_cache: dict[str, float] = {}
_jti_deny_lock = asyncio.Lock()
_JTI_CACHE_MAX = 4096
_agent_usage_tasks: set[asyncio.Task[None]] = set()


def _builtin_credential_claim(user: User) -> dict[str, int]:
    if user.auth_provider == AuthProvider.BUILTIN:
        return {"credential_epoch": user.credential_epoch}
    return {}


async def _record_agent_token_use(
    session_factory: async_sessionmaker[AsyncSession],
    token_id: str,
    used_at: datetime,
) -> None:
    """Best-effort CAS update; authentication never depends on telemetry."""
    try:
        async with session_factory() as session:
            await session.execute(
                update(AgentAPIToken)
                .where(
                    AgentAPIToken.id == token_id,
                    (
                        AgentAPIToken.last_used_at.is_(None)
                        | (AgentAPIToken.last_used_at <= used_at - timedelta(minutes=5))
                    ),
                )
                .values(last_used_at=used_at)
            )
            await session.commit()
    except Exception:
        logger.warning(
            "agent token usage telemetry update failed",
            extra={"action": "agent_token_usage_telemetry"},
        )


def _schedule_agent_token_use(
    session_factory: async_sessionmaker[AsyncSession],
    token_id: str,
    used_at: datetime,
) -> None:
    task = asyncio.create_task(_record_agent_token_use(session_factory, token_id, used_at))
    _agent_usage_tasks.add(task)

    def finish(completed: asyncio.Task[None]) -> None:
        _agent_usage_tasks.discard(completed)
        try:
            completed.result()
        except Exception:
            logger.warning(
                "agent token usage telemetry task failed",
                extra={"action": "agent_token_usage_telemetry"},
            )

    task.add_done_callback(finish)


async def _record_agent_user_token_use(
    session_factory: async_sessionmaker[AsyncSession],
    token_id: str,
    used_at: datetime,
) -> None:
    try:
        async with session_factory() as session:
            await session.execute(
                update(AgentUserToken)
                .where(
                    AgentUserToken.id == token_id,
                    (
                        AgentUserToken.last_used_at.is_(None)
                        | (
                            AgentUserToken.last_used_at
                            <= used_at - timedelta(minutes=5)
                        )
                    ),
                )
                .values(last_used_at=used_at)
            )
            await session.commit()
    except Exception:
        logger.warning(
            "agent user token usage telemetry update failed",
            extra={"action": "agent_user_token_usage_telemetry"},
        )


def _schedule_agent_user_token_use(
    session_factory: async_sessionmaker[AsyncSession],
    token_id: str,
    used_at: datetime,
) -> None:
    task = asyncio.create_task(
        _record_agent_user_token_use(session_factory, token_id, used_at)
    )
    _agent_usage_tasks.add(task)

    def finish(completed: asyncio.Task[None]) -> None:
        _agent_usage_tasks.discard(completed)
        try:
            completed.result()
        except Exception:
            logger.warning(
                "agent user token usage telemetry task failed",
                extra={"action": "agent_user_token_usage_telemetry"},
            )

    task.add_done_callback(finish)


async def _jti_is_denied_cached(jti: str) -> bool | None:
    """Check in-memory cache. Returns True/False if cached, None if miss."""
    async with _jti_deny_lock:
        exp = _jti_deny_cache.get(jti)
        if exp is None:
            return None
        if time.time() > exp:
            _jti_deny_cache.pop(jti, None)
            return None
        return True


async def _jti_cache_deny(jti: str, expires_at: float) -> None:
    async with _jti_deny_lock:
        if len(_jti_deny_cache) >= _JTI_CACHE_MAX:
            now = time.time()
            expired = [k for k, v in _jti_deny_cache.items() if now > v]
            for k in expired:
                del _jti_deny_cache[k]
        _jti_deny_cache[jti] = expires_at


def _normalize_resource_url(url: str) -> str:
    """Normalize a resource URL per RFC 8707 for comparison."""
    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.params,
            parsed.query,
            "",
        )
    )


def _oauth_issuer(config: AppConfig) -> str:
    return config.server.public_base_url.rstrip("/")


def _oauth_resource(config: AppConfig) -> str:
    return f"{_oauth_issuer(config)}/mcp"


def _validate_redirect_uri(redirect_uri: str) -> None:
    try:
        parsed = urlparse(redirect_uri)
        hostname = parsed.hostname
    except ValueError as exc:
        raise RegistrationError(
            error="invalid_redirect_uri",
            error_description="Redirect URI is invalid",
        ) from exc
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or "*" in redirect_uri
    ):
        raise RegistrationError(
            error="invalid_redirect_uri",
            error_description="Redirect URI must be an exact URI without credentials or a fragment",
        )
    is_loopback = hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and is_loopback:
        return
    raise RegistrationError(
        error="invalid_redirect_uri",
        error_description="Redirect URI must use HTTPS, except for loopback HTTP clients",
    )


def validate_redirect_uri(redirect_uri: str) -> None:
    _validate_redirect_uri(redirect_uri)


def _validate_client_metadata(
    client_info: OAuthClientInformationFull,
    redirect_uris: list[str] | None = None,
) -> None:
    parsed_redirect_uris = [
        str(uri) for uri in (client_info.redirect_uris or [])
    ]
    if redirect_uris is None:
        redirect_uris = parsed_redirect_uris
    grant_types = set(client_info.grant_types or [])
    token_exchange_only = grant_types == {TOKEN_EXCHANGE_GRANT_TYPE}
    if (
        TOKEN_EXCHANGE_GRANT_TYPE in grant_types
        and not (client_info.scope or "").strip()
    ):
        raise RegistrationError(
            error="invalid_client_metadata",
            error_description=(
                "Clients using Token Exchange must register at least one scope"
            ),
        )
    if not token_exchange_only and (
        not redirect_uris or len(redirect_uris) > 10
    ):
        raise RegistrationError(
            error="invalid_redirect_uri",
            error_description="One to ten redirect URIs are required",
        )
    if len(redirect_uris) != len(parsed_redirect_uris):
        raise RegistrationError(
            error="invalid_redirect_uri",
            error_description="Redirect URI metadata is inconsistent",
        )
    for redirect_uri, parsed_redirect_uri in zip(
        redirect_uris, parsed_redirect_uris, strict=True
    ):
        _validate_redirect_uri(redirect_uri)
        if str(AnyUrl(redirect_uri)) != parsed_redirect_uri:
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="Redirect URI metadata is inconsistent",
            )
    browser_grants = {
        "authorization_code",
        "refresh_token",
    }
    allowed_browser_grants = browser_grants | {
        TOKEN_EXCHANGE_GRANT_TYPE
    }
    if grant_types not in (
        {TOKEN_EXCHANGE_GRANT_TYPE},
        browser_grants,
        allowed_browser_grants,
    ):
        raise RegistrationError(
            error="invalid_client_metadata",
            error_description=(
                "Clients must register authorization_code with refresh_token, "
                "Token Exchange only, or all three grants"
            ),
        )
    response_types = set(client_info.response_types or [])
    if (
        token_exchange_only
        and response_types not in (set(), {"code"})
    ) or (
        not token_exchange_only
        and response_types != {"code"}
    ):
        raise RegistrationError(
            error="invalid_client_metadata",
            error_description=(
                "Token Exchange-only clients do not use a response type; "
                "browser clients must use code"
            ),
        )
    if client_info.token_endpoint_auth_method not in {
        "none",
        "client_secret_basic",
        "client_secret_post",
    }:
        raise RegistrationError(
            error="invalid_client_metadata",
            error_description="Unsupported token endpoint authentication method",
        )


def _access_token_payload(
    config: AppConfig,
    *,
    subject: str,
    resource: str | None,
    client_id: str,
    scopes: list[str],
    expires_in_seconds: int | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    expires_in = (
        expires_in_seconds
        if expires_in_seconds is not None
        else config.auth.jwt.access_token_expire_minutes * 60
    )
    payload = {
        "iss": _oauth_issuer(config),
        "sub": subject,
        "aud": resource or _oauth_resource(config),
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + expires_in,
        "type": "access",
        "client_id": client_id,
        "scope": " ".join(scopes),
    }
    if resource == _oauth_resource(config) + "/personal":
        payload["access_mode"] = "personal"
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


def _decode_access_token(config: AppConfig, token: str) -> dict[str, Any]:
    payload = jwt.decode(
        token,
        get_public_key(),
        algorithms=["RS256"],
        # Decode both issued resource audiences; endpoint guards still enforce
        # resource, scope, and client binding before authorization.
        audience=[
            _oauth_resource(config),
            _oauth_resource(config) + "/personal",
            f"{config.server.public_base_url.rstrip('/')}/api/v1",
        ],
        issuer=_oauth_issuer(config),
        options={
            "require": ["iss", "aud", "sub", "iat", "exp", "jti", "type"],
        },
    )
    subject = payload["sub"]
    jti = payload["jti"]
    if (
        payload["type"] != "access"
        or not isinstance(subject, str)
        or not subject
        or not isinstance(jti, str)
        or not jti
    ):
        raise jwt.InvalidTokenError("Invalid access token claims")
    principal = parse_subject(subject)
    if principal.kind != PrincipalKind.USER:
        raise jwt.InvalidTokenError("OAuth access token subject must be a User")
    return payload


class PASAuthProvider:
    """MCP OAuth authorization server provider backed by PolarDB/SQLite."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        config: AppConfig,
    ):
        self._session_factory = session_factory
        self._config = config

    # ─── Client Registration & Lookup ─────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        """Look up a registered OAuth client by ``client_id``.

        Returns the full client information (with decrypted secret) or
        ``None`` if the client is unknown or its secret cannot be decrypted.
        """
        async with self._session_factory() as session:
            result = await session.execute(
                select(OAuthRegisteredClient).where(OAuthRegisteredClient.client_id == client_id)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None

            secret = None
            if row.client_secret_enc:
                try:
                    secret = decrypt(row.client_secret_enc)
                except Exception:
                    logger.warning("Failed to decrypt client secret for %s", client_id)
                    return None

            grant_types = (
                json.loads(row.grant_types)
                if row.grant_types
                else ["authorization_code", "refresh_token"]
            )
            redirect_uris = (
                json.loads(row.redirect_uris)
                if row.redirect_uris
                else []
            )
            if (
                set(grant_types) == {TOKEN_EXCHANGE_GRANT_TYPE}
                and not redirect_uris
            ):
                redirect_uris = None

            return OAuthClientInformationFull(
                client_id=row.client_id,
                client_secret=secret,
                client_id_issued_at=row.client_id_issued_at,
                client_secret_expires_at=row.client_secret_expires_at,
                redirect_uris=redirect_uris,
                grant_types=grant_types,
                response_types=(json.loads(row.response_types) if row.response_types else ["code"]),
                token_endpoint_auth_method=cast(Any, row.token_endpoint_auth_method),
                scope=row.scope,
                client_name=row.client_name,
            )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Persist a newly registered OAuth client (DCR endpoint).

        The client secret, if present, is encrypted at rest.
        """
        if client_info.client_id == personal_token_service.CLIENT_ID:
            raise RegistrationError(error="invalid_client_metadata",
                                    error_description="Reserved internal client identifier")
        raw_redirect_uris = _raw_registration_redirect_uris.get()
        redirect_uris = (
            list(raw_redirect_uris)
            if raw_redirect_uris is not None
            else [str(uri) for uri in (client_info.redirect_uris or [])]
        )
        _validate_client_metadata(client_info, redirect_uris)
        async with self._session_factory() as session:
            secret_enc = None
            if client_info.client_secret:
                secret_enc = encrypt(client_info.client_secret)

            row = OAuthRegisteredClient(
                client_id=client_info.client_id,
                client_secret_enc=secret_enc,
                client_id_issued_at=client_info.client_id_issued_at,
                client_secret_expires_at=client_info.client_secret_expires_at,
                redirect_uris=json.dumps(redirect_uris),
                grant_types=json.dumps(client_info.grant_types),
                response_types=json.dumps(client_info.response_types),
                token_endpoint_auth_method=client_info.token_endpoint_auth_method,
                scope=client_info.scope,
                client_name=client_info.client_name,
            )
            session.add(row)
            await session.commit()

    # ─── Authorization Flow ───────────────────────────────────────────

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        """Begin an authorization request and return the next-step URL.

        Validates the requested ``resource`` (RFC 8707), persists a pending
        authorization session, and returns either the local login URL
        (builtin mode) or the SSO redirect URL (OIDC mode).
        """
        raw_redirect_uri = _raw_authorize_redirect_uri.get()
        redirect_uri = raw_redirect_uri or str(params.redirect_uri)
        registered_redirect_uris = {
            str(uri) for uri in (client.redirect_uris or [])
        }
        if raw_redirect_uri is not None:
            async with self._session_factory() as session:
                stored_redirect_uris = await session.scalar(
                    select(OAuthRegisteredClient.redirect_uris).where(
                        OAuthRegisteredClient.client_id == client.client_id
                    )
                )
            registered_redirect_uris = set(
                json.loads(stored_redirect_uris)
                if stored_redirect_uris
                else []
            )
        if not any(
            _redirect_uri_matches(registered_uri, redirect_uri)
            for registered_uri in registered_redirect_uris
        ):
            raise AuthorizeError(
                error="invalid_request",
                error_description="Redirect URI is not registered for this client",
            )
        try:
            _validate_redirect_uri(redirect_uri)
        except RegistrationError as exc:
            raise AuthorizeError(
                error="invalid_request",
                error_description=exc.error_description,
            ) from exc

        resource_url = _oauth_resource(self._config)

        if params.resource is not None:
            requested_resource = _normalize_resource_url(str(params.resource))
            if requested_resource == _normalize_resource_url(resource_url + "/personal"):
                resource_url += "/personal"
            if requested_resource != _normalize_resource_url(resource_url):
                raise AuthorizeError(
                    error="invalid_request",
                    error_description=f"Invalid resource. Expected: {resource_url}",
                )
        else:
            logger.warning(
                "authorize() called without resource parameter; "
                "using the configured MCP resource"
            )

        # Store the effective resource (always non-null)
        effective_resource = resource_url

        # Generate idp_state upfront (used by OIDC mode for callback correlation)
        idp_state = str(uuid.uuid4())
        idp_nonce = secrets.token_urlsafe(32)

        async with self._session_factory() as session:
            pending = OAuthPendingAuth(
                client_id=client.client_id,
                redirect_uri=redirect_uri,
                code_challenge=params.code_challenge,
                code_challenge_method="S256",
                resource=effective_resource,
                scopes=json.dumps(params.scopes or []),
                state=params.state,
                idp_state=idp_state,
                idp_nonce=idp_nonce,
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            )
            session.add(pending)
            await session.commit()
            await session.refresh(pending)
            session_id = pending.session_id

        if self._config.auth.mode == "builtin":
            return f"/mcp-auth/login?session_id={session_id}"

        # OIDC mode: redirect to external Identity Provider
        from server.auth.identity_federation import IdentityFederation

        federation = IdentityFederation(
            self._config.auth.oidc,
            provider_name=self._config.auth.oidc.provider_name,
        )
        await federation.discover_endpoints()

        callback_url = (
            self._config.auth.oidc.redirect_uri or f"{self._config.server.public_base_url}/auth/oidc/callback"
        )
        authorize_url, idp_code_verifier = federation.build_authorize_url(
            redirect_uri=callback_url,
            state=idp_state,
            nonce=(
                idp_nonce
                if self._config.auth.oidc.protocol_mode == "oidc"
                else None
            ),
        )

        # Store authorize_url (and code_verifier if IdP PKCE is enabled)
        async with self._session_factory() as session:
            from sqlalchemy import update as sa_update

            values: dict[str, str | None] = {
                "idp_authorize_url": authorize_url,
            }
            if idp_code_verifier:
                values["idp_code_verifier_enc"] = encrypt(idp_code_verifier)
            await session.execute(
                sa_update(OAuthPendingAuth).where(OAuthPendingAuth.session_id == session_id).values(**values)
            )
            await session.commit()

        return f"/mcp-auth/sso-redirect?session_id={session_id}"

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        """Resolve an opaque authorization code to its stored grant.

        Detects replay (already-consumed codes) and proactively revokes
        any refresh tokens minted from the same code.
        """
        code_hash = hashlib.sha256(authorization_code.encode()).hexdigest()

        async with self._session_factory() as session:
            result = await session.execute(
                select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code_hash == code_hash)
            )
            row = result.scalar_one_or_none()

            if row is None:
                return None

            if row.consumed_at is not None:
                # Replay attack detected — revoke all refresh tokens tied to this code
                logger.warning(
                    "Replay attack detected: authorization code %s already consumed",
                    code_hash[:8],
                )
                await session.execute(
                    update(OAuthRefreshToken)
                    .where(OAuthRefreshToken.code_id == code_hash)
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                await session.commit()
                return None

            # SQLite strips tzinfo from stored datetimes.  When read back
            # the naive datetime must be treated as UTC, otherwise
            # .timestamp() assumes local time and the expiry is wrong.
            expires_dt = row.expires_at
            if expires_dt.tzinfo is None:
                expires_dt = expires_dt.replace(tzinfo=timezone.utc)

            return AuthorizationCode(
                code=authorization_code,
                scopes=json.loads(row.scopes),
                expires_at=expires_dt.timestamp(),
                client_id=row.client_id,
                code_challenge=row.code_challenge,
                redirect_uri=AnyUrl(row.redirect_uri),
                redirect_uri_provided_explicitly=row.redirect_uri_provided_explicitly,
                resource=row.resource,
                subject=user_subject(row.user_id),
            )

    # ─── Token Issuance & Refresh ─────────────────────────────────────

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        """Exchange an authorization code for an access + refresh token pair.

        Locks the user and code before consuming it to prevent replay; on
        replay, revokes the entire refresh-token family bound to the code.
        """
        code_hash = hashlib.sha256(authorization_code.code.encode()).hexdigest()
        config = self._config
        subject = authorization_code.subject
        if not isinstance(subject, str):
            raise ValueError("Authorization code subject must be a User")
        principal = parse_subject(subject)
        if principal.kind != PrincipalKind.USER:
            raise ValueError("Authorization code subject must be a User")

        # Lock the user before the code so password mutation, login, and token
        # exchange share one ordering. Issuance and consumption commit together.
        async with self._session_factory() as session:
            user = await session.scalar(select(User).where(User.id == principal.id).with_for_update())
            if user is None or user.status == UserStatus.DISABLED:
                raise ValueError("Authorization code user is unavailable")

            row = await session.scalar(
                select(OAuthAuthorizationCode)
                .where(
                    OAuthAuthorizationCode.code_hash == code_hash,
                )
                .with_for_update()
            )
            if row is None or row.user_id != user.id or row.client_id != authorization_code.client_id:
                raise ValueError("Authorization code is unavailable")
            if row.consumed_at is not None:
                logger.warning(
                    "Authorization code replay detected during exchange: %s",
                    code_hash[:8],
                )
                await session.execute(
                    update(OAuthRefreshToken)
                    .where(OAuthRefreshToken.code_id == code_hash)
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                await session.commit()
                raise ValueError("Authorization code already consumed")
            row.consumed_at = datetime.now(timezone.utc)

            private_key, _ = _load_keys()
            access_payload = _access_token_payload(
                config,
                subject=subject,
                resource=authorization_code.resource,
                client_id=authorization_code.client_id,
                scopes=authorization_code.scopes,
            )
            access_payload.update(_builtin_credential_claim(user))
            access_token = jwt.encode(access_payload, private_key, algorithm="RS256")
            refresh_token = secrets.token_urlsafe(32)
            rt = OAuthRefreshToken(
                token_hash=hashlib.sha256(refresh_token.encode()).hexdigest(),
                client_id=authorization_code.client_id,
                user_id=principal.id,
                code_id=code_hash,
                token_family=str(uuid.uuid4()),
                scopes=json.dumps(authorization_code.scopes),
                resource=authorization_code.resource,
                expires_at=datetime.now(timezone.utc) + timedelta(days=config.auth.jwt.refresh_token_expire_days),
            )
            session.add(rt)
            await session.commit()

        return OAuthToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=config.auth.jwt.access_token_expire_minutes * 60,
            scope=" ".join(authorization_code.scopes),
        )

    async def exchange_external_token(
        self,
        client: OAuthClientInformationFull,
        subject_token: str,
        *,
        resource: str | None,
        scopes: list[str],
        agent_id: str | None = None,
        identity_source_id: str | None = None,
        feishu_user_id: str | None = None,
        feishu_union_id: str | None = None,
    ) -> OAuthToken:
        """Exchange a trusted external access token for a PAS access token."""
        managed_application = False
        async with self._session_factory() as policy_session:
            managed_policy = await resolve_managed_exchange_policy(
                policy_session,
                self._config,
                client_id=cast(str, client.client_id),
                resource=resource,
                scopes=scopes,
                agent_id=agent_id,
            )
        if managed_policy is not None:
            managed_application = True
            resource = managed_policy.resource
            scopes = list(managed_policy.scopes)
            agent_id = managed_policy.agent_id

        mcp_resource = _oauth_resource(self._config)
        api_resource = (
            f"{self._config.server.public_base_url.rstrip('/')}/api/v1"
        )
        expected_resource = resource or mcp_resource
        normalized_resource = _normalize_resource_url(expected_resource)
        allowed_resources = {
            _normalize_resource_url(mcp_resource): ("mcp", False),
            _normalize_resource_url(api_resource): ("polarrag", True),
        }
        resource_policy = allowed_resources.get(normalized_resource)
        if resource_policy is None:
            raise ValueError(
                f"Invalid resource. Expected: {mcp_resource} or {api_resource}"
            )
        required_scope, requires_agent = resource_policy
        expected_resource = (
            api_resource if requires_agent else mcp_resource
        )
        authenticator = ExternalTokenAuthenticator(
            self._session_factory,
            self._config,
        )
        identity_context = {
            "identity_source_id": identity_source_id,
            "feishu_user_id": feishu_user_id,
            "feishu_union_id": feishu_union_id,
        }
        identity = (
            await authenticator.validate(subject_token, **identity_context)
            if any(identity_context.values())
            else await authenticator.validate(subject_token)
        )
        client_scopes = set((client.scope or "").split())
        requested_scopes = list(dict.fromkeys(scopes or [required_scope]))
        if required_scope not in requested_scopes:
            raise ValueError(
                f"Requested scope must include '{required_scope}'"
            )
        if requested_scopes and client_scopes and not set(
            requested_scopes
        ).issubset(client_scopes):
            raise ValueError("Requested scope exceeds the client grant")
        if (
            identity.scope_authoritative
            and identity.scopes
            and requested_scopes
            and not set(requested_scopes).issubset(identity.scopes)
        ):
            raise ValueError(
                "Requested scope exceeds the external token grant"
            )
        effective_scopes = requested_scopes
        now = datetime.now(timezone.utc)
        expires_in = (
            self._config.auth.external_token_trust.access_token_ttl_seconds
        )
        external_expires = identity.expires_at
        if external_expires is not None:
            if external_expires.tzinfo is None:
                external_expires = external_expires.replace(
                    tzinfo=timezone.utc
                )
            remaining = int((external_expires - now).total_seconds())
            if remaining <= 0:
                raise ExternalTokenInvalid(
                    "External access token is expired"
                )
            expires_in = min(expires_in, remaining)

        async with self._session_factory() as session:
            user = await authenticator.resolve_user(session, identity)
            if user.status == UserStatus.DISABLED:
                raise ExternalTokenInvalid("PAS user is disabled")
            selected_agent = None
            if requires_agent or agent_id:
                selected_agent = await resolve_authorized_agent(
                    session,
                    user_id=user.id,
                    requested_agent_id=agent_id,
                )
                if selected_agent is None:
                    raise ValueError(
                        "Requested Agent is unavailable or no default Agent is selected"
                    )
            private_key, _ = _load_keys()
            access_payload = _access_token_payload(
                self._config,
                subject=user_subject(user.id),
                resource=expected_resource,
                client_id=cast(str, client.client_id),
                scopes=effective_scopes,
                expires_in_seconds=expires_in,
                agent_id=selected_agent.id if selected_agent else None,
            )
            access_payload.update(_builtin_credential_claim(user))
            access_token = jwt.encode(
                access_payload,
                private_key,
                algorithm="RS256",
            )
            if managed_application:
                application = await session.get(
                    OAuthExternalApplication,
                    cast(str, client.client_id),
                )
                if application is None:
                    raise ExternalApplicationUnauthorized(
                        "External application is unavailable"
                    )
                mark_external_application_used(application)
            await session.commit()
        return OAuthToken(
            access_token=access_token,
            expires_in=expires_in,
            scope=" ".join(effective_scopes),
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        """Resolve a refresh token string to its stored grant.

        Detects token reuse (already-revoked tokens) and revokes the entire
        token family on suspicion of compromise.
        """
        if refresh_token.startswith(personal_token_service.TOKEN_PREFIX):
            return None
        token_hash = hashlib.sha256(refresh_token.encode()).hexdigest()

        async with self._session_factory() as session:
            result = await session.execute(select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == token_hash))
            row = result.scalar_one_or_none()

            if row is None:
                return None

            if row.revoked_at is not None:
                # Reuse detection: revoke ALL tokens in the same family
                logger.warning(
                    "Refresh token reuse detected, revoking token family %s",
                    row.token_family,
                )
                await session.execute(
                    update(OAuthRefreshToken)
                    .where(
                        OAuthRefreshToken.token_family == row.token_family,
                        OAuthRefreshToken.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                await session.execute(
                    update(ExternalTokenSession)
                    .where(
                        ExternalTokenSession.token_family
                        == row.token_family,
                        ExternalTokenSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                await session.commit()
                return None

            # SQLite strips tzinfo — treat naive as UTC (same as auth codes)
            rt_expires = row.expires_at
            if rt_expires is not None and rt_expires.tzinfo is None:
                rt_expires = rt_expires.replace(tzinfo=timezone.utc)
            if rt_expires is not None and rt_expires <= datetime.now(timezone.utc):
                return None

            return RefreshToken(
                token=refresh_token,
                client_id=row.client_id,
                scopes=json.loads(row.scopes),
                expires_at=(int(rt_expires.timestamp()) if rt_expires else None),
                subject=user_subject(row.user_id),
            )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        """Rotate a refresh token and issue a fresh access token.

        Locks the user and old token before rotation; on reuse, revokes the
        whole token family. The new refresh token inherits the family id.
        """
        if (refresh_token.client_id == personal_token_service.CLIENT_ID
                or refresh_token.token.startswith(personal_token_service.TOKEN_PREFIX)):
            raise ValueError("Personal access credentials are not refresh tokens")
        old_hash = hashlib.sha256(refresh_token.token.encode()).hexdigest()
        config = self._config

        subject = refresh_token.subject
        if not isinstance(subject, str):
            raise ValueError("Refresh token subject must be a User")
        principal = parse_subject(subject)
        if principal.kind != PrincipalKind.USER:
            raise ValueError("Refresh token subject must be a User")

        external_identity = None
        external_session_id: str | None = None
        external_expires: datetime | None = None
        ciphertext: str | None = None
        async with self._session_factory() as session:
            preliminary = await session.scalar(
                select(OAuthRefreshToken).where(
                    OAuthRefreshToken.token_hash == old_hash
                )
            )
            if preliminary is not None:
                external_session = await session.scalar(
                    select(ExternalTokenSession).where(
                        ExternalTokenSession.token_family
                        == preliminary.token_family,
                        ExternalTokenSession.revoked_at.is_(None),
                    )
                )
                if external_session is not None:
                    external_session_id = external_session.id
                    external_expires = external_session.external_expires_at
                    ciphertext = external_session.subject_token_ciphertext
                    expected_provider_type = external_session.provider_type
                    expected_provider_key = external_session.provider_key
                    expected_fingerprint = (
                        external_session.provider_fingerprint
                    )
                    expected_external_subject = (
                        external_session.external_subject
                    )
                    expected_user_id = external_session.user_id
                    token_family = external_session.token_family
                else:
                    ciphertext = None
        if ciphertext is not None:
            authenticator = ExternalTokenAuthenticator(
                self._session_factory,
                self._config,
            )
            try:
                external_identity = await authenticator.validate(
                    decrypt(ciphertext)
                )
                if (
                    external_identity.provider_type
                    != expected_provider_type
                    or external_identity.provider_key
                    != expected_provider_key
                    or external_identity.provider_fingerprint
                    != expected_fingerprint
                    or external_identity.subject
                    != expected_external_subject
                    or principal.id != expected_user_id
                ):
                    raise ExternalTokenInvalid(
                        "External token trust or identity changed"
                    )
            except ExternalTokenUnavailable:
                raise
            except (ExternalTokenInvalid, ValueError):
                await self._revoke_token_family(token_family)
                raise ValueError(
                    "External access token is no longer valid"
                ) from None
        effective_scopes = scopes if scopes else refresh_token.scopes
        if (
            external_identity is not None
            and external_identity.scopes
            and not set(effective_scopes).issubset(
                external_identity.scopes
            )
        ):
            await self._revoke_token_family(token_family)
            raise ValueError(
                "Refresh token scope exceeds the external token grant"
            )
        external_access_ttl = (
            self._external_access_token_ttl(
                external_identity.expires_at
            )
            if external_identity is not None
            else None
        )

        # Lock user first and rotate the old/new refresh rows in one transaction.
        async with self._session_factory() as session:
            user = await session.scalar(select(User).where(User.id == principal.id).with_for_update())
            if user is None or user.status == UserStatus.DISABLED:
                raise ValueError("Refresh token user is unavailable")

            old_row = await session.scalar(
                select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == old_hash).with_for_update()
            )
            if old_row is None or old_row.user_id != user.id:
                raise ValueError("Refresh token not found")
            old_expires_at = old_row.expires_at
            if old_expires_at is not None and old_expires_at.tzinfo is None:
                old_expires_at = old_expires_at.replace(tzinfo=timezone.utc)
            if old_expires_at is not None and old_expires_at <= datetime.now(timezone.utc):
                raise ValueError("Refresh token expired")
            if old_row.revoked_at is not None:
                logger.warning(
                    "Refresh token reuse detected during exchange: %s",
                    old_hash[:8],
                )
                await session.execute(
                    update(OAuthRefreshToken)
                    .where(
                        OAuthRefreshToken.token_family == old_row.token_family,
                        OAuthRefreshToken.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                await session.execute(
                    update(ExternalTokenSession)
                    .where(
                        ExternalTokenSession.token_family
                        == old_row.token_family,
                        ExternalTokenSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=datetime.now(timezone.utc))
                )
                await session.commit()
                raise ValueError("Refresh token already consumed")

            old_row.revoked_at = datetime.now(timezone.utc)
            token_family = old_row.token_family
            code_id = old_row.code_id
            resource = old_row.resource
            new_refresh_token = secrets.token_urlsafe(32)
            rt = OAuthRefreshToken(
                token_hash=hashlib.sha256(new_refresh_token.encode()).hexdigest(),
                client_id=refresh_token.client_id,
                user_id=user.id,
                code_id=code_id,
                token_family=token_family,
                scopes=json.dumps(scopes if scopes else refresh_token.scopes),
                resource=resource,
                expires_at=datetime.now(timezone.utc) + timedelta(days=config.auth.jwt.refresh_token_expire_days),
            )
            session.add(rt)
            private_key, _ = _load_keys()
            access_payload = _access_token_payload(
                config,
                subject=cast(str, refresh_token.subject),
                resource=resource,
                client_id=refresh_token.client_id,
                scopes=effective_scopes,
                expires_in_seconds=external_access_ttl,
            )
            access_payload.update(_builtin_credential_claim(user))
            access_token = jwt.encode(access_payload, private_key, algorithm="RS256")
            if external_session_id is not None:
                current_external_session = await session.get(
                    ExternalTokenSession,
                    external_session_id,
                )
                if (
                    current_external_session is None
                    or current_external_session.revoked_at is not None
                    or current_external_session.token_family != token_family
                ):
                    raise ValueError(
                        "External token session is unavailable"
                    )
                current_external_session.last_validated_at = datetime.now(
                    timezone.utc
                )
                current_external_session.external_expires_at = (
                    external_identity.expires_at
                    if external_identity is not None
                    else external_expires
                )
            await session.commit()
        expires_in = (
            external_access_ttl
            if external_access_ttl is not None
            else config.auth.jwt.access_token_expire_minutes * 60
        )
        return OAuthToken(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=expires_in,
            scope=" ".join(effective_scopes),
        )

    def _external_access_token_ttl(
        self,
        external_expires_at: datetime | None,
    ) -> int:
        ttl = (
            self._config.auth.external_token_trust.access_token_ttl_seconds
        )
        if external_expires_at is None:
            return ttl
        if external_expires_at.tzinfo is None:
            external_expires_at = external_expires_at.replace(
                tzinfo=timezone.utc
            )
        remaining = int(
            (
                external_expires_at - datetime.now(timezone.utc)
            ).total_seconds()
        )
        if remaining <= 0:
            raise ValueError("External access token is expired")
        return min(ttl, remaining)

    async def _revoke_token_family(self, token_family: str) -> None:
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            await session.execute(
                update(OAuthRefreshToken)
                .where(
                    OAuthRefreshToken.token_family == token_family,
                    OAuthRefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            await session.execute(
                update(ExternalTokenSession)
                .where(
                    ExternalTokenSession.token_family == token_family,
                    ExternalTokenSession.revoked_at.is_(None),
                )
                .values(revoked_at=now)
            )
            await session.commit()

    # ─── Token Validation & Revocation ────────────────────────────────

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = await self._load_verified_access_token(token)
        mode = request_mcp_mode.get()
        if access is not None and mode is not None:
            if is_personal_access(access) != (mode == "personal"):
                return None
        return access

    async def _load_verified_access_token(self, token: str) -> AccessToken | None:
        """Verify a bearer access token and return its decoded grant.

        Agent tokens are opaque credentials resolved by hash. Other tokens
        follow the existing JWT signature, audience, and JTI deny-list path.
        Returns ``None`` for any validation failure.
        """
        if token.startswith(personal_token_service.TOKEN_PREFIX):
            async with self._session_factory() as session:
                resolved = await personal_token_service.resolve(session, token)
                if resolved is None:
                    return None
                user, row = resolved
                if row.resource != _oauth_resource(self._config) + "/personal":
                    return None
                return AccessToken(token=token, client_id=personal_token_service.CLIENT_ID,
                    scopes=[], subject=user_subject(user.id), resource=row.resource,
                    expires_at=int(personal_token_service.normalized(row.expires_at).timestamp()),
                    claims={"access_mode": "personal"})
        if token.startswith(agent_user_token_service.TOKEN_PREFIX):
            return await self._load_agent_user_access_token(token)
        if token.startswith("pas_agent_"):
            return await self._load_agent_access_token(token)

        try:
            payload = _decode_access_token(self._config, token)
        except (PyJWTError, ValueError):
            try:
                unverified = jwt.decode(
                    token,
                    options={
                        "verify_signature": False,
                        "verify_exp": False,
                        "verify_aud": False,
                    },
                )
            except (PyJWTError, ValueError):
                unverified = {}
            if unverified.get("iss") == _oauth_issuer(self._config):
                return None
            return await self._load_direct_external_access_token(token)

        jti = cast(str, payload["jti"])
        cached = await _jti_is_denied_cached(jti)
        if cached is True:
            return None
        if cached is None:
            async with self._session_factory() as session:
                result = await session.execute(
                    select(OAuthDeniedJTI).where(OAuthDeniedJTI.jti == jti)
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    await _jti_cache_deny(jti, row.expires_at.timestamp())
                    return None

        subject = payload.get("sub")
        if not isinstance(subject, str):
            return None
        try:
            principal = parse_subject(subject)
        except InvalidPrincipalSubject:
            return None
        if principal.kind != PrincipalKind.USER:
            return None
        async with self._session_factory() as session:
            user = await session.get(User, principal.id)
        if user is None or user.status == UserStatus.DISABLED:
            return None
        token_credential_epoch = payload.get("credential_epoch")
        if user.auth_provider == AuthProvider.BUILTIN and (
            not isinstance(token_credential_epoch, int)
            or isinstance(token_credential_epoch, bool)
            or token_credential_epoch != user.credential_epoch
        ):
            return None


        return AccessToken(
            token=token,
            client_id=payload.get("client_id", ""),
            scopes=payload.get("scope", "").split(),
            expires_at=payload.get("exp"),
            resource=payload.get("aud"),
            subject=payload.get("sub"),
            claims=payload,
        )

    async def _load_direct_external_access_token(
        self,
        token: str,
    ) -> AccessToken | None:
        authenticator = ExternalTokenAuthenticator(
            self._session_factory,
            self._config,
        )
        if not authenticator.direct_mcp_enabled:
            return None
        try:
            identity = await authenticator.validate(token)
            async with self._session_factory() as session:
                user = await authenticator.resolve_user(session, identity)
                if user.status == UserStatus.DISABLED:
                    return None
                await session.commit()
        except (ExternalTokenInvalid, ExternalTokenUnavailable, ValueError):
            return None
        expires_at = identity.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return AccessToken(
            token=token,
            client_id=f"external:{identity.provider_type}",
            scopes=list(identity.scopes),
            expires_at=(
                int(expires_at.timestamp()) if expires_at else None
            ),
            resource=_oauth_resource(self._config),
            subject=user_subject(user.id),
        )

    async def _load_agent_access_token(self, token: str) -> AccessToken | None:
        token_hash = hash_agent_token(token)
        async with self._session_factory() as session:
            result = await session.execute(
                select(AgentAPIToken, Agent)
                .join(Agent, Agent.id == AgentAPIToken.agent_id)
                .where(AgentAPIToken.token_hash == token_hash)
            )
            match = result.one_or_none()
            if match is None:
                return None
            row, agent = match
            if agent.status != AgentStatus.ACTIVE or row.revoked_at is not None:
                return None

            now = datetime.now(timezone.utc)
            expires_at = row.expires_at
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= now:
                    return None

            token_id = row.id
            access_token = AccessToken(
                token=token,
                client_id="agent-token",
                scopes=[],
                expires_at=int(expires_at.timestamp()) if expires_at else None,
                subject=agent_subject(row.agent_id),
            )
        _schedule_agent_token_use(self._session_factory, token_id, now)
        return access_token

    async def _load_agent_user_access_token(
        self, token: str
    ) -> AccessToken | None:
        async with self._session_factory() as session:
            context = await agent_user_token_service.resolve_token(
                session, token
            )
            if context is None:
                return None
            row = context.token
            expires_at = row.expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            access_token = AccessToken(
                token=token,
                client_id=f"agent-user:{context.assignment.id}",
                scopes=[],
                expires_at=(
                    int(expires_at.timestamp()) if expires_at else None
                ),
                subject=user_subject(context.user.id),
            )
            token_id = row.id
        _schedule_agent_user_token_use(self._session_factory, token_id, now)
        return access_token

    async def revoke_token(
        self, token: AccessToken | RefreshToken
    ) -> None:
        """Revoke an access or refresh token (RFC 7009).

        Access tokens are denied via JTI (cache + DB) until their natural
        expiry. Refresh tokens revoke the entire token family.
        """
        if isinstance(token, AccessToken):
            # Decode JWT to get jti and exp
            try:
                payload = jwt.decode(
                    token.token,
                    get_public_key(),
                    algorithms=["RS256"],
                    options={"verify_aud": False},
                )
            except PyJWTError:
                return
            jti = payload.get("jti")
            exp = payload.get("exp")
            if jti and exp:
                await _jti_cache_deny(jti, float(exp))
                async with self._session_factory() as session:
                    denied = OAuthDeniedJTI(
                        jti=jti,
                        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
                    )
                    try:
                        async with session.begin_nested():
                            session.add(denied)
                            await session.flush()
                    except IntegrityError:
                        pass
                    await session.commit()
        elif isinstance(token, RefreshToken):
            token_hash = hashlib.sha256(token.token.encode()).hexdigest()
            async with self._session_factory() as session:
                result = await session.execute(
                    select(OAuthRefreshToken).where(OAuthRefreshToken.token_hash == token_hash)
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    # Revoke all tokens in the same family
                    await session.execute(
                        update(OAuthRefreshToken)
                        .where(
                            OAuthRefreshToken.token_family == row.token_family,
                            OAuthRefreshToken.revoked_at.is_(None),
                        )
                        .values(revoked_at=datetime.now(timezone.utc))
                    )
                    await session.execute(
                        update(ExternalTokenSession)
                        .where(
                            ExternalTokenSession.token_family
                            == row.token_family,
                            ExternalTokenSession.revoked_at.is_(None),
                        )
                        .values(revoked_at=datetime.now(timezone.utc))
                    )
                    await session.commit()
