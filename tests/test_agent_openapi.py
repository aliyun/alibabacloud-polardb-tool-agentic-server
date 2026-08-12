from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.mcp.agent_openapi import router as agent_openapi_router
from server.mcp.db_instance_rest import router as agent_db_instance_router


async def test_agent_lifecycle_schema_is_scoped_and_not_app_discoverable():
    app = FastAPI()
    app.include_router(agent_db_instance_router)
    app.include_router(agent_openapi_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        app_schema = (await client.get("/openapi.json")).json()
        scoped = (await client.get("/mcp/rest/openapi.json")).json()

    lifecycle_paths = {
        "/mcp/rest/db-instances",
        "/mcp/rest/db-instances/{resource_id}",
    }
    assert lifecycle_paths.isdisjoint(app_schema["paths"])
    assert set(scoped["paths"]) == lifecycle_paths
    assert set(scoped["components"]["securitySchemes"]) == {"AgentBearer"}
    for path in scoped["paths"].values():
        for operation in path.values():
            assert operation["security"] == [{"AgentBearer": []}]


async def test_agent_docs_points_to_scoped_schema():
    app = FastAPI()
    app.include_router(agent_openapi_router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/mcp/rest/docs")
    assert response.status_code == 200
    assert "/mcp/rest/openapi.json" in response.text
