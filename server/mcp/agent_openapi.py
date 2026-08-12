from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.openapi.docs import get_swagger_ui_html

from server.mcp.db_instance_rest import build_agent_db_instance_router
from server.version import __version__

router = APIRouter(include_in_schema=False)


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


@router.get("/mcp/rest/openapi.json", include_in_schema=False)
async def agent_openapi() -> JSONResponse:
    return JSONResponse(agent_openapi_schema())


@router.get(
    "/mcp/rest/docs",
    include_in_schema=False,
    response_class=HTMLResponse,
)
async def agent_docs() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/mcp/rest/openapi.json",
        title="PAS Agent Database Lifecycle REST API",
    )
