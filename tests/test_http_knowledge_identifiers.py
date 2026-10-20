from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.api import platform_polarrag
from server.api.platform_oauth import get_platform_access_context
from server.db.engine import get_session
from server.mcp.tools.polarrag import _result
from server.models import AgentPolarRAGInstanceBinding, KnowledgeResource, PolarRAGSpace
from tests.test_knowledge_visibility_bind import catalog as catalog

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.mark.parametrize("route", ["knowledge-bases/search", "spaces/space/search", "documents/_find"])
async def test_platform_responses_add_pairs_without_changing_mcp_payload(setup, catalog, monkeypatch, route):
    factory, _, member = setup
    agent_id, _, resource_ids, _, _ = catalog
    item = {"knowledge_resource_id": resource_ids[0]}
    if route == "documents/_find":
        item["document"] = {"doc_id": "doc-1", "filename": "guide.pdf", "kb_id": "kb-0"}
        body = {"filename": "guide.pdf", "targets": [{"knowledge_resource_id": resource_ids[0]}]}
        payload = {"items": [item]}
    else:
        item.update(doc_id="doc-1", text="Original chunk", score=0.8)
        body = {"query": "guide"}
        payload = {"results": [item]}
    payload["partial_failures"] = [
        {"knowledge_resource_id": resource_ids[1], "error": "POLARRAG_UNAVAILABLE"},
        {"knowledge_resource_id": resource_ids[0], "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE"},
        {"knowledge_resource_id": "missing-resource", "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE"},
    ]
    tool_result = _result(payload)
    original = tool_result.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def invoke(*_args):
        return tool_result

    # A nonempty request avoids discovery; the MCP boundary owns access checks.
    if route == "knowledge-bases/search":
        body["targets"] = [{"knowledge_resource_id": value} for value in resource_ids]
    elif route == "spaces/space/search":
        body["knowledge_resource_ids"] = resource_ids
    app = FastAPI()
    app.include_router(platform_polarrag.router, prefix="/api")
    app.dependency_overrides[get_platform_access_context] = lambda: SimpleNamespace(
        agent=SimpleNamespace(id=agent_id), user=member,
    )

    async def session_dependency():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    monkeypatch.setattr(platform_polarrag, "_invoke_tool", invoke)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(f"/api/v1/{route}", json=body)
        assert response.status_code == 200, response.text
        result = response.json()
        row = result["items"][0]
        assert (row["knowledge_resource_id"], row["space_id"], row["kb_id"]) == (
            resource_ids[0], "space", "kb-0",
        )
        if route == "documents/_find":
            assert row["document"] == item["document"]
        else:
            assert (row["doc_id"], row["content"], row["score"]) == ("doc-1", "Original chunk", 0.8)
        assert result["partial_failures"] == [
            {**payload["partial_failures"][0], "space_id": "space", "kb_id": "kb-1"},
            {**payload["partial_failures"][1], "space_id": None, "kb_id": None},
            {**payload["partial_failures"][2], "space_id": None, "kb_id": None},
        ]
        forwarded = await http.post("/api/v1/tools/call", json={"name": "kb_search", "arguments": {}})
        assert forwarded.status_code == 200
        assert forwarded.json() == original
        assert json.loads(tool_result.content[0].text) == payload


async def test_search_pairs_follow_resource_ids_across_spaces(setup, catalog, monkeypatch):
    factory, _, _ = setup
    resource_ids = catalog[2]
    async with factory() as session:
        other_space = PolarRAGSpace(
            polarrag_instance_id=catalog[4], space_id="other-space", name="Other space",
            identity_domain="domain", enabled=True,
        )
        session.add(other_space)
        await session.flush()
        resource = await session.get(KnowledgeResource, resource_ids[1])
        resource.knowledge_space_id = other_space.knowledge_space_id
        resource.space_id = "other-space"
        resource.kb_id = "kb-0"
        await session.commit()

        async def invoke(*_args):
            return _result({"results": [
                {"knowledge_resource_id": value, "doc_id": "doc", "text": "chunk"}
                for value in reversed(resource_ids)
            ]})

        monkeypatch.setattr(platform_polarrag, "_invoke_tool", invoke)
        result = await platform_polarrag.search_knowledge_bases(
            platform_polarrag.KnowledgeBaseSearchRequest(query="guide"), SimpleNamespace(), session,
        )
        assert [(item["space_id"], item["kb_id"]) for item in result["items"]] == [
            ("other-space", "kb-0"), ("space", "kb-0"),
        ]


async def test_public_resource_options_include_both_identifiers(client, catalog):
    http, headers, _ = client
    response = await http.get(
        f"/api/agents/{catalog[0]}/polarrag-bindings/{catalog[1]}/public-resources", headers=headers,
    )
    assert response.status_code == 200
    assert [(item["knowledge_resource_id"], item["space_id"], item["kb_id"])
            for item in response.json()["items"]] == [
        (catalog[2][0], "space", "kb-0"), (catalog[2][1], "space", "kb-1"),
    ]


@pytest.mark.parametrize("selector", ["resource_ids", "pairs"])
@pytest.mark.parametrize("route", ["knowledge-binding-status", "knowledge-all-users-status", "knowledge-all-users"])
async def test_admin_status_responses_keep_legacy_ids_and_add_pairs(client, setup, catalog, route, selector):
    http, headers, _ = client
    factory, _, member = setup
    async with factory() as session:
        binding = await session.get(AgentPolarRAGInstanceBinding, catalog[1])
        binding.public_knowledge_resource_ids_json = None
        await session.commit()
    body = (
        {"knowledge_resource_ids": list(reversed(catalog[2]))}
        if selector == "resource_ids"
        else {"targets": [{"space_id": "space", "kb_id": "kb-1"}, {"space_id": "space", "kb_id": "kb-0"}]}
    )
    if route == "knowledge-binding-status":
        body["subjects"] = [{"type": "USER", "user_id": member.id}]
    elif route == "knowledge-all-users":
        body["operation"] = "BIND"
    response = await http.post(f"/api/admin/agents/{catalog[0]}/{route}", headers=headers, json=body)
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert {(item["knowledge_resource_id"], item["space_id"], item["kb_id"]) for item in rows} == {
        (catalog[2][0], "space", "kb-0"), (catalog[2][1], "space", "kb-1"),
    }
    assert all(item["status"] in {"BOUND", "PARTIAL", "UNBOUND"} for item in rows)


def test_search_openapi_documents_pairs_only_for_multi_resource_routes():
    app = FastAPI()
    app.include_router(platform_polarrag.router, prefix="/api")
    schema = app.openapi()
    for path in ("/api/v1/knowledge-bases/search", "/api/v1/spaces/{space_id}/search"):
        response = schema["paths"][path]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        model = schema["components"]["schemas"][response["$ref"].split("/")[-1]]
        item_ref = model["properties"]["items"]["items"]["$ref"]
        fields = schema["components"]["schemas"][item_ref.split("/")[-1]]["properties"]
        assert {"knowledge_resource_id", "kb_id", "space_id"} <= fields.keys()
