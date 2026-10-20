from __future__ import annotations

import json
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Response, UploadFile
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from server.api.platform_oauth import PlatformAccessContext
import server.api.platform_polarrag as platform_polarrag
from server.api.platform_polarrag import (
    DocumentFindByNameRequest,
    DocumentBatchRechunkRequest,
    DocumentRechunkRequest,
    DocumentUploadPrepareRequest,
    KnowledgeBaseSearchRequest,
    KnowledgeBaseSelector,
    SearchRequest,
    SearchHitResponse,
    SpaceSearchRequest,
    _document_search_results,
    _chunk_list_response,
    _invoke_tool,
    _resolve_search_targets,
    _resolve_space_knowledge_base,
    _search_response,
    _tool_payload,
    batch_rechunk_knowledge_base_documents,
    batch_rechunk_space_knowledge_base_documents,
    complete_knowledge_base_document_upload,
    find_knowledge_base_documents,
    get_knowledge_base_document,
    get_knowledge_base_document_chunk_context,
    list_knowledge_base_document_chunks,
    list_knowledge_base_documents,
    prepare_knowledge_base_document_upload,
    rechunk_knowledge_base_document,
    search_knowledge_bases,
    list_space_knowledge_base_documents,
    list_space_knowledge_base_document_chunks,
    router,
    search_space_knowledge_base,
    search_space_knowledge_base_document,
    search_space_knowledge_bases,
)
from server.api.polarrag_documents import (
    _visible_document_page,
    _visible_document_acl,
    _visible_documents,
    attach_document_permissions,
)
from server.mcp.tools.polarrag import POLARRAG_TOOL_NAMES


def test_document_search_maps_raw_chunks_and_scores():
    results = _document_search_results(
        {
            "hits": {
                "hits": [
                    {
                        "_score": 0.87,
                        "_source": {
                            "doc_id": "doc-1",
                            "doc_generation": 2,
                            "filename": "退款 指南.backup.v2.pdf",
                            "source_uri": "oss://bucket/refund.pdf",
                            "chunk_index": 3,
                            "text": "original chunk",
                            "page_numbers": [4],
                            "image_resources": [
                                {
                                    "id": "image-1",
                                    "oss_uri": "oss://bucket/image-1.png",
                                }
                            ],
                            "metadata": {"section": "refund"},
                        },
                    }
                ]
            }
        },
        knowledge_resource_id="resource-1",
        doc_id="doc-1",
    )
    response = _search_response({"results": results, "partial_failures": []}, 10)

    assert response == {
        "items": [
            {
                "knowledge_resource_id": "resource-1",
                "doc_id": "doc-1",
                "doc_generation": 2,
                "filename": "退款 指南.backup.v2.pdf",
                "source_uri": "oss://bucket/refund.pdf",
                "chunk_index": 3,
                "content": "original chunk",
                "score": 0.87,
                "image_resources": [
                    {
                        "id": "image-1",
                        "oss_uri": "oss://bucket/image-1.png",
                    }
                ],
                "metadata": {
                    "page_numbers": [4],
                    "headings": [],
                    "captions": [],
                    "section": "refund",
                },
            }
        ],
        "top_k": 10,
        "partial_failures": [],
    }


def test_search_hit_response_exposes_filename_and_source_uri():
    item = SearchHitResponse.model_validate(
        {
            "knowledge_resource_id": "resource-1",
            "doc_id": "doc-1",
            "filename": "退款 指南.backup.v2.pdf",
            "source_uri": "oss://bucket/refund.pdf",
            "chunk_index": 3,
            "content": "original chunk",
            "score": 0.87,
            "image_resources": [],
            "metadata": {},
        }
    )

    assert item.model_dump()["filename"] == "退款 指南.backup.v2.pdf"
    assert item.model_dump()["source_uri"] == "oss://bucket/refund.pdf"


