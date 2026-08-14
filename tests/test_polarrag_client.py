from __future__ import annotations

import base64
import json
import os

import httpx
import pytest

from server.core.crypto import encrypt
from server.models import PolarRAGInstance, PolarRAGInstanceStatus
from server.polarrag.client import HttpPolarRAGClient, client_from_instance
from server.polarrag.contracts import (
    PolarRAGErrorCode,
    PolarRAGUpstreamError,
)


@pytest.fixture
def encryption_key(monkeypatch):
    key = os.urandom(32)
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", base64.b64encode(key).decode())
    return key


def _instance(encryption_key: bytes) -> PolarRAGInstance:
    return PolarRAGInstance(
        id="instance-1",
        name="RAG",
        scheme="https",
        host="rag.example.test",
        port=9443,
        username_ciphertext=encrypt("shared-user", key=encryption_key),
        password_ciphertext=encrypt("shared-password", key=encryption_key),
        tls_verify=True,
        status=PolarRAGInstanceStatus.ACTIVE,
        created_by="admin-1",
    )


async def test_client_from_instance_decrypts_credentials_and_uses_fixed_route(
    encryption_key,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"hits": {"hits": []}})

    client = client_from_instance(
        _instance(encryption_key),
        transport=httpx.MockTransport(handler),
    )
    await client.search(
        "space-a",
        "kb-a",
        query="acl",
        search_mode="balanced",
        top_k=10,
        min_score=None,
        reranker=False,
        acl_context={
            "identity_domain": "tenant-a",
            "principals": [{"provider": "feishu", "type": "user", "id": "ou-1"}],
        },
    )

    assert captured["url"] == (
        "https://rag.example.test:9443/_plugins/_polar_rag/spaces/space-a/search"
    )
    expected_auth = base64.b64encode(
        b"shared-user:shared-password"
    ).decode()
    assert captured["authorization"] == f"Basic {expected_auth}"
    assert captured["body"] == {
        "query_text": "acl",
        "kb_id": "kb-a",
        "search_mode": "balanced",
        "size": 10,
        "reranker": False,
        "acl_context": {
            "identity_domain": "tenant-a",
            "principals": [{"provider": "feishu", "type": "user", "id": "ou-1"}],
        },
    }


def test_client_from_instance_sanitizes_credential_decryption_failure(
    encryption_key,
    monkeypatch,
) -> None:
    def fail_decrypt(_ciphertext: str) -> str:
        raise RuntimeError("secret ciphertext sentinel")

    monkeypatch.setattr("server.polarrag.client.decrypt", fail_decrypt)

    with pytest.raises(PolarRAGUpstreamError) as caught:
        client_from_instance(_instance(encryption_key))

    assert caught.value.code.value == "POLARRAG_CREDENTIAL_UNAVAILABLE"
    assert "sentinel" not in str(caught.value)


async def test_catalog_clients_follow_opaque_pagination_and_parse_owner() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/spaces/_list"):
            if body.get("cursor") == "space-next":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "space_id": "space-b",
                                "space_name": "Space B",
                                "status": "ACTIVE",
                                "identity_domain": "tenant-b",
                                "updated_at": "2026-07-31T02:00:00Z",
                            }
                        ],
                        "next_cursor": None,
                        "has_more": False,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "space_id": "space-a",
                            "space_name": "Space A",
                            "status": "ACTIVE",
                            "identity_domain": "tenant-a",
                            "oss_bucket": "tenant-a-documents",
                            "oss_endpoint": "oss-cn-hangzhou.aliyuncs.com",
                            "updated_at": "2026-07-31T01:00:00Z",
                        }
                    ],
                    "next_cursor": "space-next",
                    "has_more": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "space_id": "space-a",
                        "kb_id": "kb-a",
                        "name": "kb-a",
                        "kb_type": "PERSONAL",
                        "status": "ACTIVE",
                        "identity_domain": "tenant-a",
                        "owner": {
                            "provider": "feishu",
                            "type": "user",
                            "id": "ou-owner",
                        },
                        "updated_at": "2026-07-31T03:00:00Z",
                    }
                ],
                "next_cursor": None,
                "has_more": False,
            },
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )

    spaces = await client.list_spaces()
    knowledge_bases = await client.list_knowledge_bases("space-a")

    assert [space.space_id for space in spaces] == ["space-a", "space-b"]
    assert spaces[0].oss_bucket == "tenant-a-documents"
    assert spaces[0].oss_endpoint == "oss-cn-hangzhou.aliyuncs.com"
    assert spaces[1].oss_bucket is None
    assert spaces[1].oss_endpoint is None
    assert knowledge_bases[0].owner == {
        "provider": "feishu",
        "type": "user",
        "id": "ou-owner",
    }
    assert requests == [
        (
            "/_plugins/_polar_rag/spaces/_list",
            {"size": 100, "statuses": ["ACTIVE"]},
        ),
        (
            "/_plugins/_polar_rag/spaces/_list",
            {
                "size": 100,
                "statuses": ["ACTIVE"],
                "cursor": "space-next",
            },
        ),
        (
            "/_plugins/_polar_rag/spaces/space-a/knowledge_bases/_list",
            {"size": 100, "statuses": ["ACTIVE"]},
        ),
    ]


