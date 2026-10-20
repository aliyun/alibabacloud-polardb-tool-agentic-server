from server.mcp.agent_openapi import agent_openapi_schema
from server.openapi import application_schema


def test_agent_lifecycle_schema_is_scoped_and_not_app_discoverable():
    app_schema = application_schema()
    scoped = agent_openapi_schema()
    assert "/api/v1/knowledge-bases" in app_schema["paths"]

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


def test_offline_schema_export(capsys, monkeypatch):
    import json
    from server.openapi import main

    for surface in ("application", "agent"):
        monkeypatch.setattr("sys.argv", ["openapi", "--surface", surface])
        main()
        schema = json.loads(capsys.readouterr().out)
        assert schema["openapi"]
        assert schema["paths"]