@pytest.mark.parametrize(
    ("method", "path", "tool_name", "body"),
    [
        (
            "post",
            "/api/v1/knowledge-bases/search",
            "kb_search",
            {
                "query": "退款",
                "targets": [{"knowledge_resource_id": "resource-a"}],
            },
        ),
        (
            "post",
            "/api/v1/spaces/space-a/search",
            "kb_search",
            {"query": "退款", "knowledge_resource_ids": ["resource-a"]},
        ),
        (
            "post",
            "/api/v1/knowledge-bases/resource-a/search",
            "kb_search",
            {"query": "退款"},
        ),
        (
            "post",
            "/api/v1/spaces/space-a/knowledge-bases/kb-a/search",
            "kb_search",
            {"query": "退款"},
        ),
        (
            "post",
            "/api/v1/knowledge-bases/resource-a/documents/doc-a/search",
            "doc_recall",
            {"query": "退款"},
        ),
        (
            "post",
            "/api/v1/spaces/space-a/knowledge-bases/kb-a/documents/doc-a/search",
            "doc_recall",
            {"query": "退款"},
        ),
        (
            "get",
            "/api/v1/knowledge-bases/resource-a/documents/doc-a/chunks?limit=2",
            "doc_list_chunks",
            None,
        ),
        (
            "get",
            "/api/v1/spaces/space-a/knowledge-bases/kb-a/documents/doc-a/chunks?limit=2",
            "doc_list_chunks",
            None,
        ),
        (
            "get",
            "/api/v1/knowledge-bases/resource-a/documents/doc-a/chunks/0/context",
            "kb_fetch_context",
            None,
        ),
        (
            "get",
            "/api/v1/spaces/space-a/knowledge-bases/kb-a/documents/doc-a/chunks/0/context",
            "kb_fetch_context",
            None,
        ),
    ],
)
@pytest.mark.parametrize("authorized", [True, False])
@pytest.mark.parametrize("source_uri_state", ["present", "null", "missing"])
async def test_filename_http_contract_across_search_and_chunk_routes(
    monkeypatch,
    method,
    path,
    tool_name,
    body,
    authorized,
    source_uri_state,
):
    import httpx
    from server.db.engine import get_session

    filename = "退款 指南.backup.v2.pdf"
    source_uri = "oss://bucket/refund.pdf"
    source_fields = (
        {} if source_uri_state == "missing"
        else {"source_uri": source_uri if source_uri_state == "present" else None}
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[platform_polarrag.get_platform_access_context] = (
        lambda: SimpleNamespace()
    )
    app.dependency_overrides[get_session] = lambda: SimpleNamespace()

    async def resolve_resource(*_args, **_kwargs):
        return "resource-a"

    async def resource_pairs(_session, resource_ids):
        assert set(resource_ids) == {"resource-a"}
        return {"resource-a": {"space_id": "space-a", "kb_id": "kb-a"}}

    raw_hits = [
        {
            "_score": 1.0 - chunk_index / 10,
            "_source": {
                "doc_id": "doc-a",
                "doc_generation": 2,
                "filename": filename,
                **source_fields,
                "chunk_index": chunk_index,
                "text": f"chunk {chunk_index}",
            },
        }
        for chunk_index in range(2)
    ]

    async def invoke(_context, name, _arguments):
        assert name == tool_name
        if not authorized:
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=json.dumps({"error": "NO_ACCESSIBLE_KNOWLEDGE_RESOURCE"}),
                    )
                ],
                isError=True,
            )
        if name == "kb_search":
            payload = {
                "results": [
                    {
                        "knowledge_resource_id": "resource-a",
                        **hit["_source"],
                        "score": hit["_score"],
                    }
                    for hit in raw_hits
                ],
                "partial_failures": [],
            }
        else:
            payload = {"result": {"hits": {"hits": raw_hits}}}
            if name == "doc_list_chunks":
                payload["result"]["hits"]["total"] = {
                    "value": 2,
                    "relation": "eq",
                }
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            isError=False,
        )

    monkeypatch.setattr(platform_polarrag, "_resolve_search_targets", resolve_resource)
    monkeypatch.setattr(
        platform_polarrag,
        "_resolve_space_knowledge_base",
        resolve_resource,
    )
    monkeypatch.setattr(platform_polarrag, "knowledge_resource_pairs", resource_pairs)
    monkeypatch.setattr(platform_polarrag, "_invoke_tool", invoke)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        response = await http.request(method, path, json=body)

    if not authorized:
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "NO_ACCESSIBLE_KNOWLEDGE_RESOURCE"
        assert "filename" not in response.text
        assert filename not in response.text
        assert "source_uri" not in response.text
        assert source_uri not in response.text
        return

    assert response.status_code == 200, response.text
    payload = response.json()
    items = (
        [hit["_source"] for hit in payload["hits"]["hits"]]
        if tool_name == "kb_fetch_context"
        else payload["items"]
    )
    assert len(items) == 2
    assert [item["filename"] for item in items] == [filename, filename]
    assert [item["doc_generation"] for item in items] == [2, 2]
    if source_uri_state == "missing" and tool_name in {"doc_list_chunks", "kb_fetch_context"}:
        assert all("source_uri" not in item for item in items)
    else:
        expected = source_uri if source_uri_state == "present" else None
        assert [item["source_uri"] for item in items] == [expected, expected]


async def test_document_detail_includes_plugin_permission_details(monkeypatch):
    resource = SimpleNamespace(space_id="space-a", kb_id="kb-a")
    access = SimpleNamespace(
        resources=[resource],
        instance=SimpleNamespace(),
        acl_context={
            "identity_domain": "tenant-a",
            "principals": [
                {"provider": "polarrag", "type": "user", "id": "pas-user"}
            ],
        },
    )

    class Upstream:
        supports_plugin_107 = True

        async def document_info(self, space_id, doc_id, **_kwargs):
            return {
                "doc_id": doc_id,
                "space_id": space_id,
                "kb_id": "kb-a",
                "filename": "guide.md",
            }

        async def list_document_permissions(self, _space_id, _doc_ids, **_kwargs):
            return {
                "documents": [
                    {
                        "doc_id": "doc-a",
                        "identity_domain": "tenant-a",
                        "state": "ACTIVE",
                        "updating": False,
                        "acl_version": 1,
                        "acl_set_id": "acl-set-a",
                        "grants": [
                            {
                                "principal": {
                                    "provider": "polarrag",
                                    "type": "user",
                                    "id": "pas-user",
                                },
                                "actions": ["READ"],
                            }
                        ],
                    }
                ]
            }

    monkeypatch.setattr(platform_polarrag, "_member_access", AsyncMock(return_value=access))
    monkeypatch.setattr(platform_polarrag, "client_from_instance", lambda _instance: Upstream())

    result = await get_knowledge_base_document(
        "resource-a",
        "doc-a",
        SimpleNamespace(agent=SimpleNamespace(id="agent-a"), user=SimpleNamespace()),
        SimpleNamespace(),
    )

    assert result["acl"]["grants"][0]["actions"] == ["READ"]


async def test_document_permissions_are_split_at_plugin_batch_limit():
    class Upstream:
        supports_plugin_107 = True

        def __init__(self):
            self.batches = []

        async def list_document_permissions(self, _space_id, doc_ids, **_kwargs):
            assert len(doc_ids) <= 200
            self.batches.append(doc_ids)
            return {"documents": []}

    upstream = Upstream()
    documents = [{"doc_id": f"doc-{index}"} for index in range(201)]

    result = await attach_document_permissions(
        upstream,
        "space-a",
        documents,
        {
            "identity_domain": "tenant-a",
            "principals": [
                {"provider": "polarrag", "type": "user", "id": "pas-user"}
            ],
        },
    )

    assert [len(batch) for batch in upstream.batches] == [200, 1]
    assert all(document["acl"] is None for document in result)


def test_document_permission_rejects_non_string_action():
    with pytest.raises(HTTPException) as captured:
        _visible_document_acl(
            {
                "doc_id": "doc-a",
                "identity_domain": "tenant-a",
                "state": "ACTIVE",
                "updating": False,
                "acl_version": 1,
                "acl_set_id": "acl-set-a",
                "grants": [
                    {
                        "principal": {
                            "provider": "polarrag",
                            "type": "user",
                            "id": "pas-user",
                        },
                        "actions": [{}],
                    }
                ],
            },
            expected_doc_id="doc-a",
        )

    assert captured.value.status_code == 502