async def test_client_lists_unclaimed_kbs_and_claims_with_canonical_owner() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/_claim"):
            return httpx.Response(
                200,
                json={
                    "space_id": "space-a",
                    "kb_id": "kb-a",
                    "kb_type": "PERSONAL",
                    "status": "ACTIVE",
                    "owner_principal_token": "sensitive-owner-token",
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "space_id": "space-a",
                        "kb_id": "kb-a",
                        "name": "KB A",
                        "kb_type": "PERSONAL",
                        "status": "UNCLAIMED",
                        "identity_domain": "tenant-a",
                        "owner": None,
                        "updated_at": "2026-08-05T09:41:51.379Z",
                    }
                ],
                "next_cursor": None,
                "has_more": False,
            },
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )
    unclaimed = await client.list_unclaimed_knowledge_bases("space-a")
    result = await client.claim_knowledge_base(
        "space-a",
        "kb-a",
        owner="alice",
    )

    assert [(record.kb_id, record.status, record.owner) for record in unclaimed] == [
        ("kb-a", "UNCLAIMED", None)
    ]
    assert result is None
    assert "sensitive-owner-token" not in repr(result)
    assert requests == [
        (
            "/_plugins/_polar_rag/spaces/space-a/knowledge_bases/_list",
            {"size": 100, "statuses": ["UNCLAIMED"]},
        ),
        (
            "/_plugins/_polar_rag/spaces/space-a/knowledge_bases/kb-a/_claim",
            {"owner": "alice"},
        ),
    ]


async def test_client_rejects_unclaimed_public_kb() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "space_id": "space-a",
                        "kb_id": "public-kb",
                        "name": "Public KB",
                        "kb_type": "PUBLIC",
                        "status": "UNCLAIMED",
                        "identity_domain": "tenant-a",
                        "owner": None,
                    }
                ],
                "next_cursor": None,
                "has_more": False,
            },
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PolarRAGUpstreamError) as captured:
        await client.list_unclaimed_knowledge_bases("space-a")

    assert captured.value.code == PolarRAGErrorCode.INVALID_RESPONSE


async def test_capability_check_uses_polarrag_version_marker_route() -> None:
    paths: list[str] = []
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.content:
            payloads.append(json.loads(request.content))
        if request.url.path == "/":
            return httpx.Response(200, json={"version": {"number": "3"}})
        if request.url.path == "/_plugins/_polar_rag/_version":
            return httpx.Response(200, json={"version": "1.0.0"})
        if request.url.path == "/_plugins/_polar_rag/spaces/_list":
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "next_cursor": None,
                    "has_more": False,
                },
            )
        if request.url.path.endswith("/knowledge_bases/_list"):
            return httpx.Response(
                404,
                json={"error_type": "space_not_found"},
            )
        return httpx.Response(404, json={"error": "not found"})

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )

    capabilities = await client.check_capabilities()

    assert capabilities.version == "1.0.0"
    assert capabilities.space_catalog is True
    assert capabilities.knowledge_base_catalog is True
    assert paths == [
        "/",
        "/_plugins/_polar_rag/_version",
        "/_plugins/_polar_rag/spaces/_list",
        (
            "/_plugins/_polar_rag/spaces/__pas_capability_probe__"
            "/knowledge_bases/_list"
        ),
    ]
    assert payloads == [
        {"size": 1, "statuses": ["ACTIVE"]},
        {"size": 1, "statuses": ["ACTIVE"]},
    ]


