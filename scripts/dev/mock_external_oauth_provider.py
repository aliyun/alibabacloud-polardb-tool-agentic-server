from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response


ACTIVE_ASSERTIONS = frozenset(
    {
        "mock-valid",
        "mock-subject-mismatch",
        "mock-source-mismatch",
        "mock-userinfo-not-found",
        "mock-userinfo-error",
        "mock-wrong-audience",
    }
)


@dataclass(frozen=True)
class MockProviderConfig:
    client_id: str
    client_secret: str
    pas_source_id: str
    user_id: str
    union_id: str | None = None
    audience: str = "polarrag"
    issuer: str = "https://mock-external-oauth.example"
    login_client_id: str = "pas-console"
    login_client_secret: str = "local-console-secret"
    login_subject: str = "mock-admin"
    login_display_name: str = "Mock Administrator"
    login_email: str = "mock-admin@example.com"


@dataclass(frozen=True)
class AuthorizationCode:
    redirect_uri: str
    code_challenge: str | None
    code_challenge_method: str | None


def _pkce_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _redirect_with_query(
    redirect_uri: str,
    values: dict[str, str],
) -> RedirectResponse:
    parsed = urlsplit(redirect_uri)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(values.items())
    location = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )
    return RedirectResponse(location, status_code=303)


def _oauth_error(
    status_code: int,
    error: str,
    *,
    authenticate: str | None = None,
) -> JSONResponse:
    headers = {
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }
    if authenticate:
        headers["WWW-Authenticate"] = authenticate
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
        headers=headers,
    )


def _valid_basic_credentials(
    authorization: str | None,
    config: MockProviderConfig,
) -> bool:
    if not authorization or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(
            authorization.removeprefix("Basic "),
            validate=True,
        ).decode("utf-8")
        client_id, client_secret = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return secrets.compare_digest(client_id, config.client_id) and (
        secrets.compare_digest(client_secret, config.client_secret)
    )


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.removeprefix("Bearer ").strip()
    return token or None


