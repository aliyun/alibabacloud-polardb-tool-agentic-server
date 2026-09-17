from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException, Response
from mcp.server.auth.provider import AccessToken
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from server.api.platform_oauth import PlatformAccessContext
from server.api.platform_polarrag import (
    DocumentFindByNameRequest,
    DocumentBatchRechunkRequest,
    DocumentRechunkRequest,
    DocumentUploadPrepareRequest,
    KnowledgeBaseSearchRequest,
    KnowledgeBaseSelector,
    SearchRequest,
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
    get_knowledge_base_document_chunk_context,
    list_knowledge_base_document_chunks,
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
from server.api.polarrag_documents import _visible_document_page, _visible_documents
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
                "total": {"value": 2},
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
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps({"result": payload}))]
        )

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
        lambda: SimpleNamespace(
            polarrag_tool_limits=SimpleNamespace(
                max_exhaustive_knowledge_resources=2048
            )
        ),
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
    routes = {
        (method, route.path)
        for route in router.routes
        if hasattr(route, "methods")
        for method in route.methods
    }

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

    assert "207" in paths[
        "/api/v1/knowledge-bases/{knowledge_resource_id}/documents/rechunk"
    ]["post"]["responses"]
    assert "207" in paths[
        "/api/v1/spaces/{space_id}/knowledge-bases/{kb_id}/documents/rechunk"
    ]["post"]["responses"]


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
            },
        ),
        (
            "doc_rechunk",
            {
                "knowledge_resource_id": "resource-a",
                "doc_id": "doc-denied",
                "chunk_strategy": "hybrid",
                "chunk_max_tokens": 512,
            },
        ),
        (
            "doc_rechunk",
            {
                "knowledge_resource_id": "resource-a",
                "doc_id": "doc-b",
                "chunk_strategy": "hybrid",
                "chunk_max_tokens": 512,
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
    routes = {
        (method, route.path)
        for route in router.routes
        if hasattr(route, "methods")
        for method in route.methods
    }
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
        payload = (
            '{"items":[],"partial_failures":[]}'
            if name == "doc_find_by_name"
            else '{"result":{"ok":true}}'
        )
        return CallToolResult(
            content=[TextContent(type="text", text=payload)]
        )

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
    assert await prepare_knowledge_base_document_upload(
        "resource-a", upload, context
    ) == {"ok": True}
    assert await complete_knowledge_base_document_upload("session-a", context) == {
        "ok": True
    }

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

    async def fake_list(resource_id, limit, cursor, filename, context, session):
        captured["list"] = (resource_id, limit, cursor, filename, context, session)
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