async def test_catalog_rejects_stalled_cursor_and_malformed_owner() -> None:
    responses = iter(
        [
            {
                "items": [
                    {
                        "space_id": "space-a",
                        "space_name": "Space A",
                        "status": "ACTIVE",
                        "identity_domain": "tenant-a",
                    }
                ],
                "next_cursor": "same",
                "has_more": True,
            },
            {
                "items": [],
                "next_cursor": "same",
                "has_more": True,
            },
        ]
    )

    def stalled(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(stalled),
    )
    with pytest.raises(PolarRAGUpstreamError) as cursor_error:
        await client.list_spaces()
    assert cursor_error.value.code == PolarRAGErrorCode.INVALID_RESPONSE

    def malformed_owner(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "space_id": "space-a",
                        "kb_id": "kb-a",
                        "name": "kb-a",
                        "kb_type": "PERSONAL",
                        "status": "ACTIVE",
                        "identity_domain": "tenant-a",
                        "owner": None,
                        "updated_at": "2026-07-31T03:00:00Z",
                    }
                ],
                "next_cursor": None,
                "has_more": False,
            },
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(malformed_owner),
    )
    with pytest.raises(PolarRAGUpstreamError) as owner_error:
        await client.list_knowledge_bases("space-a")
    assert owner_error.value.code == PolarRAGErrorCode.INVALID_RESPONSE


async def test_upstream_errors_are_classified_without_response_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            text="secret upstream diagnostics and password=do-not-leak",
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PolarRAGUpstreamError) as captured:
        await client.document_info(
            "space-a",
            "doc-a",
            acl_context={"identity_domain": "tenant-a", "principals": []},
        )

    assert captured.value.code == PolarRAGErrorCode.UNAVAILABLE
    assert captured.value.retryable is True
    assert "do-not-leak" not in str(captured.value)


async def test_search_maps_missing_reranker_to_sanitized_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "type": "illegal_argument_exception",
                    "reason": (
                        "reranker is not configured for space: space-a"
                    ),
                    "root_cause": [
                        {
                            "reason": (
                                "password=do-not-leak; request_id=private"
                            )
                        }
                    ],
                },
                "status": 400,
            },
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PolarRAGUpstreamError) as captured:
        await client.search(
            "space-a",
            "kb-a",
            query="acl",
            search_mode="balanced",
            top_k=10,
            min_score=None,
            reranker=True,
            acl_context={
                "identity_domain": "tenant-a",
                "principals": [],
            },
        )

    assert captured.value.code == PolarRAGErrorCode.RERANKER_NOT_CONFIGURED
    assert captured.value.retryable is False
    assert captured.value.status_code == 400
    assert "do-not-leak" not in str(captured.value)


async def test_search_keeps_unrelated_400_as_invalid_response() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"reason": "query_text is required"}},
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PolarRAGUpstreamError) as captured:
        await client.search(
            "space-a",
            "kb-a",
            query="acl",
            search_mode="balanced",
            top_k=10,
            min_score=None,
            reranker=True,
            acl_context={
                "identity_domain": "tenant-a",
                "principals": [],
            },
        )

    assert captured.value.code == PolarRAGErrorCode.INVALID_RESPONSE


async def test_list_documents_uses_acl_filtered_keyset_page() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        return httpx.Response(
            200,
            json={
                "documents": [
                    {
                        "doc_id": "doc-b",
                        "space_id": "space-a",
                        "kb_id": "kb-a",
                        "filename": "guide.md",
                        "status": "COMPLETED",
                    }
                ],
                "has_more": True,
                "next_after_doc_id": "doc-b",
            },
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )
    acl_context = {
        "identity_domain": "tenant-a",
        "principals": [
            {"provider": "feishu", "type": "user", "id": "ou-user"}
        ],
    }

    page = await client.list_documents(
        "space-a",
        kb_id="kb-a",
        size=20,
        after_doc_id="doc-a",
        acl_context=acl_context,
    )

    assert page["next_after_doc_id"] == "doc-b"
    assert requests == [
        (
            "/_plugins/_polar_rag/spaces/space-a/knowledge_bases/"
            "kb-a/documents/_list",
            {
                "size": 20,
                "after_doc_id": "doc-a",
                "acl_context": acl_context,
            },
        )
    ]


async def test_search_maps_missing_kb_to_sanitized_resource_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            text="kb-a does not exist; password=do-not-leak",
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PolarRAGUpstreamError) as captured:
        await client.search(
            "space-a",
            "kb-a",
            query="acl",
            search_mode="balanced",
            top_k=10,
            min_score=None,
            reranker=False,
            acl_context={
                "identity_domain": "tenant-a",
                "principals": [],
            },
        )

    assert captured.value.code == (
        PolarRAGErrorCode.KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE
    )
    assert "do-not-leak" not in str(captured.value)