async def test_document_list_cursor_continues_plugin_107_page(monkeypatch):
    requests = []

    async def fake_list_documents(body, *, user, session):
        requests.append(body)
        return {
            "documents": [],
            "has_more": body.page < 2,
            "next_after_doc_id": None,
            "total": 21,
            "page": body.page,
            "size": body.size,
        }

    monkeypatch.setattr(
        "server.api.platform_polarrag.list_documents",
        fake_list_documents,
    )
    context = SimpleNamespace(agent=SimpleNamespace(id="agent-a"), user=SimpleNamespace())
    session = SimpleNamespace()

    first = await list_knowledge_base_documents(
        "resource-a",
        20,
        None,
        None,
        1,
        context,
        session,
    )
    second = await list_knowledge_base_documents(
        "resource-a",
        20,
        first["next_cursor"],
        None,
        1,
        context,
        session,
    )

    assert isinstance(first["next_cursor"], str)
    assert first["next_cursor"].startswith("pas-document-page-v2:2:20")
    assert second["next_cursor"] is None
    assert [request.page for request in requests] == [1, 2]
    assert [request.after_doc_id for request in requests] == [None, None]


async def test_document_list_rejects_non_ascii_page_cursor():
    context = SimpleNamespace(agent=SimpleNamespace(id="agent-a"), user=SimpleNamespace())

    with pytest.raises(HTTPException) as captured:
        await list_knowledge_base_documents(
            "resource-a",
            20,
            "pas-document-page-v1:²",
            None,
            1,
            context,
            SimpleNamespace(),
        )

    assert captured.value.status_code == 400
    assert captured.value.detail["code"] == "INVALID_CURSOR"


async def test_document_list_rejects_page_size_change_during_cursor_pagination(
    monkeypatch,
):
    async def fake_list_documents(body, *, user, session):
        return {
            "documents": [],
            "has_more": True,
            "next_after_doc_id": None,
            "total": 40,
            "page": body.page,
            "size": body.size,
        }

    monkeypatch.setattr(
        "server.api.platform_polarrag.list_documents",
        fake_list_documents,
    )
    context = SimpleNamespace(agent=SimpleNamespace(id="agent-a"), user=SimpleNamespace())
    first = await list_knowledge_base_documents(
        "resource-a",
        20,
        None,
        None,
        1,
        context,
        SimpleNamespace(),
    )

    with pytest.raises(HTTPException) as captured:
        await list_knowledge_base_documents(
            "resource-a",
            10,
            first["next_cursor"],
            None,
            1,
            context,
            SimpleNamespace(),
        )

    assert captured.value.status_code == 400
    assert captured.value.detail["code"] == "CURSOR_LIMIT_MISMATCH"


async def test_document_list_rejects_legacy_page_cursor_without_upstream_call(
    monkeypatch,
):
    requests = []

    async def fake_list_documents(body, *, user, session):
        requests.append(body)
        return {
            "documents": [],
            "has_more": False,
            "next_after_doc_id": None,
            "total": 21,
            "page": body.page,
            "size": body.size,
        }

    monkeypatch.setattr(
        "server.api.platform_polarrag.list_documents",
        fake_list_documents,
    )
    context = SimpleNamespace(agent=SimpleNamespace(id="agent-a"), user=SimpleNamespace())
    with pytest.raises(HTTPException) as captured:
        await list_knowledge_base_documents(
            "resource-a",
            10,
            "pas-document-page-v1:2",
            None,
            1,
            context,
            SimpleNamespace(),
        )

    assert captured.value.status_code == 400
    assert captured.value.detail["code"] == "INVALID_CURSOR"
    assert requests == []


@pytest.mark.parametrize(
    ("cursor", "payload"),
    [
        (
            "legacy-doc-id",
            {
                "documents": [{"doc_id": "new-first-page"}],
                "has_more": True,
                "next_after_doc_id": None,
                "total": 40,
                "page": 1,
                "size": 20,
            },
        ),
        (
            "pas-document-page-v2:2:20",
            {
                "documents": [{"doc_id": "legacy-first-page"}],
                "has_more": True,
                "next_after_doc_id": "legacy-next",
            },
        ),
    ],
)
async def test_document_list_rejects_cursor_protocol_change(
    monkeypatch,
    cursor,
    payload,
):
    async def fake_list_documents(body, *, user, session):
        return payload

    monkeypatch.setattr(
        "server.api.platform_polarrag.list_documents",
        fake_list_documents,
    )
    context = SimpleNamespace(agent=SimpleNamespace(id="agent-a"), user=SimpleNamespace())

    with pytest.raises(HTTPException) as captured:
        await list_knowledge_base_documents(
            "resource-a",
            20,
            cursor,
            None,
            1,
            context,
            SimpleNamespace(),
        )

    assert captured.value.status_code == 400
    assert captured.value.detail["code"] == "CURSOR_PROTOCOL_MISMATCH"


async def test_document_list_continues_legacy_raw_cursor(monkeypatch):
    requests = []

    async def fake_list_documents(body, *, user, session):
        requests.append(body)
        return {
            "documents": [{"doc_id": "legacy-second-page"}],
            "has_more": False,
            "next_after_doc_id": None,
        }

    monkeypatch.setattr(
        "server.api.platform_polarrag.list_documents",
        fake_list_documents,
    )
    context = SimpleNamespace(agent=SimpleNamespace(id="agent-a"), user=SimpleNamespace())
    response = await list_knowledge_base_documents(
        "resource-a",
        20,
        "legacy-doc-id",
        None,
        1,
        context,
        SimpleNamespace(),
    )

    assert response["items"] == [{"doc_id": "legacy-second-page"}]
    assert requests[0].after_doc_id == "legacy-doc-id"


def test_document_search_rejects_cross_document_hit():
    with pytest.raises(HTTPException) as captured:
        _document_search_results(
            {
                "hits": {
                    "hits": [
                        {
                            "_score": 1.0,
                            "_source": {
                                "doc_id": "other-doc",
                                "chunk_index": 0,
                                "text": "hidden",
                            },
                        }
                    ]
                }
            },
            knowledge_resource_id="resource-1",
            doc_id="doc-1",
        )
    assert captured.value.status_code == 502
    assert captured.value.detail["code"] == "POLARRAG_INVALID_RESPONSE"


def test_document_chunk_list_maps_image_resources_and_offset():
    response = _chunk_list_response(
        {
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "hits": [
                    {
                        "_source": {
                            "doc_id": "doc-1",
                            "chunk_index": 1,
                            "text": "chunk",
                            "image_resources": [
                                {
                                    "id": "document-0/pictures/1",
                                    "oss_uri": "oss://bucket/picture-1.png",
                                }
                            ],
                        }
                    }
                ],
            }
        },
        doc_id="doc-1",
        offset=0,
        limit=1,
    )

    assert response == {
        "items": [
            {
                "doc_id": "doc-1",
                "chunk_index": 1,
                "text": "chunk",
                "image_resources": [
                    {
                        "id": "document-0/pictures/1",
                        "oss_uri": "oss://bucket/picture-1.png",
                    }
                ],
            }
        ],
        "offset": 0,
        "limit": 1,
        "total": 2,
        "next_offset": 1,
    }


