from __future__ import annotations

from typing import Any

from server.core.agent_user_token_service import TOKEN_PREFIX


def access_token_agent_id(access_token: Any) -> str | None:
    claims = getattr(access_token, "claims", None)
    if isinstance(claims, dict):
        agent_id = claims.get("agent_id")
        if isinstance(agent_id, str) and agent_id:
            return agent_id
    client_id = getattr(access_token, "client_id", "")
    if isinstance(client_id, str) and client_id.startswith("agent:"):
        return client_id.removeprefix("agent:")
    return None


def is_legacy_agent_user_token(access_token: Any) -> bool:
    token = getattr(access_token, "token", "")
    if isinstance(token, str) and token.startswith(TOKEN_PREFIX):
        return True
    client_id = getattr(access_token, "client_id", "")
    return isinstance(client_id, str) and client_id.startswith("agent:")