def create_app(config: MockProviderConfig) -> FastAPI:
    authorization_codes: dict[str, AuthorizationCode] = {}
    login_access_tokens: set[str] = set()
    app = FastAPI(
        title="Mock external OAuth provider",
        description=(
            "Development-only OAuth login, RFC 7662 Introspection, and "
            "UserInfo-compatible identity lookup service."
        ),
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/oauth2/authorize")
    async def authorize(request: Request) -> Response:
        query = request.query_params
        redirect_uri = query.get("redirect_uri")
        state = query.get("state")
        if (
            query.get("response_type") != "code"
            or query.get("client_id") != config.login_client_id
            or not redirect_uri
            or not state
        ):
            return _oauth_error(400, "invalid_request")
        parsed_redirect = urlsplit(redirect_uri)
        if (
            parsed_redirect.scheme not in {"http", "https"}
            or not parsed_redirect.netloc
        ):
            return _oauth_error(400, "invalid_request")

        code_challenge = query.get("code_challenge")
        code_challenge_method = query.get("code_challenge_method")
        if code_challenge_method and code_challenge_method != "S256":
            return _redirect_with_query(
                redirect_uri,
                {
                    "error": "invalid_request",
                    "state": state,
                },
            )
        if bool(code_challenge) != bool(code_challenge_method):
            return _redirect_with_query(
                redirect_uri,
                {
                    "error": "invalid_request",
                    "state": state,
                },
            )

        code = secrets.token_urlsafe(32)
        authorization_codes[code] = AuthorizationCode(
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
        return _redirect_with_query(
            redirect_uri,
            {
                "code": code,
                "state": state,
            },
        )

    @app.post("/oauth2/token")
    async def login_token(request: Request) -> JSONResponse:
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith(
            "application/x-www-form-urlencoded"
        ):
            return _oauth_error(400, "invalid_request")
        form = await request.form()
        code = form.get("code")
        redirect_uri = form.get("redirect_uri")
        client_id = form.get("client_id")
        client_secret = form.get("client_secret")
        if (
            form.get("grant_type") != "authorization_code"
            or not isinstance(code, str)
            or not isinstance(redirect_uri, str)
            or not isinstance(client_id, str)
            or not isinstance(client_secret, str)
        ):
            return _oauth_error(400, "invalid_request")
        if not (
            secrets.compare_digest(client_id, config.login_client_id)
            and secrets.compare_digest(
                client_secret,
                config.login_client_secret,
            )
        ):
            return _oauth_error(401, "invalid_client")

        authorization_code = authorization_codes.get(code)
        if authorization_code is None:
            return _oauth_error(400, "invalid_grant")
        if not secrets.compare_digest(
            redirect_uri,
            authorization_code.redirect_uri,
        ):
            return _oauth_error(400, "invalid_grant")
        if authorization_code.code_challenge is not None:
            code_verifier = form.get("code_verifier")
            if not isinstance(code_verifier, str):
                return _oauth_error(400, "invalid_grant")
            try:
                actual_challenge = _pkce_challenge(code_verifier)
            except UnicodeEncodeError:
                return _oauth_error(400, "invalid_grant")
            if not secrets.compare_digest(
                actual_challenge,
                authorization_code.code_challenge,
            ):
                return _oauth_error(400, "invalid_grant")

        authorization_codes.pop(code)
        access_token = secrets.token_urlsafe(32)
        login_access_tokens.add(access_token)
        return JSONResponse(
            content={
                "access_token": access_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "profile email",
            },
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )

    @app.get("/oauth2/login/user_info")
    async def login_user_info(
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        access_token = _bearer_token(authorization)
        if access_token not in login_access_tokens:
            return _oauth_error(
                401,
                "invalid_token",
                authenticate='Bearer error="invalid_token"',
            )
        return JSONResponse(
            content={
                "sub": config.login_subject,
                "name": config.login_display_name,
                "email": config.login_email,
            },
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )

    @app.post("/oauth2/introspect")
    async def introspect(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if not _valid_basic_credentials(authorization, config):
            return _oauth_error(
                401,
                "invalid_client",
                authenticate='Basic realm="mock-oauth-introspection"',
            )
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith(
            "application/x-www-form-urlencoded"
        ):
            return _oauth_error(400, "invalid_request")
        form = await request.form()
        tokens = form.getlist("token")
        if len(tokens) != 1 or not isinstance(tokens[0], str) or not tokens[0]:
            return _oauth_error(400, "invalid_request")
        assertion = tokens[0]
        if assertion == "mock-provider-error":
            return _oauth_error(503, "temporarily_unavailable")
        if assertion not in ACTIVE_ASSERTIONS:
            return JSONResponse(
                content={"active": False},
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                },
            )
        audience = (
            "unexpected-audience"
            if assertion == "mock-wrong-audience"
            else config.audience
        )
        now = int(time.time())
        return JSONResponse(
            content={
                "active": True,
                "client_id": "mock-upstream-client",
                "sub": "mock-user-123",
                "scope": "external_identity",
                "token_type": "Bearer",
                "aud": audience,
                "iss": config.issuer,
                "iat": now,
                "exp": now + 3600,
                "jti": f"{assertion}-jti",
            },
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )

    @app.get("/oauth2/user_info")
    async def user_info(
        authorization: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        assertion = _bearer_token(authorization)
        if assertion not in ACTIVE_ASSERTIONS:
            return _oauth_error(
                401,
                "invalid_token",
                authenticate='Bearer error="invalid_token"',
            )
        if assertion == "mock-userinfo-error":
            return _oauth_error(503, "temporarily_unavailable")
        if assertion == "mock-userinfo-not-found":
            return _oauth_error(404, "identity_not_found")
        subject = (
            "different-mock-user"
            if assertion == "mock-subject-mismatch"
            else "mock-user-123"
        )
        source_id = (
            "different-source-id"
            if assertion == "mock-source-mismatch"
            else config.pas_source_id
        )
        content = {
            "sub": subject,
            "user_id": config.user_id,
            "pas_source_id": source_id,
        }
        if config.union_id:
            content["union_id"] = config.union_id
        return JSONResponse(
            content=content,
            headers={
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )

    return app


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a development-only external OAuth provider for PAS Token "
            "Exchange testing."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=19090)
    parser.add_argument("--client-id", default="pas-to-provider")
    parser.add_argument("--client-secret", default="local-development-secret")
    parser.add_argument("--pas-source-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--union-id")
    parser.add_argument("--audience", default="polarrag")
    parser.add_argument("--login-client-id", default="pas-console")
    parser.add_argument(
        "--login-client-secret",
        default="local-console-secret",
    )
    parser.add_argument("--login-subject", default="mock-admin")
    parser.add_argument(
        "--login-display-name",
        default="Mock Administrator",
    )
    parser.add_argument(
        "--login-email",
        default="mock-admin@example.com",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = MockProviderConfig(
        client_id=args.client_id,
        client_secret=args.client_secret,
        pas_source_id=args.pas_source_id,
        user_id=args.user_id,
        union_id=args.union_id,
        audience=args.audience,
        login_client_id=args.login_client_id,
        login_client_secret=args.login_client_secret,
        login_subject=args.login_subject,
        login_display_name=args.login_display_name,
        login_email=args.login_email,
    )
    print(
        "\n".join(
            (
                "Mock external OAuth provider",
                "  Browser OAuth 2.0 + UserInfo:",
                f"    Authorization: http://{args.host}:{args.port}/oauth2/authorize",
                f"    Token:         http://{args.host}:{args.port}/oauth2/token",
                f"    UserInfo:      http://{args.host}:{args.port}/oauth2/login/user_info",
                f"    Client ID:     {config.login_client_id}",
                "  External assertion validation:",
                f"    Introspection: http://{args.host}:{args.port}/oauth2/introspect",
                f"    UserInfo:      http://{args.host}:{args.port}/oauth2/user_info",
                f"    Client ID:     {config.client_id}",
                "  Assertions:",
                "    mock-valid",
                "    mock-inactive",
                "    mock-subject-mismatch",
                "    mock-source-mismatch",
                "    mock-userinfo-not-found",
                "    mock-provider-error",
                "    mock-userinfo-error",
                "    mock-wrong-audience",
            )
        ),
        flush=True,
    )
    uvicorn.run(
        create_app(config),
        host=args.host,
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