def test_document_lists_preserve_upstream_source():
    search_documents = _visible_documents(
        {
            "hits": {
                "hits": [
                    {
                        "_source": {
                            "doc_id": "doc-1",
                            "kb_id": "kb-1",
                            "filename": "native.docx",
                            "source": "NATIVE",
                        }
                    }
                ]
            }
        },
        "kb-1",
    )
    page_documents = _visible_document_page(
        {
            "documents": [
                {
                    "doc_id": "doc-2",
                    "kb_id": "kb-1",
                    "filename": "oss.pdf",
                    "source": "OSS",
                }
            ]
        },
        "kb-1",
    )

    assert search_documents[0]["source"] == "NATIVE"
    assert page_documents[0]["source"] == "OSS"


async def test_document_chunk_route_probes_for_next_page(monkeypatch):
    calls = []

    async def fake_invoke(_context, _tool_name, arguments):
        calls.append(arguments)
        chunk_indexes = [0, 1] if arguments["offset"] == 0 else [2]
        payload = {
            "hits": {
                "total": {"value": 2},
                "hits": [
                    {
                        "_source": {
                            "doc_id": "doc-1",
                            "chunk_index": index,
                        }
                    }
                    for index in chunk_indexes
                ],
            }
        }
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"result": payload}))])

    monkeypatch.setattr("server.api.platform_polarrag._invoke_tool", fake_invoke)

    response = await list_knowledge_base_document_chunks(
        "resource-1",
        "doc-1",
        offset=0,
        limit=2,
        context=SimpleNamespace(),
    )

    assert [call["offset"] for call in calls] == [0, 2]
    assert response["total"] is None
    assert response["next_offset"] == 2


async def test_document_chunk_route_uses_exact_total_without_probe(monkeypatch):
    calls = []

    async def fake_invoke(_context, _tool_name, arguments):
        calls.append(arguments)
        payload = {
            "hits": {
                "total": {"value": 3, "relation": "eq"},
                "hits": [
                    {
                        "_source": {
                            "doc_id": "doc-1",
                            "chunk_index": index,
                        }
                    }
                    for index in (0, 1)
                ],
            }
        }
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"result": payload}))])

    monkeypatch.setattr("server.api.platform_polarrag._invoke_tool", fake_invoke)

    response = await list_knowledge_base_document_chunks(
        "resource-1",
        "doc-1",
        offset=0,
        limit=2,
        context=SimpleNamespace(),
    )

    assert len(calls) == 1
    assert response["total"] == 3
    assert response["next_offset"] == 2


def test_tool_error_maps_inaccessible_resource_to_not_found():
    result = CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
                        "message": "not accessible",
                    }
                ),
            )
        ],
        isError=True,
    )
    with pytest.raises(HTTPException) as captured:
        _tool_payload(result)
    assert captured.value.status_code == 404
    assert captured.value.detail["code"] == ("KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE")


def test_tool_error_maps_no_accessible_knowledge_resource_to_not_found():
    result = CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps({"error": "NO_ACCESSIBLE_KNOWLEDGE_RESOURCE"}),
            )
        ],
        isError=True,
    )
    with pytest.raises(HTTPException) as captured:
        _tool_payload(result)
    assert captured.value.status_code == 404
    assert captured.value.detail["code"] == "NO_ACCESSIBLE_KNOWLEDGE_RESOURCE"


def test_tool_error_maps_upstream_unavailable_to_service_unavailable():
    result = CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(
                    {
                        "error": "POLARRAG_UNAVAILABLE",
                        "message": "temporarily unavailable",
                    }
                ),
            )
        ],
        isError=True,
    )
    with pytest.raises(HTTPException) as captured:
        _tool_payload(result)
    assert captured.value.status_code == 503


async def test_invoke_tool_uses_only_public_fastmcp_methods(monkeypatch):
    class PublicOnlyMCP:
        def __init__(self):
            self.called_with = None

        async def list_tools(self):
            return [SimpleNamespace(name="visible_tool")]

        async def call_tool(self, name, arguments):
            self.called_with = (name, arguments)
            return [TextContent(type="text", text='{"result":"ok"}')]

    mcp = PublicOnlyMCP()
    monkeypatch.setattr(
        "server.api.platform_polarrag._get_mcp_server",
        lambda: mcp,
    )
    context = PlatformAccessContext(
        access_token=AccessToken(
            token="platform-token",
            client_id="agent:agent-id",
            scopes=["polarrag"],
            resource="https://pas.example.com/api/v1",
            subject="user:user-id",
        ),
        user=None,
        agent=None,
    )

    result = await _invoke_tool(
        context,
        "visible_tool",
        {"query": "hello"},
    )

    assert mcp.called_with == ("visible_tool", {"query": "hello"})
    assert result == CallToolResult(
        content=[TextContent(type="text", text='{"result":"ok"}')],
        isError=False,
    )


def test_knowledge_base_selector_requires_one_supported_identifier():
    assert KnowledgeBaseSelector(space_id="space-a", kb_id="kb-a").model_dump() == {
        "knowledge_resource_id": None,
        "space_id": "space-a",
        "kb_id": "kb-a",
    }
    with pytest.raises(ValidationError):
        KnowledgeBaseSelector(knowledge_resource_id="resource-a", kb_id="kb-a")
    with pytest.raises(ValidationError):
        KnowledgeBaseSelector(space_id="space-a")
    assert SpaceSearchRequest(query="refund policy").kb_ids is None
    with pytest.raises(ValidationError):
        SpaceSearchRequest(query="refund policy", kb_ids=[])


async def test_resolve_space_knowledge_base_rejects_ambiguous_space_id():
    def scalar_result(values):
        return SimpleNamespace(scalars=lambda: values)

    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                scalar_result(
                    [
                        SimpleNamespace(
                            space_id="space-a",
                            knowledge_space_id="knowledge-space-a",
                        )
                    ]
                ),
                scalar_result(["resource-a", "resource-b"]),
            ]
        )
    )

    with pytest.raises(HTTPException) as captured:
        await _resolve_space_knowledge_base(
            session,
            space_id="space-a",
            kb_id="kb-a",
        )

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "SPACE_ID_AMBIGUOUS"