async def test_document_mutations_use_protected_space_routes() -> None:
    requests: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.method, request.url.path, body))
        if request.method == "DELETE":
            return httpx.Response(
                200,
                json={
                    "doc_id": "doc-a",
                    "task_id": "delete-task-a",
                    "status": "DELETING",
                    "error_message": None,
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "doc_id": "doc-a",
                "space_id": "space-a",
                "kb_id": "kb-a",
                "status": "RECHUNKING",
                "noop": False,
                "previous_generation": 0,
                "target_generation": 1,
                "task_id": "rechunk-task-a",
            },
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )
    acl_context = {
        "identity_domain": "tenant-a",
        "principals": [
            {"provider": "feishu", "type": "user", "id": "ou-user"}
        ],
    }

    deleted = await client.delete_document(
        "space-a",
        "doc-a",
        acl_context=acl_context,
    )
    rechunked = await client.rechunk_document(
        "space-a",
        "doc-a",
        chunk_strategy="hybrid",
        chunk_max_tokens=512,
        acl_context=acl_context,
    )

    assert deleted == {
        "doc_id": "doc-a",
        "task_id": "delete-task-a",
        "status": "DELETING",
        "error_message": None,
    }
    assert rechunked["target_generation"] == 1
    assert requests == [
        (
            "DELETE",
            "/_plugins/_polar_rag/spaces/space-a/managed_documents/doc-a",
            {"acl_context": acl_context},
        ),
        (
            "PUT",
            "/_plugins/_polar_rag/spaces/space-a/documents/doc-a/chunk_strategy",
            {
                "chunk_strategy": "hybrid",
                "chunk_max_tokens": 512,
                "acl_context": acl_context,
            },
        ),
    ]


async def test_submit_document_uses_user_derived_managed_route() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "idempotent": False,
                "document": {
                    "doc_id": "doc-a",
                    "space_id": "space-a",
                    "kb_id": "kb-a",
                    "filename": "guide.md",
                    "status": "DISPATCHED",
                },
            },
        )

    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(handler),
    )
    acl_context = {
        "identity_domain": "tenant-a",
        "principals": [
            {"provider": "feishu", "type": "user", "id": "ou-user"}
        ],
        "actor": {"provider": "feishu", "type": "user", "id": "ou-user"},
    }

    submitted = await client.submit_document(
        "space-a",
        "kb-a",
        oss_path="oss://tenant-a-documents/pas/doc-a/guide.md",
        filename="guide.md",
        file_type="md",
        file_md5="0cc175b9c0f1b6a831c399e269772661",
        file_size_bytes=1,
        metadata={"sha256": "ca978112ca1bbdcafac231b39a23dc4d"},
        acl_context=acl_context,
    )

    assert submitted == {
        "doc_id": "doc-a",
        "space_id": "space-a",
        "kb_id": "kb-a",
        "filename": "guide.md",
        "status": "DISPATCHED",
    }
    assert captured == {
        "method": "POST",
        "path": "/_plugins/_polar_rag/spaces/space-a/managed_documents",
        "body": {
            "kb_id": "kb-a",
            "oss_path": "oss://tenant-a-documents/pas/doc-a/guide.md",
            "filename": "guide.md",
            "file_type": "md",
            "file_md5": "0cc175b9c0f1b6a831c399e269772661",
            "file_size_bytes": 1,
            "metadata": {
                "sha256": "ca978112ca1bbdcafac231b39a23dc4d"
            },
            "acl_context": acl_context,
            "acl": {"mode": "POLARRAG_DERIVED"},
        },
    }


@pytest.mark.parametrize(
    ("response_body", "expected_code"),
    [
        (
            {
                "error_type": "kb_upload_forbidden",
                "error": "password=do-not-leak; private principal",
            },
            "POLARRAG_DOCUMENT_UPLOAD_FORBIDDEN",
        ),
        (
            {"error": "unknown authorization failure password=do-not-leak"},
            "POLARRAG_AUTH_FAILED",
        ),
    ],
)
async def test_submit_document_distinguishes_acl_denial_from_service_auth(
    response_body,
    expected_code,
) -> None:
    client = HttpPolarRAGClient(
        base_url="https://rag.example.test:9443",
        username="user",
        password="password",
        tls_verify=True,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(403, json=response_body)
        ),
    )

    with pytest.raises(PolarRAGUpstreamError) as captured:
        await client.submit_document(
            "space-a",
            "kb-a",
            oss_path="oss://tenant-a-documents/pas/guide.md",
            filename="guide.md",
            file_type="md",
            file_md5="0cc175b9c0f1b6a831c399e269772661",
            file_size_bytes=1,
            metadata={"sha256": "ca978112ca1bbdcafac231b39a23dc4d"},
            acl_context={
                "identity_domain": "tenant-a",
                "principals": [
                    {
                        "provider": "feishu",
                        "type": "user",
                        "id": "ou-user",
                    }
                ],
                "actor": {
                    "provider": "feishu",
                    "type": "user",
                    "id": "ou-user",
                },
            },
        )

    assert captured.value.code.value == expected_code
    assert captured.value.retryable is False
    assert "do-not-leak" not in str(captured.value)
