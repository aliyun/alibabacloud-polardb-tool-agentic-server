from __future__ import annotations

import hashlib
import html
import json
import logging
import secrets
from datetime import datetime, timezone, timedelta

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from sqlalchemy import select

from server.auth.builtin import authenticate_builtin
from server.db.engine import get_session_factory
from server.models.oauth import OAuthPendingAuth, OAuthAuthorizationCode

__all__ = [
    "handle_login_page", "handle_login_callback",
    "handle_sso_redirect", "handle_oidc_callback",
]

logger = logging.getLogger(__name__)


def _utc_now_comparable(dt: datetime) -> datetime:
    """Return a UTC now() that is comparable with *dt*.

    SQLite returns naive datetimes while other backends may return
    timezone-aware ones.  We match the awareness of *dt* so the ``<``
    comparison never raises ``TypeError``.
    """
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return now.replace(tzinfo=None)
    return now


async def handle_login_page(request: Request) -> Response:
    """GET /mcp-auth/login?session_id=... -- render builtin login form."""
    session_id = request.query_params.get("session_id", "")
    if not session_id:
        return HTMLResponse("<h3>Missing session_id</h3>", status_code=400)

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(OAuthPendingAuth).where(OAuthPendingAuth.session_id == session_id)
        )
        pending = result.scalar_one_or_none()

    if pending is None:
        return HTMLResponse("<h3>Invalid or expired session</h3>", status_code=404)

    if pending.expires_at < _utc_now_comparable(pending.expires_at):
        return HTMLResponse("<h3>Session expired</h3>", status_code=410)

    esc = html.escape
    form_html = f"""<!DOCTYPE html>
<html>
<head><title>alibabacloud polardb tool agentic server - Authorize</title>
<style>
body {{ font-family: sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }}
.card {{ background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); width: 320px; }}
h2 {{ margin-top: 0; }}
input {{ width: 100%; padding: 8px; margin: 4px 0 12px; box-sizing: border-box; border: 1px solid #ddd; border-radius: 4px; }}
button {{ width: 100%; padding: 10px; background: #1677ff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }}
button:hover {{ background: #0958d9; }}
</style>
</head>
<body>
<div class="card">
<h2>Sign In</h2>
<form method="POST" action="/mcp-auth/login/callback">
<input type="hidden" name="session_id" value="{esc(session_id)}">
<label>Username</label>
<input type="text" name="username" required autofocus>
<label>Password</label>
<input type="password" name="password" required>
<button type="submit">Authorize</button>
</form>
</div>
</body>
</html>"""
    return HTMLResponse(content=form_html)