async def test_space_target_rejects_resource_from_another_space():
    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(space_id="space-b")))

    with pytest.raises(HTTPException) as captured:
        await _resolve_search_targets(
            session,
            [KnowledgeBaseSelector(knowledge_resource_id="resource-a")],
            expected_space_id="space-a",
        )

    assert captured.value.status_code == 422
    assert captured.value.detail["code"] == "KNOWLEDGE_RESOURCE_OUTSIDE_SPACE"


async def test_multi_knowledge_base_search_resolves_mixed_selectors(monkeypatch):
    captured = {}

    async def fake_resolve(session, targets, *, expected_space_id=None):
        captured["targets"] = [target.model_dump() for target in targets]
        captured["expected_space_id"] = expected_space_id
        return ["resource-a", "resource-b"]

    async def fake_invoke(context, name, arguments):
        captured["tool"] = (name, arguments)
        return CallToolResult(
            content=[TextContent(type="text", text='{"results":[],"partial_failures":[]}')],
            isError=False,
        )

    monkeypatch.setattr(
        "server.api.platform_polarrag._resolve_search_targets",
        fake_resolve,
    )
    monkeypatch.setattr("server.api.platform_polarrag._invoke_tool", fake_invoke)

    response = await search_knowledge_bases(
        KnowledgeBaseSearchRequest(
            query="refund policy",
            targets=[
                KnowledgeBaseSelector(knowledge_resource_id="resource-a"),
                KnowledgeBaseSelector(space_id="space-a", kb_id="kb-b"),
            ],
        ),
        context=SimpleNamespace(),
        session=SimpleNamespace(),
    )

    assert captured["targets"] == [
        {"knowledge_resource_id": "resource-a", "space_id": None, "kb_id": None},
        {"knowledge_resource_id": None, "space_id": "space-a", "kb_id": "kb-b"},
    ]
    assert captured["expected_space_id"] is None
    assert captured["tool"] == (
        "kb_search",
        {
            "query": "refund policy",
            "knowledge_resource_ids": ["resource-a", "resource-b"],
            "search_mode": "balanced",
            "top_k": 10,
            "min_score": None,
            "reranker": False,
        },
    )
    assert response == {"items": [], "top_k": 10, "partial_failures": []}


async def test_space_search_resolves_multiple_knowledge_bases(monkeypatch):
    captured = {}

    async def fake_resolve(session, targets, *, expected_space_id=None):
        captured["targets"] = [target.model_dump() for target in targets]
        captured["expected_space_id"] = expected_space_id
        return ["resource-a", "resource-b", "resource-c"]

    async def fake_invoke(context, name, arguments):
        captured["tool"] = (name, arguments)
        return CallToolResult(
            content=[TextContent(type="text", text='{"results":[],"partial_failures":[]}')],
            isError=False,
        )

    monkeypatch.setattr(
        "server.api.platform_polarrag._resolve_search_targets",
        fake_resolve,
    )
    monkeypatch.setattr("server.api.platform_polarrag._invoke_tool", fake_invoke)

    await search_space_knowledge_bases(
        "space-a",
        SpaceSearchRequest(
            query="refund policy",
            kb_ids=["kb-a", "kb-b"],
            knowledge_resource_ids=["resource-c"],
        ),
        context=SimpleNamespace(),
        session=SimpleNamespace(),
    )

    assert captured["targets"] == [
        {"knowledge_resource_id": None, "space_id": "space-a", "kb_id": "kb-a"},
        {"knowledge_resource_id": None, "space_id": "space-a", "kb_id": "kb-b"},
        {"knowledge_resource_id": "resource-c", "space_id": None, "kb_id": None},
    ]
    assert captured["expected_space_id"] == "space-a"
    assert captured["tool"][1]["knowledge_resource_ids"] == [
        "resource-a",
        "resource-b",
        "resource-c",
    ]


async def test_space_search_rejects_visible_resources_above_configured_limit(
    monkeypatch,
):
    captured = {}

    async def fake_space(_session, _space_id):
        return SimpleNamespace(knowledge_space_id="space-key")

    async def fake_scope(_session, _agent_id, _user_id):
        return SimpleNamespace()

    async def fake_page(_session, _user, **kwargs):
        captured.update(kwargs)
        return [SimpleNamespace(id="resource-a")], True, True

    monkeypatch.setattr("server.api.platform_polarrag._resolve_space", fake_space)
    monkeypatch.setattr(
        "server.api.platform_polarrag.resolve_polarrag_resource_scope_for_agent",
        fake_scope,
    )
    monkeypatch.setattr(
        "server.api.platform_polarrag.list_visible_knowledge_resources_cursor_page",
        fake_page,
    )
    monkeypatch.setattr(
        "server.api.platform_polarrag.get_config",
        lambda: SimpleNamespace(polarrag_tool_limits=SimpleNamespace(max_exhaustive_knowledge_resources=2048)),
        raising=False,
    )

    with pytest.raises(HTTPException) as error:
        await search_space_knowledge_bases(
            "space-a",
            SpaceSearchRequest(query="refund policy"),
            context=SimpleNamespace(
                agent=SimpleNamespace(id="agent-a"),
                user=SimpleNamespace(id="user-a"),
            ),
            session=SimpleNamespace(),
        )

    assert error.value.status_code == 422
    assert error.value.detail == {
        "code": "TOO_MANY_KNOWLEDGE_RESOURCES",
        "message": "Visible knowledge resources exceed the configured limit.",
    }
    assert captured["limit"] == 2048


def test_space_knowledge_base_document_routes_are_registered():
    routes = {(method, route.path) for route in router.routes if hasattr(route, "methods") for method in route.methods}

    prefix = "/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents"
    assert {
        ("GET", prefix),
        ("POST", prefix),
        ("POST", f"{prefix}/rechunk"),
        ("GET", f"{prefix}/{{doc_id}}"),
        ("GET", f"{prefix}/{{doc_id}}/original"),
        ("DELETE", f"{prefix}/{{doc_id}}"),
        ("POST", f"{prefix}/{{doc_id}}/search"),
        ("GET", f"{prefix}/{{doc_id}}/chunks"),
        ("POST", "/v1/spaces/{space_id}/knowledge-bases/{kb_id}/search"),
    } <= routes

    assert (
        "POST",
        "/v1/spaces/{space_id}/documents/batch",
    ) in routes


