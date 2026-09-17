"""Personal MCP audience and compatibility routing.

An explicit audience keeps existing workspace grants unchanged and ensures older
images fail closed instead of interpreting personal credentials as Agent grants.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from starlette.responses import JSONResponse

PERSONAL_MCP_PATH = "/mcp/personal"
PERSONAL_METADATA_PATH = "/.well-known/oauth-protected-resource/mcp/personal"
request_mcp_mode: ContextVar[str | None] = ContextVar("request_mcp_mode", default=None)


def is_personal_access(token: Any) -> bool:
    claims = getattr(token, "claims", None)
    return isinstance(claims, dict) and claims.get("access_mode") == "personal"


class PersonalMCPMiddleware:
    def __init__(self, app, config):
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "").rstrip("/")
        base = self.config.server.public_base_url.rstrip("/")
        if path == PERSONAL_METADATA_PATH:
            if not base:
                return await JSONResponse({"error": "oauth_not_configured"}, status_code=503)(scope, receive, send)
            return await JSONResponse(
                {
                    "resource": base + PERSONAL_MCP_PATH,
                    "authorization_servers": [base],
                    "bearer_methods_supported": ["header"],
                }
            )(scope, receive, send)
        personal = path == PERSONAL_MCP_PATH
        if path != "/mcp" and not personal:
            return await self.app(scope, receive, send)
        original_mode = request_mcp_mode.set("personal" if personal else "legacy")
        try:
            if personal:
                scope = dict(scope, path="/mcp", raw_path=b"/mcp")

            async def send_response(message):
                if personal and message["type"] == "http.response.start":
                    headers = []
                    for name, value in message.get("headers", []):
                        if name.lower() == b"www-authenticate":
                            value = ('Bearer resource_metadata="' + base + PERSONAL_METADATA_PATH + '"').encode()
                        headers.append((name, value))
                    message = dict(message, headers=headers)
                await send(message)

            await self.app(scope, receive, send_response)
        finally:
            request_mcp_mode.reset(original_mode)
