"""Web SSO Guard — OIDC pre-authentication for the admin Web UI.

When enabled, all Web UI routes require a valid ``web_sso_guard`` JWT cookie.
Requests without the cookie are redirected to the OIDC authorization
endpoint. The guard is a "door check" — it verifies OIDC identity but does
not create or map application users.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import jwt
from jwt import PyJWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

logger = logging.getLogger(__name__)

GUARD_COOKIE = "web_sso_guard"

_EXCLUDED_PREFIXES = (
    "/mcp",
    "/livez",
    "/readyz",
    "/healthz/",
    "/.well-known/",
    "/mcp-auth/",
    "/auth/oidc/callback",
    "/auth/web-sso-guard/",
)


def _load_keys() -> tuple[str, str]:
    from server.auth.jwt_manager import _load_keys as jwt_load_keys
    return jwt_load_keys()


def _is_excluded(path: str, extra_excluded: list[str]) -> bool:
    for prefix in _EXCLUDED_PREFIXES:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    for prefix in extra_excluded:
        if path == prefix.rstrip("/") or path.startswith(prefix):
            return True
    return False


def _verify_guard_cookie(token: str) -> bool:
    try:
        _, public_key = _load_keys()
        payload = jwt.decode(token, public_key, algorithms=["RS256"])
        return payload.get("type") == "web_sso_guard"
    except PyJWTError:
        return False


def _build_state(original_path: str, code_verifier: str | None = None) -> str:
    state_data: dict[str, str] = {"path": original_path}
    if code_verifier:
        from server.core.crypto import encrypt
        state_data["cv"] = encrypt(code_verifier)
    return base64.urlsafe_b64encode(json.dumps(state_data).encode()).decode()


def decode_state(state: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(state + "==")
        result: dict[str, Any] = json.loads(raw)
        return result
    except Exception:
        return {"path": "/"}


def validate_redirect_path(path: str) -> str:
    if not path or not path.startswith("/") or path.startswith("//") or "://" in path:
        return "/"
    return path


class WebSSOGuardMiddleware(BaseHTTPMiddleware):

    def __init__(self, app: Any, config: dict[str, Any] | None = None):
        super().__init__(app)
        self._config = config or {}
        self._excluded_paths: list[str] = self._config.get("excluded_paths", [])

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        path = request.url.path

        if _is_excluded(path, self._excluded_paths):
            response: Response = await call_next(request)
            return response

        cookie = request.cookies.get(GUARD_COOKIE)
        if cookie and _verify_guard_cookie(cookie):
            response = await call_next(request)
            return response

        return await self._redirect_to_sso(request)

    async def _redirect_to_sso(self, request: Request) -> Response:
        from server.config import get_config
        from server.auth.identity_federation import IdentityFederation

        config = get_config()
        federation = IdentityFederation(
            config.auth.oidc,
            provider_name=config.auth.oidc.provider_name,
        )
        await federation.discover_endpoints()

        callback_url = f"{config.server.public_base_url}/auth/web-sso-guard/callback"
        original_path = str(request.url.path)
        if request.url.query:
            original_path += f"?{request.url.query}"

        placeholder_state = _build_state(original_path)
        authorize_url, code_verifier = federation.build_authorize_url(
            redirect_uri=callback_url,
            state=placeholder_state,
        )

        if code_verifier:
            final_state = _build_state(original_path, code_verifier)
            authorize_url, _ = federation.build_authorize_url(
                redirect_uri=callback_url,
                state=final_state,
                code_verifier=code_verifier,
            )

        return RedirectResponse(url=authorize_url, status_code=302)


async def handle_web_sso_guard_callback(request: Request) -> Response:
    """GET /auth/web-sso-guard/callback — handle OIDC redirect callback."""
    code = request.query_params.get("code", "")
    state_param = request.query_params.get("state", "")

    if not code or not state_param:
        return HTMLResponse("<h3>Missing code or state</h3>", status_code=400)

    state = decode_state(state_param)
    redirect_path = validate_redirect_path(state.get("path", "/"))

    from server.config import get_config
    from server.auth.identity_federation import IdentityFederation

    config = get_config()
    federation = IdentityFederation(
        config.auth.oidc,
        provider_name=config.auth.oidc.provider_name,
    )
    await federation.discover_endpoints()

    callback_url = f"{config.server.public_base_url}/auth/web-sso-guard/callback"

    code_verifier: str | None = None
    if state.get("cv"):
        try:
            from server.core.crypto import decrypt
            code_verifier = decrypt(state["cv"])
        except Exception:
            logger.warning("Failed to decrypt code_verifier from state")

    try:
        await federation.exchange_code(code, callback_url, code_verifier=code_verifier)
    except Exception:
        logger.exception("Web SSO guard code exchange failed")
        return HTMLResponse(
            "<h3>SSO authentication failed</h3><p>Please try again.</p>",
            status_code=401,
        )

    private_key, _ = _load_keys()
    ttl_hours = config.auth.web_sso_guard.session_ttl_hours
    now = int(time.time())
    guard_token = jwt.encode(
        {"type": "web_sso_guard", "iat": now, "exp": now + ttl_hours * 3600},
        private_key,
        algorithm="RS256",
    )

    response = RedirectResponse(url=redirect_path, status_code=302)
    response.set_cookie(
        key=GUARD_COOKIE,
        value=guard_token,
        max_age=ttl_hours * 3600,
        httponly=True,
        secure=not config.server.dev_mode,
        samesite="lax",
    )
    return response