def test_space_batch_upload_openapi_caps_files_and_kb_ids_at_twenty():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    schema = app.openapi()
    operation = schema["paths"]["/api/v1/spaces/{space_id}/documents/batch"][
        "post"
    ]
    body_ref = operation["requestBody"]["content"]["multipart/form-data"][
        "schema"
    ]["$ref"]
    body = schema["components"]["schemas"][body_ref.rsplit("/", 1)[-1]]

    assert body["properties"]["files"]["minItems"] == 1
    assert body["properties"]["files"]["maxItems"] == 20
    assert body["properties"]["kb_ids"]["minItems"] == 1
    assert body["properties"]["kb_ids"]["maxItems"] == 20
    assert {"201", "207", "422"} <= operation["responses"].keys()


async def test_space_batch_upload_maps_each_file_to_its_kb(monkeypatch):
    assert hasattr(platform_polarrag, "upload_space_documents_batch")
    captured = {}

    async def fake_resolve(_session, *, space_id, kb_ids):
        captured["resolved"] = (space_id, kb_ids)
        return ["resource-a", "resource-b", "resource-a"]

    async def fake_upload(
        response,
        agent_id,
        knowledge_resource_ids,
        files,
        user,
        session,
    ):
        captured["upload"] = (
            response,
            agent_id,
            knowledge_resource_ids,
            [file.filename for file in files],
            user,
            session,
        )
        return {"accepted": [], "failed": []}

    monkeypatch.setattr(
        platform_polarrag,
        "_resolve_space_knowledge_bases",
        fake_resolve,
    )
    monkeypatch.setattr(
        platform_polarrag,
        "upload_documents_batch",
        fake_upload,
    )
    response = Response()
    context = SimpleNamespace(
        agent=SimpleNamespace(id="agent-a"),
        user=SimpleNamespace(id="user-a"),
    )
    session = SimpleNamespace()
    files = [
        UploadFile(file=io.BytesIO(b"a"), filename="a.md"),
        UploadFile(file=io.BytesIO(b"b"), filename="b.md"),
        UploadFile(file=io.BytesIO(b"c"), filename="c.md"),
    ]

    result = await platform_polarrag.upload_space_documents_batch(
        "space-a",
        ["kb-a", "kb-b", "kb-a"],
        files,
        response,
        context,
        session,
    )

    assert result == {"accepted": [], "failed": []}
    assert captured["resolved"] == (
        "space-a",
        ["kb-a", "kb-b", "kb-a"],
    )
    assert captured["upload"] == (
        response,
        "agent-a",
        ["resource-a", "resource-b", "resource-a"],
        ["a.md", "b.md", "c.md"],
        context.user,
        session,
    )


def test_batch_rechunk_rejects_more_than_twenty_documents():
    with pytest.raises(ValidationError, match="List should have at most 20 items"):
        DocumentBatchRechunkRequest(doc_ids=[f"doc-{index}" for index in range(21)])


def test_batch_rechunk_rejects_duplicate_documents():
    with pytest.raises(ValidationError, match="doc_ids must not contain duplicates"):
        DocumentBatchRechunkRequest(doc_ids=["doc-a", "doc-a"])


def test_batch_rechunk_openapi_declares_partial_failure_response():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    paths = app.openapi()["paths"]

    assert "207" in paths["/api/v1/knowledge-bases/{knowledge_resource_id}/documents/rechunk"]["post"]["responses"]
    assert "207" in paths["/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/rechunk"]["post"]["responses"]


async def test_batch_rechunk_continues_after_a_document_failure(monkeypatch):
    calls = []

    async def fake_invoke(_context, name, arguments):
        calls.append((name, arguments))
        if arguments["doc_id"] == "doc-denied":
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text='{"error":"DOCUMENT_PERMISSION_DENIED","message":"denied"}',
                    )
                ],
                isError=True,
            )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text='{"result":{"status":"accepted"}}',
                )
            ]
        )

    monkeypatch.setattr("server.api.platform_polarrag._invoke_tool", fake_invoke)
    response = Response()

    result = await batch_rechunk_knowledge_base_documents(
        "resource-a",
        DocumentBatchRechunkRequest(
            doc_ids=["doc-a", "doc-denied", "doc-b"],
            chunk_strategy="hybrid",
            chunk_max_tokens=512,
        ),
        response,
        SimpleNamespace(),
    )

    assert response.status_code == 207
    assert result == {
        "accepted": [
            {"doc_id": "doc-a", "status": "accepted"},
            {"doc_id": "doc-b", "status": "accepted"},
        ],
        "failed": [
            {
                "doc_id": "doc-denied",
                "status_code": 403,
                "code": "DOCUMENT_PERMISSION_DENIED",
                "message": "denied",
            }
        ],
    }
    assert calls == [
        (
            "doc_rechunk",
            {
                "knowledge_resource_id": "resource-a",
                "doc_id": "doc-a",
                "chunk_strategy": "hybrid",
                "chunk_max_tokens": 512,
                "force": False,
            },
        ),
        (
            "doc_rechunk",
            {
                "knowledge_resource_id": "resource-a",
                "doc_id": "doc-denied",
                "chunk_strategy": "hybrid",
                "chunk_max_tokens": 512,
                "force": False,
            },
        ),
        (
            "doc_rechunk",
            {
                "knowledge_resource_id": "resource-a",
                "doc_id": "doc-b",
                "chunk_strategy": "hybrid",
                "chunk_max_tokens": 512,
                "force": False,
            },
        ),
    ]


async def test_batch_space_rechunk_resolves_the_resource_once(monkeypatch):
    captured = {}

    async def fake_resolve(_session, *, space_id, kb_id):
        captured["selector"] = (space_id, kb_id)
        return "resource-a"

    async def fake_batch(resource_id, body, response, context):
        captured["batch"] = (resource_id, body, response, context)
        return {"accepted": [], "failed": []}

    monkeypatch.setattr(
        "server.api.platform_polarrag._resolve_space_knowledge_base",
        fake_resolve,
    )
    monkeypatch.setattr(
        "server.api.platform_polarrag.batch_rechunk_knowledge_base_documents",
        fake_batch,
    )
    response = Response()
    context = SimpleNamespace()
    body = DocumentBatchRechunkRequest(doc_ids=["doc-a"])

    assert await batch_rechunk_space_knowledge_base_documents(
        "space-a",
        "kb-a",
        body,
        response,
        context,
        SimpleNamespace(),
    ) == {"accepted": [], "failed": []}
    assert captured["selector"] == ("space-a", "kb-a")
    assert captured["batch"] == ("resource-a", body, response, context)