async def handle_login_callback(request: Request) -> Response:
    """POST /mcp-auth/login/callback -- authenticate and issue authorization code."""
    form = await request.form()
    session_id = str(form.get("session_id", ""))
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))

    if not session_id or not username or not password:
        return HTMLResponse("<h3>Missing required fields</h3>", status_code=400)

    factory = get_session_factory()
    async with factory() as db_session:
        # Load pending auth session
        result = await db_session.execute(
            select(OAuthPendingAuth).where(OAuthPendingAuth.session_id == session_id)
        )
        pending = result.scalar_one_or_none()

        if pending is None:
            return HTMLResponse("<h3>Invalid or expired session</h3>", status_code=404)

        if pending.expires_at < _utc_now_comparable(pending.expires_at):
            return HTMLResponse("<h3>Session expired</h3>", status_code=410)

        # Authenticate user
        user = await authenticate_builtin(db_session, username, password)
        if user is None:
            retry_url = f"/mcp-auth/login?session_id={html.escape(str(session_id))}"
            return HTMLResponse(
                f'<html><body><h3>Invalid credentials</h3>'
                f'<a href="{retry_url}">Try again</a></body></html>',
                status_code=401,
            )

        # Generate authorization code
        code = secrets.token_urlsafe(32)
        code_hash = hashlib.sha256(code.encode()).hexdigest()

        redirect_uri = pending.redirect_uri
        state = pending.state

        code_record = OAuthAuthorizationCode(
            code_hash=code_hash,
            client_id=pending.client_id,
            user_id=user.id,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=True,
            code_challenge=pending.code_challenge,
            code_challenge_method=pending.code_challenge_method,
            resource=pending.resource,
            scopes=pending.scopes,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(code_record)

        await db_session.delete(pending)
        await db_session.commit()

    separator = "&" if "?" in redirect_uri else "?"
    redirect_url = f"{redirect_uri}{separator}code={code}"
    if state:
        redirect_url += f"&state={state}"
    return RedirectResponse(url=redirect_url, status_code=302)


async def handle_sso_redirect(request: Request) -> Response:
    """GET /mcp-auth/sso-redirect?session_id=... -- interstitial before IdP redirect."""
    session_id = request.query_params.get("session_id", "")
    if not session_id:
        return HTMLResponse("<h3>Missing session_id</h3>", status_code=400)

    factory = get_session_factory()
    async with factory() as session:
        result = await session.execute(
            select(OAuthPendingAuth).where(OAuthPendingAuth.session_id == session_id)
        )
        pending = result.scalar_one_or_none()

    if pending is None or not pending.idp_authorize_url:
        return HTMLResponse("<h3>Invalid or expired session</h3>", status_code=404)

    if pending.expires_at < _utc_now_comparable(pending.expires_at):
        return HTMLResponse("<h3>Session expired</h3>", status_code=410)

    from server.config import get_config
    provider_name = get_config().auth.oidc.provider_name or "SSO"

    sso_url_escaped = html.escape(pending.idp_authorize_url)
    sso_url_js = json.dumps(pending.idp_authorize_url)
    page = f"""<!DOCTYPE html>
<html>
<head><title>alibabacloud polardb tool agentic server - SSO Login</title>
<meta http-equiv="refresh" content="3;url={sso_url_escaped}">
<style>
body {{ font-family: sans-serif; display: flex; justify-content: center;
       align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }}
.card {{ background: white; padding: 2rem; border-radius: 8px;
         box-shadow: 0 2px 8px rgba(0,0,0,0.1); max-width: 480px; text-align: center; }}
.spinner {{ border: 3px solid #f3f3f3; border-top: 3px solid #1677ff;
            border-radius: 50%; width: 24px; height: 24px;
            animation: spin 1s linear infinite; display: inline-block; margin-bottom: 12px; }}
@keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
a {{ color: #1677ff; }}
.note {{ color: #888; font-size: 13px; margin-top: 16px; line-height: 1.5; }}
</style>
</head>
<body>
<div class="card">
  <div class="spinner"></div>
  <h3>Redirecting to {html.escape(provider_name.upper())} login...</h3>
  <p>If not redirected automatically, <a href="{sso_url_escaped}">click here</a>.</p>
  <p class="note">First-time users: the application may require admin approval.<br>
     If you see an error on the login page, please contact your administrator<br>
     to approve the application, then try connecting again.</p>
</div>
<script>setTimeout(function(){{ window.location.href = {sso_url_js}; }}, 3000);</script>
</body>
</html>"""
    return HTMLResponse(content=page)


async def handle_oidc_callback(request: Request) -> Response:
    """GET /auth/oidc/callback?code=...&state=... -- handle IdP redirect."""
    code = request.query_params.get("code", "")
    idp_state = request.query_params.get("state", "")

    if not code or not idp_state:
        return HTMLResponse("<h3>Missing code or state</h3>", status_code=400)

    factory = get_session_factory()
    async with factory() as db_session:
        # Find pending auth by idp_state
        result = await db_session.execute(
            select(OAuthPendingAuth).where(
                OAuthPendingAuth.idp_state == idp_state
            )
        )
        pending = result.scalar_one_or_none()
        if pending is None:
            return HTMLResponse(
                "<h3>Invalid or expired session</h3>", status_code=404
            )

        # Check expiry
        if pending.expires_at < _utc_now_comparable(pending.expires_at):
            return HTMLResponse("<h3>Session expired</h3>", status_code=410)

        # Exchange IdP code for tokens
        from server.config import get_config
        from server.auth.identity_federation import IdentityFederation
        from server.core.crypto import decrypt as crypto_decrypt

        config = get_config()
        federation = IdentityFederation(
            config.auth.oidc,
            provider_name=config.auth.oidc.provider_name,
        )
        await federation.discover_endpoints()

        # Recover IdP-side PKCE code_verifier if stored
        idp_code_verifier: str | None = None
        if pending.idp_code_verifier_enc:
            try:
                idp_code_verifier = crypto_decrypt(pending.idp_code_verifier_enc)
            except Exception:
                logger.warning("Failed to decrypt idp_code_verifier_enc")

        callback_url = (
            config.auth.oidc.redirect_uri
            or f"{config.server.public_base_url}/auth/oidc/callback"
        )
        token_response = await federation.exchange_code(
            code, callback_url, code_verifier=idp_code_verifier
        )

        # Extract user identity
        identity = await federation.extract_user_identity(token_response)

        # Find or create local user
        user = await federation.find_or_create_user(db_session, identity)

        redirect_uri = pending.redirect_uri
        state = pending.state

        mcp_code = secrets.token_urlsafe(32)
        code_hash = hashlib.sha256(mcp_code.encode()).hexdigest()

        code_record = OAuthAuthorizationCode(
            code_hash=code_hash,
            client_id=pending.client_id,
            user_id=user.id,
            redirect_uri=redirect_uri,
            redirect_uri_provided_explicitly=True,
            code_challenge=pending.code_challenge,
            code_challenge_method=pending.code_challenge_method,
            resource=pending.resource,
            scopes=pending.scopes,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        db_session.add(code_record)

        await db_session.delete(pending)
        await db_session.commit()

    separator = "&" if "?" in redirect_uri else "?"
    redirect_url = f"{redirect_uri}{separator}code={mcp_code}"
    if state:
        redirect_url += f"&state={state}"

    safe_url_js = json.dumps(redirect_url)
    page = f"""<!DOCTYPE html>
<html>
<head><title>Authorization Successful</title>
<style>
body {{ font-family: sans-serif; display: flex; justify-content: center;
       align-items: center; min-height: 100vh; margin: 0; background: #f5f5f5; }}
.card {{ background: white; padding: 2rem; border-radius: 8px;
         box-shadow: 0 2px 8px rgba(0,0,0,0.1); max-width: 420px; text-align: center; }}
.check {{ font-size: 48px; color: #52c41a; }}
.note {{ color: #888; font-size: 13px; margin-top: 12px; }}
</style>
</head>
<body>
<div class="card">
  <div class="check">&#10003;</div>
  <h3>Authorization Successful</h3>
  <p>Returning to your application...</p>
  <p class="note">This tab will close automatically.<br>
     If it doesn't, you may close it manually.</p>
</div>
<script>
window.location.href = {safe_url_js};
setTimeout(function(){{ try {{ window.close(); }} catch(e){{}} }}, 2000);
</script>
</body>
</html>"""
    return HTMLResponse(content=page)
