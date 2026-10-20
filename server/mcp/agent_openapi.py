from __future__ import annotations

from functools import lru_cache

from fastapi.openapi.utils import get_openapi

from server.mcp.db_instance_rest import build_agent_db_instance_router
from server.version import __version__


@lru_cache(maxsize=1)
def agent_openapi_schema() -> dict:
    schema_router = build_agent_db_instance_router(include_in_schema=True)
    schema = get_openapi(
        title="PAS Agent Database Lifecycle REST API",
        version=__version__,
        description=(
            "Agent-only database create, poll, and delete contract. "
            "Credentials are sensitive: never cache, log, or forward them."
        ),
        routes=schema_router.routes,
    )
    schema["x-agent-instructions"] = {
        "workflow": ["create", "poll until READY", "use", "delete"],
        "credential_safety": "Never cache, log, or forward database passwords.",
    }
    return schema