def test_polarrag_mcp_tools_have_native_resource_http_routes():
    routes = {(method, route.path) for route in router.routes if hasattr(route, "methods") for method in route.methods}
    resource = "/v1/knowledge-bases/{knowledge_resource_id}"
    document = f"{resource}/documents/{{doc_id}}"
    native_routes = {
        "list_knowledge_resources": ("GET", "/v1/knowledge-bases"),
        "kb_search": ("POST", "/v1/knowledge-bases/search"),
        "kb_fetch_context": ("GET", f"{document}/chunks/{{chunk_index}}/context"),
        "doc_list_chunks": ("GET", f"{document}/chunks"),
        "doc_find_by_name": ("POST", "/v1/documents/_find"),
        "doc_status": ("GET", document),
        "doc_recall": ("POST", f"{document}/search"),
        "doc_get_original": ("GET", f"{document}/original"),
        "doc_delete": ("DELETE", document),
        "doc_rechunk": ("POST", f"{document}/rechunk"),
        "prepare_document_upload": ("POST", f"{resource}/document-uploads"),
        "complete_document_upload": (
            "POST",
            "/v1/document-uploads/{upload_session_id}/complete",
        ),
    }

    assert set(native_routes) == set(POLARRAG_TOOL_NAMES)
    assert set(native_routes.values()) <= routes


async def test_native_polarrag_http_routes_delegate_to_matching_tools(monkeypatch):
    calls = []

    async def fake_resolve(_session, _targets, *, expected_space_id=None):
        assert expected_space_id is None
        return ["resource-a", "resource-b"]

    async def fake_invoke(_context, name, arguments):
        calls.append((name, arguments))
        payload = '{"items":[],"partial_failures":[]}' if name == "doc_find_by_name" else '{"result":{"ok":true}}'
        return CallToolResult(content=[TextContent(type="text", text=payload)])

    monkeypatch.setattr(
        "server.api.platform_polarrag._resolve_search_targets",
        fake_resolve,
    )
    monkeypatch.setattr("server.api.platform_polarrag._invoke_tool", fake_invoke)
    context = SimpleNamespace()

    assert await find_knowledge_base_documents(
        DocumentFindByNameRequest(
            targets=[KnowledgeBaseSelector(knowledge_resource_id="resource-a")],
            filename="guide.pdf",
        ),
        context,
        SimpleNamespace(),
    ) == {"items": [], "partial_failures": []}
    assert await get_knowledge_base_document_chunk_context(
        "resource-a",
        "doc-a",
        3,
        2,
        context,
    ) == {"ok": True}
    assert await rechunk_knowledge_base_document(
        "resource-a",
        "doc-a",
        DocumentRechunkRequest(chunk_strategy="hybrid", chunk_max_tokens=512),
        context,
    ) == {"ok": True}
    upload = DocumentUploadPrepareRequest(
        filename="guide.pdf",
        file_size_bytes=1024,
        file_md5="a" * 32,
        file_sha256="b" * 64,
        content_type="application/pdf",
    )
    assert await prepare_knowledge_base_document_upload("resource-a", upload, context) == {"ok": True}
    assert await complete_knowledge_base_document_upload("session-a", context) == {"ok": True}

    assert calls == [
        (
            "doc_find_by_name",
            {
                "knowledge_resource_ids": ["resource-a", "resource-b"],
                "filename": "guide.pdf",
                "limit": 20,
            },
        ),
        (
            "kb_fetch_context",
            {
                "knowledge_resource_id": "resource-a",
                "doc_id": "doc-a",
                "chunk_index": 3,
                "window_size": 2,
            },
        ),
        (
            "doc_rechunk",
            {
                "knowledge_resource_id": "resource-a",
                "doc_id": "doc-a",
                "chunk_strategy": "hybrid",
                "chunk_max_tokens": 512,
                "force": False,
            },
        ),
        (
            "prepare_document_upload",
            {
                "knowledge_resource_id": "resource-a",
                "filename": "guide.pdf",
                "file_size_bytes": 1024,
                "file_md5": "a" * 32,
                "file_sha256": "b" * 64,
                "content_type": "application/pdf",
            },
        ),
        ("complete_document_upload", {"upload_session_id": "session-a"}),
    ]


async def test_space_document_routes_resolve_pair_before_delegating(monkeypatch):
    captured = {}

    async def fake_resolve(session, *, space_id, kb_id):
        captured["selector"] = (space_id, kb_id)
        return "resource-a"

    async def fake_list(resource_id, limit, cursor, filename, page, context, session):
        captured["list"] = (
            resource_id,
            limit,
            cursor,
            filename,
            page,
            context,
            session,
        )
        return {"items": [], "next_cursor": None}

    async def fake_search(resource_id, doc_id, body, context):
        captured["search"] = (resource_id, doc_id, body, context)
        return {"items": [], "top_k": body.top_k, "partial_failures": []}

    async def fake_chunks(resource_id, doc_id, offset, limit, context):
        captured["chunks"] = (resource_id, doc_id, offset, limit, context)
        return {
            "items": [],
            "offset": offset,
            "limit": limit,
            "total": 0,
            "next_offset": None,
        }

    async def fake_knowledge_base_search(resource_id, body, context):
        captured["knowledge_base_search"] = (resource_id, body, context)
        return {"items": [], "top_k": body.top_k, "partial_failures": []}

    monkeypatch.setattr(
        "server.api.platform_polarrag._resolve_space_knowledge_base",
        fake_resolve,
    )
    monkeypatch.setattr(
        "server.api.platform_polarrag.list_knowledge_base_documents",
        fake_list,
    )
    monkeypatch.setattr(
        "server.api.platform_polarrag.search_knowledge_base_document",
        fake_search,
    )
    monkeypatch.setattr(
        "server.api.platform_polarrag.list_knowledge_base_document_chunks",
        fake_chunks,
    )
    monkeypatch.setattr(
        "server.api.platform_polarrag.search_knowledge_base",
        fake_knowledge_base_search,
    )
    context = SimpleNamespace()
    session = SimpleNamespace()

    assert await list_space_knowledge_base_documents(
        "space-a",
        "kb-a",
        20,
        None,
        None,
        1,
        context,
        session,
    ) == {"items": [], "next_cursor": None}
    assert await search_space_knowledge_base_document(
        "space-a",
        "kb-a",
        "doc-a",
        SearchRequest(query="refund policy"),
        context,
        session,
    ) == {"items": [], "top_k": 10, "partial_failures": []}
    assert await list_space_knowledge_base_document_chunks(
        "space-a",
        "kb-a",
        "doc-a",
        5,
        20,
        context,
        session,
    ) == {
        "items": [],
        "offset": 5,
        "limit": 20,
        "total": 0,
        "next_offset": None,
    }
    assert await search_space_knowledge_base(
        "space-a",
        "kb-a",
        SearchRequest(query="refund policy"),
        context,
        session,
    ) == {"items": [], "top_k": 10, "partial_failures": []}
    assert captured["selector"] == ("space-a", "kb-a")
    assert captured["list"][:4] == ("resource-a", 20, None, None)
    assert captured["search"][0:2] == ("resource-a", "doc-a")
    assert captured["chunks"] == ("resource-a", "doc-a", 5, 20, context)
    assert captured["knowledge_base_search"][0] == "resource-a"


