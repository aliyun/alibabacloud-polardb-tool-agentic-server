from __future__ import annotations

from server.app import create_app


def test_only_external_token_exchange_route_is_exposed():
    paths = create_app().openapi()["paths"]

    assert "/api/v1/oauth/authorization-requests" not in paths
    assert "/api/v1/oauth/token" not in paths
    assert "/api/v1/oauth/revoke" not in paths
    assert "/api/v1/external-auth/token" in paths


def test_external_token_exchange_openapi_describes_form_and_errors():
    operation = create_app().openapi()["paths"][
        "/api/v1/external-auth/token"
    ]["post"]
    form_schema = operation["requestBody"]["content"][
        "application/x-www-form-urlencoded"
    ]["schema"]

    assert form_schema["required"] == [
        "grant_type",
        "subject_token",
        "subject_token_type",
    ]
    assert "resource" in form_schema["properties"]
    assert "agent_id" in form_schema["properties"]
    assert {
        "identity_source_id",
        "feishu_user_id",
        "feishu_union_id",
    }.issubset(form_schema["properties"])
    assert set(operation["responses"]) >= {
        "200",
        "400",
        "401",
        "413",
        "429",
        "503",
    }


def test_platform_polarrag_resource_routes_remain_exposed():
    from tests._knowledge_helpers import enable_knowledge_routes

    assert "/api/v1/knowledge-bases" not in create_app().openapi()["paths"]
    paths = enable_knowledge_routes(create_app()).openapi()["paths"]

    assert "/api/v1/knowledge-bases" in paths
    assert "/api/v1/tools/call" in paths