async def test_space_search_without_ids_uses_visible_agent_scope(monkeypatch):
    from server.api import platform_polarrag as api

    scope = object()
    captured = {}

    async def resolve_space(session, space_id):
        assert space_id == "space-a"
        return SimpleNamespace(knowledge_space_id="space-key")

    async def resolve_scope(session, agent_id, user_id):
        assert (agent_id, user_id) == ("agent-a", "user-a")
        return scope

    async def visible(session, user, **kwargs):
        assert kwargs["resource_scope"] is scope
        assert kwargs["knowledge_space_id"] == "space-key"
        return [SimpleNamespace(id="visible-a"), SimpleNamespace(id="visible-b")], False, True

    async def invoke(context, name, arguments):
        captured.update(arguments)
        return CallToolResult(content=[TextContent(type="text", text='{"results":[],"partial_failures":[]}')], isError=False)

    monkeypatch.setattr(api, "_resolve_space", resolve_space)
    monkeypatch.setattr(api, "resolve_polarrag_resource_scope_for_agent", resolve_scope)
    monkeypatch.setattr(api, "list_visible_knowledge_resources_cursor_page", visible)
    monkeypatch.setattr(api, "_invoke_tool", invoke)
    result = await api.search_space_knowledge_bases("space-a", SpaceSearchRequest(query="query"), context=SimpleNamespace(agent=SimpleNamespace(id="agent-a"), user=SimpleNamespace(id="user-a")), session=object())
    assert captured["knowledge_resource_ids"] == ["visible-a", "visible-b"]
    assert result["partial_failures"] == []


@pytest.mark.parametrize("by_space", [False, True])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("force", [None, False, True])
async def test_rechunk_http_force_contract(monkeypatch, by_space, batch, force):
    import httpx
    from server.db.engine import get_session

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[platform_polarrag.get_platform_access_context] = lambda: SimpleNamespace()
    app.dependency_overrides[get_session] = lambda: SimpleNamespace()
    calls = []

    async def resolve(*args, **kwargs):
        return "resource-a"

    async def invoke(context, name, arguments):
        calls.append(arguments)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"result": {
            "doc_id": arguments["doc_id"], "status": "RECHUNKING",
        }}))])

    monkeypatch.setattr(platform_polarrag, "_resolve_space_knowledge_base", resolve)
    monkeypatch.setattr(platform_polarrag, "_invoke_tool", invoke)
    prefix = "/api/v1/spaces/space-a/knowledge-bases/kb-a" if by_space else "/api/v1/knowledge-bases/resource-a"
    path = prefix + ("/documents/rechunk" if batch else "/documents/doc-a/rechunk")
    body = {"doc_ids": ["doc-a", "doc-b"]} if batch else {}
    if force is not None:
        body["force"] = force
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(path, json=body)
    assert response.status_code == 202, response.text
    assert len(calls) == (2 if batch else 1)
    assert all(call["force"] is bool(force) for call in calls)


@pytest.mark.parametrize("space_route", [False, True])
@pytest.mark.parametrize("conflict", [False, True])
async def test_context_http_forwards_pin_and_original_query_and_preserves_409(
    monkeypatch, space_route, conflict,
):
    import httpx
    from server.db.engine import get_session

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[platform_polarrag.get_platform_access_context] = lambda: SimpleNamespace()
    app.dependency_overrides[get_session] = lambda: SimpleNamespace()
    captured = {}

    async def resolve(*_args, **_kwargs):
        return "resource-a"

    async def invoke(_context, name, arguments):
        assert name == "kb_fetch_context"
        captured.update(arguments)
        payload = (
            {"error": "citation_source_changed", "message": "The document source changed."}
            if conflict else {"result": {"hits": {"hits": [{"_source": {
                "doc_id": "doc-a", "chunk_index": 7, "doc_generation": 3, "text": "fresh context",
            }}]}}}
        )
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))], isError=conflict,
        )

    monkeypatch.setattr(platform_polarrag, "_resolve_space_knowledge_base", resolve)
    monkeypatch.setattr(platform_polarrag, "_invoke_tool", invoke)
    prefix = "/api/v1/spaces/space-a/knowledge-bases/kb-a" if space_route else "/api/v1/knowledge-bases/resource-a"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get(
            f"{prefix}/documents/doc-a/chunks/1/context",
            params={"expected_doc_generation": 0, "query": "退款 指南", "window_size": 2},
        )
    assert captured == {
        "knowledge_resource_id": "resource-a", "doc_id": "doc-a", "chunk_index": 1,
        "window_size": 2, "expected_doc_generation": 0, "query": "退款 指南",
    }
    assert response.status_code == (409 if conflict else 200)
    if conflict:
        assert response.json()["detail"]["code"] == "citation_source_changed"
    else:
        source = response.json()["hits"]["hits"][0]["_source"]
        assert (source["chunk_index"], source["doc_generation"]) == (7, 3)


@pytest.mark.parametrize("generation", ["-1", "2147483648", "true", "invalid"])
async def test_context_http_rejects_invalid_generation_before_tool(monkeypatch, generation):
    import httpx

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[platform_polarrag.get_platform_access_context] = lambda: SimpleNamespace()
    invoke = AsyncMock()
    monkeypatch.setattr(platform_polarrag, "_invoke_tool", invoke)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get(
            "/api/v1/knowledge-bases/resource-a/documents/doc-a/chunks/1/context",
            params={"expected_doc_generation": generation},
        )
    assert response.status_code == 422
    invoke.assert_not_awaited()
