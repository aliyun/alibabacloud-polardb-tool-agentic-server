from __future__ import annotations

import base64
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.core.crypto import encrypt
from server.models import (
    Agent,
    AuthProvider,
    Base,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalType,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    PolarRAGUploadCleanup,
    PolarRAGUploadSession,
    PolarRAGUploadStatus,
    User,
)
from server.polarrag.contracts import (
    PolarRAGErrorCode,
    PolarRAGUpstreamError,
)


class FakeSearchClient:
    async def search(self, _space_id, kb_id, **_kwargs):
        if kb_id == "retryable":
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.UNAVAILABLE,
                retryable=True,
            )
        if kb_id == "missing":
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE,
            )
        score = 0.9 if kb_id == "high" else 0.4
        return {
            "hits": {
                "hits": [
                    {
                        "_score": score,
                        "_source": {
                            "doc_id": f"doc-{kb_id}",
                            "text": f"text-{kb_id}",
                            "filename": f"{kb_id}.pdf",
                            "chunk_index": 1,
                            "metadata": {
                                "publish_year": 2026,
                                "acl_read_tokens": ["secret"],
                            },
                        },
                    }
                ]
            }
        }


class RerankerNotConfiguredClient:
    async def search(self, _space_id, _kb_id, **_kwargs):
        raise PolarRAGUpstreamError(
            PolarRAGErrorCode.RERANKER_NOT_CONFIGURED,
            status_code=400,
        )


class FakeDocumentClient:
    def __init__(self, *, kb_id: str = "low") -> None:
        self.kb_id = kb_id
        self.calls: list[str] = []
        self.mutation_kwargs: list[dict] = []

    async def document_info(self, space_id, doc_id, **_kwargs):
        self.calls.append("document_info")
        return {
            "doc_id": doc_id,
            "space_id": space_id,
            "kb_id": self.kb_id,
            "filename": "guide.pdf",
            "file_type": "pdf",
            "file_size_bytes": 123,
            "file_md5": "abc123",
            "oss_path": "oss://bucket/guide.pdf",
            "status": "COMPLETED",
            "metadata": {"private": "value"},
        }

    async def fetch_context(self, _space_id, _doc_id, **_kwargs):
        self.calls.append("fetch_context")
        return {"hits": {"hits": []}}

    async def delete_document(self, _space_id, doc_id, **kwargs):
        self.calls.append("delete_document")
        self.mutation_kwargs.append(kwargs)
        return {"doc_id": doc_id, "task_id": "delete-task", "status": "DELETING"}

    async def rechunk_document(self, _space_id, doc_id, **kwargs):
        self.calls.append("rechunk_document")
        self.mutation_kwargs.append(kwargs)
        return {
            "success": True,
            "doc_id": doc_id,
            "status": "RECHUNKING",
            "noop": False,
            "target_generation": 1,
        }


@pytest.fixture
async def seeded(monkeypatch):
    key = os.urandom(32)
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(key).decode(),
    )
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        admin = User(external_id="admin", display_name="Admin", auth_provider=AuthProvider.BUILTIN)
        user = User(external_id="user", display_name="User", auth_provider=AuthProvider.BUILTIN)
        session.add_all([admin, user])
        await session.flush()
        session.add(
            EnterprisePrincipalAssignment.create(
                pas_user_id=user.id,
                identity_domain="tenant-a",
                provider="feishu",
                principal_type=EnterprisePrincipalType.USER,
                principal_id="ou-user",
                source=EnterprisePrincipalSource.ADMIN_MANAGED,
            )
        )
        instance = PolarRAGInstance(
            name="RAG",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext=encrypt("user", key=key),
            password_ciphertext=encrypt("password", key=key),
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id,
            space_id="space-a",
            name="Space A",
            identity_domain="tenant-a",
            oss_bucket="tenant-a-documents",
            oss_endpoint="https://oss-cn-hangzhou.aliyuncs.com",
            oss_access_key_id_ciphertext=encrypt("oss-ak", key=key),
            oss_access_key_secret_ciphertext=encrypt("oss-sk", key=key),
            oss_object_prefix="pas/documents",
            oss_config_validated=True,
            enabled=True,
        )
        session.add(space)
        await session.flush()
        resources = []
        for kb_id in ("low", "high", "retryable", "missing"):
            resource = KnowledgeResource(
                knowledge_space_id=space.knowledge_space_id,
                polarrag_instance_id=instance.id,
                space_id=space.space_id,
                kb_id=kb_id,
                name=kb_id.title(),
                kb_type="PUBLIC",
                identity_domain="tenant-a",
                binding_mode=KnowledgeBindingMode.DOMAIN,
                sync_status=KnowledgeResourceSyncStatus.ACTIVE,
                enabled=True,
            )
            session.add(resource)
            resources.append(resource)
        await session.commit()
        yield session, user, resources
    await engine.dispose()


class FakeMultipartStore:
    def __init__(self) -> None:
        self.key: str | None = None
        self.upload_id = "oss-upload-id"
        self.parts: list[SimpleNamespace] = []
        self.completed: list[tuple[str, str, list[SimpleNamespace]]] = []
        self.aborted: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.fail_complete = False

    async def initiate_multipart(self, key: str) -> str:
        self.key = key
        return self.upload_id

    def sign_part_url(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        *,
        expires_seconds: int,
    ) -> str:
        assert key == self.key
        assert upload_id == self.upload_id
        assert expires_seconds == 900
        return f"https://upload.example.test/part/{part_number}"

    async def list_multipart_parts(
        self, key: str, upload_id: str
    ) -> list[SimpleNamespace]:
        assert key == self.key
        assert upload_id == self.upload_id
        return self.parts

    async def complete_multipart(
        self,
        key: str,
        upload_id: str,
        parts: list[SimpleNamespace],
    ) -> None:
        self.completed.append((key, upload_id, parts))
        if self.fail_complete:
            raise RuntimeError("sensitive OSS failure")

    async def abort_multipart(self, key: str, upload_id: str) -> None:
        self.aborted.append((key, upload_id))

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


class FakeUploadClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def submit_document(self, space_id, kb_id, **kwargs):
        self.calls.append({"space_id": space_id, "kb_id": kb_id, **kwargs})
        if self.fail:
            raise RuntimeError("sensitive upstream failure")
        return {"doc_id": "polarrag-doc-id", "status": "DISPATCHED"}


async def _upload_agent(session) -> Agent:
    agent = Agent(name=f"upload-agent-{uuid.uuid4().hex}")
    session.add(agent)
    await session.flush()
    return agent


async def test_kb_search_merges_scores_and_preserves_retryable_partial_failure(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import handle_kb_search

    session, user, resources = seeded
    result = await handle_kb_search(
        session,
        user,
        query="acl",
        knowledge_resource_ids=[resource.id for resource in resources],
        top_k=10,
        client_factory=lambda _instance: FakeSearchClient(),
    )
    payload = json.loads(result.content[0].text)

    assert result.isError is False
    assert [item["doc_id"] for item in payload["results"]] == [
        "doc-high",
        "doc-low",
    ]
    assert payload["successful_searches"] == 2
    assert payload["failed_searches"] == 2
    assert payload["partial_failures"] == [
        {
            "knowledge_resource_id": resources[2].id,
            "error": "POLARRAG_UNAVAILABLE",
        },
        {
            "knowledge_resource_id": resources[3].id,
            "error": "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE",
        },
    ]
    assert payload["results"][0]["metadata_hints"] == {
        "publish_year": 2026
    }


async def test_kb_search_returns_sanitized_reranker_configuration_error(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import handle_kb_search

    session, user, resources = seeded
    result = await handle_kb_search(
        session,
        user,
        query="acl",
        knowledge_resource_ids=[resources[0].id],
        reranker=True,
        client_factory=lambda _instance: RerankerNotConfiguredClient(),
    )
    payload = json.loads(result.content[0].text)

    assert result.isError is True
    assert payload == {
        "error": "RERANKER_NOT_CONFIGURED",
        "message": (
            "Reranking is not configured for this knowledge Space. "
            "Ask an administrator to configure it or retry without reranking."
        ),
    }


async def test_kb_search_maps_client_construction_failure_to_tool_error(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import handle_kb_search

    session, user, resources = seeded

    def unavailable_client(_instance):
        raise PolarRAGUpstreamError(PolarRAGErrorCode.INVALID_RESPONSE)

    result = await handle_kb_search(
        session,
        user,
        query="acl",
        knowledge_resource_ids=[resources[0].id],
        client_factory=unavailable_client,
    )

    assert result.isError is True
    assert json.loads(result.content[0].text) == {
        "error": "POLARRAG_INVALID_RESPONSE",
        "message": "POLARRAG_INVALID_RESPONSE",
    }


async def test_polarrag_audit_context_has_coordinates_without_principal_ids(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import _build_audit_client_info

    session, user, resources = seeded
    payload = {
        "returned_count": 2,
        "successful_searches": 2,
        "failed_searches": 1,
        "partial_failures": [{"error": "POLARRAG_UNAVAILABLE"}],
    }

    context = await _build_audit_client_info(
        session,
        user,
        [resource.id for resource in resources],
        payload,
        error_code=None,
    )

    assert context == {
        "knowledge_resource_ids": [resource.id for resource in resources],
        "polarrag_instance_ids": [resources[0].polarrag_instance_id],
        "space_ids": ["space-a"],
        "kb_ids": ["high", "low", "missing", "retryable"],
        "providers": ["feishu"],
        "principal_count": 1,
        "polarrag_status": "success",
        "hit_count": 2,
        "successful_searches": 2,
        "failed_searches": 1,
        "partial_failure_count": 1,
    }
    assert "ou-user" not in json.dumps(context)


def test_polarrag_tool_catalog_has_exact_safe_schema(monkeypatch) -> None:
    from server.mcp.transport import _build_mcp_server
    from server.mcp.tools.polarrag import POLARRAG_TOOL_NAMES

    monkeypatch.setenv(
        "PAS_DATABASE_URL",
        "sqlite+aiosqlite:///:memory:",
    )
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode(),
    )
    server = _build_mcp_server()
    manager = server._tool_manager
    forbidden = {
        "user_id",
        "provider",
        "identity_domain",
        "principal",
        "principals",
        "acl_context",
        "endpoint",
        "username",
        "password",
        "dsl",
        "file_path",
        "file_content",
        "oss_path",
        "bucket",
        "object_key",
    }

    assert POLARRAG_TOOL_NAMES == {
        "list_knowledge_resources",
        "kb_search",
        "kb_fetch_context",
        "doc_find_by_name",
        "doc_status",
        "doc_recall",
        "doc_get_original",
        "doc_delete",
        "doc_rechunk",
        "prepare_document_upload",
        "complete_document_upload",
    }
    for name in POLARRAG_TOOL_NAMES:
        tool = manager.get_tool(name)
        assert tool is not None
        assert tool.parameters["additionalProperties"] is False
        assert not forbidden.intersection(tool.parameters["properties"])
    assert (
        "knowledge_resource_ids"
        in manager.get_tool("kb_search").parameters["required"]
    )

    descriptions = {
        name: manager.get_tool(name).description
        for name in POLARRAG_TOOL_NAMES
    }
    assert "Call this first" in descriptions["list_knowledge_resources"]
    assert "knowledge_space_id" in descriptions["list_knowledge_resources"]
    assert "same PolarRAG instance and Space" in descriptions["kb_search"]
    assert "partial_failures" in descriptions["kb_search"]
    assert "RERANKER_NOT_CONFIGURED" in descriptions["kb_search"]
    assert "chunk_index" in descriptions["kb_fetch_context"]
    assert "filename" in descriptions["doc_find_by_name"]
    assert "processing and indexing status" in descriptions["doc_status"]
    assert "within one document" in descriptions["doc_recall"]
    assert "does not download or proxy" in descriptions["doc_get_original"]
    assert "MANAGE" in descriptions["doc_delete"]
    assert "EXECUTE" in descriptions["doc_rechunk"]
    assert "local upload script" in descriptions["prepare_document_upload"]
    assert "missing parts" in descriptions["complete_document_upload"]
    assert "call this tool again" in descriptions["complete_document_upload"]
    assert "authoritative doc_id" in descriptions["complete_document_upload"]
    prepare_schema = manager.get_tool("prepare_document_upload").parameters
    assert set(prepare_schema["required"]) == {
        "knowledge_resource_id",
        "filename",
        "file_size_bytes",
        "file_md5",
        "file_sha256",
    }
    assert manager.get_tool("complete_document_upload").parameters["required"] == [
        "upload_session_id"
    ]
    assert set(manager.get_tool("complete_document_upload").parameters["properties"]) == {
        "upload_session_id"
    }
    assert manager.get_tool("doc_delete").annotations.destructiveHint is True
    assert manager.get_tool("doc_delete").annotations.readOnlyHint is False
    assert manager.get_tool("doc_rechunk").annotations.destructiveHint is False
    assert manager.get_tool("doc_rechunk").annotations.readOnlyHint is False


async def test_document_tools_verify_selected_resource_and_normalize_original(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import (
        handle_doc_get_original,
        handle_doc_status,
        handle_kb_fetch_context,
    )

    session, user, resources = seeded
    document_client = FakeDocumentClient()

    def factory(_instance):
        return document_client

    status = await handle_doc_status(
        session,
        user,
        knowledge_resource_id=resources[0].id,
        doc_id="doc-a",
        client_factory=factory,
    )
    original = await handle_doc_get_original(
        session,
        user,
        knowledge_resource_id=resources[0].id,
        doc_id="doc-a",
        client_factory=factory,
    )
    context = await handle_kb_fetch_context(
        session,
        user,
        knowledge_resource_id=resources[0].id,
        doc_id="doc-a",
        chunk_index=1,
        client_factory=factory,
    )

    assert json.loads(status.content[0].text)["result"] == {
        "doc_id": "doc-a",
        "kb_id": "low",
        "filename": "guide.pdf",
        "status": "COMPLETED",
    }
    assert json.loads(original.content[0].text)["result"] == {
        "doc_id": "doc-a",
        "filename": "guide.pdf",
        "file_type": "pdf",
        "oss_path": "oss://bucket/guide.pdf",
        "size": 123,
        "content_hash": "abc123",
    }
    assert context.isError is False
    assert document_client.calls == [
        "document_info",
        "document_info",
        "document_info",
        "fetch_context",
    ]


async def test_document_tools_hide_cross_resource_document(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import (
        handle_doc_get_original,
        handle_kb_fetch_context,
    )

    session, user, resources = seeded
    document_client = FakeDocumentClient(kb_id="high")

    def factory(_instance):
        return document_client

    original = await handle_doc_get_original(
        session,
        user,
        knowledge_resource_id=resources[0].id,
        doc_id="doc-a",
        client_factory=factory,
    )
    context = await handle_kb_fetch_context(
        session,
        user,
        knowledge_resource_id=resources[0].id,
        doc_id="doc-a",
        chunk_index=1,
        client_factory=factory,
    )

    assert original.isError is True
    assert json.loads(original.content[0].text)["error"] == (
        "DOCUMENT_NOT_ACCESSIBLE"
    )
    assert context.isError is True
    assert json.loads(context.content[0].text)["error"] == (
        "DOCUMENT_NOT_ACCESSIBLE"
    )
    assert document_client.calls == ["document_info", "document_info"]


async def test_document_mutations_verify_resource_and_use_trusted_identity(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import (
        handle_doc_delete,
        handle_doc_rechunk,
    )

    session, user, resources = seeded
    document_client = FakeDocumentClient()

    deleted = await handle_doc_delete(
        session,
        user,
        knowledge_resource_id=resources[0].id,
        doc_id="doc-a",
        client_factory=lambda _instance: document_client,
    )
    rechunked = await handle_doc_rechunk(
        session,
        user,
        knowledge_resource_id=resources[0].id,
        doc_id="doc-a",
        chunk_strategy="hybrid",
        chunk_max_tokens=384,
        client_factory=lambda _instance: document_client,
    )

    assert deleted.isError is False
    assert json.loads(deleted.content[0].text)["result"] == {
        "doc_id": "doc-a",
        "task_id": "delete-task",
        "status": "DELETING",
    }
    assert rechunked.isError is False
    assert json.loads(rechunked.content[0].text)["result"] == {
        "doc_id": "doc-a",
        "status": "RECHUNKING",
        "noop": False,
        "target_generation": 1,
    }
    assert document_client.calls == [
        "document_info",
        "delete_document",
        "document_info",
        "rechunk_document",
    ]
    assert document_client.mutation_kwargs == [
        {
            "acl_context": {
                "identity_domain": "tenant-a",
                    "principals": [
                        {"provider": "feishu", "type": "user", "id": "ou-user"},
                        {
                            "provider": "polarrag",
                            "type": "user",
                            "id": user.external_id,
                        },
                    ],
            }
        },
        {
            "chunk_strategy": "hybrid",
            "chunk_max_tokens": 384,
            "acl_context": {
                "identity_domain": "tenant-a",
                    "principals": [
                        {"provider": "feishu", "type": "user", "id": "ou-user"},
                        {
                            "provider": "polarrag",
                            "type": "user",
                            "id": user.external_id,
                        },
                    ],
            },
        },
    ]


async def test_document_mutations_hide_cross_resource_document(seeded) -> None:
    from server.mcp.tools.polarrag import (
        handle_doc_delete,
        handle_doc_rechunk,
    )

    session, user, resources = seeded
    document_client = FakeDocumentClient(kb_id="high")

    def factory(_instance):
        return document_client

    deleted = await handle_doc_delete(
        session,
        user,
        knowledge_resource_id=resources[0].id,
        doc_id="doc-a",
        client_factory=factory,
    )
    rechunked = await handle_doc_rechunk(
        session,
        user,
        knowledge_resource_id=resources[0].id,
        doc_id="doc-a",
        chunk_strategy="inherit",
        chunk_max_tokens=None,
        client_factory=factory,
    )

    assert json.loads(deleted.content[0].text)["error"] == "DOCUMENT_NOT_ACCESSIBLE"
    assert json.loads(rechunked.content[0].text)["error"] == "DOCUMENT_NOT_ACCESSIBLE"
    assert document_client.calls == ["document_info", "document_info"]


async def test_prepare_document_upload_uses_server_owned_destination(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import (
        handle_prepare_document_upload,
    )

    session, user, resources = seeded
    agent = await _upload_agent(session)
    store = FakeMultipartStore()
    prepared = await handle_prepare_document_upload(
        session,
        user,
        agent_id=agent.id,
        knowledge_resource_id=resources[0].id,
        filename="guide.md",
        file_size_bytes=9 * 1024 * 1024,
        file_md5="a" * 32,
        file_sha256="b" * 64,
        content_type="text/markdown",
        object_store_factory=lambda _space: store,
    )
    prepared_payload = json.loads(prepared.content[0].text)

    assert prepared.isError is False
    assert prepared_payload["knowledge_resource_id"] == resources[0].id
    upload = prepared_payload["result"]
    assert upload["upload_mode"] == "multipart"
    assert upload["part_size_bytes"] == 8 * 1024 * 1024
    assert upload["part_count"] == 2
    assert upload["parts"] == [
        {
            "part_number": 1,
            "upload_url": "https://upload.example.test/part/1",
        },
        {
            "part_number": 2,
            "upload_url": "https://upload.example.test/part/2",
        },
    ]
    assert "oss_bucket" not in upload
    assert "oss_object_key" not in upload
    row = await session.get(PolarRAGUploadSession, upload["upload_session_id"])
    assert row is not None
    assert row.pas_user_id == user.id
    assert row.agent_id == agent.id
    assert row.knowledge_resource_id == resources[0].id
    assert row.oss_bucket == "tenant-a-documents"
    assert row.oss_object_key.startswith("pas/documents/")
    assert row.status == PolarRAGUploadStatus.PREPARED
    cleanup = await session.get(PolarRAGUploadCleanup, row.id)
    assert cleanup is not None
    assert cleanup.object_state == "multipart"
    assert cleanup.oss_access_key_id_ciphertext == (
        resources[0].space.oss_access_key_id_ciphertext
    )
    assert cleanup.oss_access_key_secret_ciphertext == (
        resources[0].space.oss_access_key_secret_ciphertext
    )

async def test_complete_document_upload_verifies_parts_and_returns_polarrag_id(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import (
        handle_complete_document_upload,
        handle_prepare_document_upload,
    )

    session, user, resources = seeded
    resources[0].kb_type = "PERSONAL"
    resources[0].binding_mode = KnowledgeBindingMode.OWNER
    resources[0].owner_pas_user_id = user.id
    await session.commit()
    agent = await _upload_agent(session)
    store = FakeMultipartStore()
    upstream = FakeUploadClient()
    prepared = await handle_prepare_document_upload(
        session,
        user,
        agent_id=agent.id,
        knowledge_resource_id=resources[0].id,
        filename="guide.md",
        file_size_bytes=9 * 1024 * 1024,
        file_md5="a" * 32,
        file_sha256="b" * 64,
        content_type="text/markdown",
        object_store_factory=lambda _space: store,
    )
    upload_session_id = json.loads(prepared.content[0].text)["result"][
        "upload_session_id"
    ]
    store.parts = [
        SimpleNamespace(
            part_number=1,
            etag="etag-1",
            size=8 * 1024 * 1024,
        ),
        SimpleNamespace(
            part_number=2,
            etag="etag-2",
            size=1024 * 1024,
        ),
    ]
    completed = await handle_complete_document_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=upload_session_id,
        object_store_factory=lambda _space: store,
        client_factory=lambda _instance: upstream,
    )
    payload = json.loads(completed.content[0].text)

    assert completed.isError is False
    assert payload["result"] == {
        "doc_id": "polarrag-doc-id",
        "status": "DISPATCHED",
        "filename": "guide.md",
    }
    assert len(store.completed) == 1
    assert [part.part_number for part in store.completed[0][2]] == [1, 2]
    assert upstream.calls[0]["space_id"] == "space-a"
    assert upstream.calls[0]["kb_id"] == "low"
    assert "doc_id" not in upstream.calls[0]
    assert upstream.calls[0]["acl_context"]["actor"] == {
        "provider": "polarrag",
        "type": "user",
        "id": user.external_id,
    }
    assert upstream.calls[0]["metadata"] == {"sha256": "b" * 64}
    row = await session.get(PolarRAGUploadSession, upload_session_id)
    assert row is not None
    assert row.status == PolarRAGUploadStatus.COMPLETED
    assert row.doc_id == "polarrag-doc-id"
    assert (
        await session.get(PolarRAGUploadCleanup, upload_session_id)
        is None
    )

    repeated = await handle_complete_document_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=upload_session_id,
        object_store_factory=lambda _space: store,
        client_factory=lambda _instance: upstream,
    )
    assert repeated.isError is False
    assert len(store.completed) == 1
    assert len(upstream.calls) == 1


async def test_upload_session_rejects_other_agent_and_abort_cleans_oss(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import handle_prepare_document_upload
    from server.polarrag.mcp_upload import (
        UploadSessionError,
        abort_upload,
        resume_upload,
    )

    session, user, resources = seeded
    owner = await _upload_agent(session)
    other = await _upload_agent(session)
    store = FakeMultipartStore()
    prepared = await handle_prepare_document_upload(
        session,
        user,
        agent_id=owner.id,
        knowledge_resource_id=resources[0].id,
        filename="guide.md",
        file_size_bytes=1024,
        file_md5="a" * 32,
        file_sha256="b" * 64,
        content_type=None,
        object_store_factory=lambda _space: store,
    )
    upload_session_id = json.loads(prepared.content[0].text)["result"][
        "upload_session_id"
    ]

    with pytest.raises(
        UploadSessionError, match="UPLOAD_SESSION_NOT_ACCESSIBLE"
    ):
        await resume_upload(
            session,
            user,
            agent_id=other.id,
            upload_session_id=upload_session_id,
            resource_scope=None,
            object_store_factory=lambda _space: store,
        )

    _resource, aborted = await abort_upload(
        session,
        user,
        agent_id=owner.id,
        upload_session_id=upload_session_id,
        object_store_factory=lambda _space: store,
    )
    assert aborted["status"] == "aborted"
    assert store.aborted == [(store.key, store.upload_id)]
    row = await session.get(PolarRAGUploadSession, upload_session_id)
    assert row is not None
    assert row.status == PolarRAGUploadStatus.ABORTED
    assert (
        await session.get(PolarRAGUploadCleanup, upload_session_id)
        is None
    )


async def test_complete_document_upload_returns_safe_acl_denial(
    seeded,
    monkeypatch,
) -> None:
    from server.mcp.tools.polarrag import handle_complete_document_upload
    from server.polarrag.mcp_upload import UploadSessionError

    session, user, _resources = seeded
    agent = await _upload_agent(session)

    async def deny_upload(*_args, **_kwargs):
        raise UploadSessionError("POLARRAG_DOCUMENT_UPLOAD_FORBIDDEN")

    monkeypatch.setattr(
        "server.mcp.tools.polarrag.complete_upload",
        deny_upload,
    )

    result = await handle_complete_document_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=str(uuid.uuid4()),
    )

    assert result.isError is True
    assert json.loads(result.content[0].text) == {
        "error": "POLARRAG_DOCUMENT_UPLOAD_FORBIDDEN",
        "message": (
            "PolarRAG denied document upload for this enterprise identity. "
            "Ask an administrator to verify the Space identity domain and "
            "canonical PAS user ownership."
        ),
    }


async def test_complete_document_upload_returns_only_missing_parts_then_completes(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import (
        handle_complete_document_upload,
        handle_prepare_document_upload,
    )

    session, user, resources = seeded
    agent = await _upload_agent(session)
    store = FakeMultipartStore()
    prepared = await handle_prepare_document_upload(
        session,
        user,
        agent_id=agent.id,
        knowledge_resource_id=resources[0].id,
        filename="guide.md",
        file_size_bytes=9 * 1024 * 1024,
        file_md5="a" * 32,
        file_sha256="b" * 64,
        content_type=None,
        object_store_factory=lambda _space: store,
    )
    upload_session_id = json.loads(prepared.content[0].text)["result"][
        "upload_session_id"
    ]
    store.parts = [
        SimpleNamespace(
            part_number=1,
            etag="server-etag-1",
            size=8 * 1024 * 1024,
        )
    ]
    incomplete = await handle_complete_document_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=upload_session_id,
        object_store_factory=lambda _space: store,
        client_factory=lambda _instance: FakeUploadClient(),
    )
    assert incomplete.isError is False
    incomplete_result = json.loads(incomplete.content[0].text)["result"]
    assert incomplete_result["status"] == "prepared"
    assert incomplete_result["parts"] == [
        {
            "part_number": 2,
            "upload_url": "https://upload.example.test/part/2",
        }
    ]
    store.parts = [
        SimpleNamespace(
            part_number=1,
            etag="server-etag-1",
            size=8 * 1024 * 1024,
        ),
        SimpleNamespace(
            part_number=2,
            etag="server-etag-2",
            size=1024 * 1024,
        ),
    ]

    result = await handle_complete_document_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=upload_session_id,
        object_store_factory=lambda _space: store,
        client_factory=lambda _instance: FakeUploadClient(),
    )

    assert result.isError is False
    assert json.loads(result.content[0].text)["result"]["doc_id"] == (
        "polarrag-doc-id"
    )
    assert len(store.completed) == 1


async def test_complete_document_upload_rejects_invalid_oss_part_size(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import (
        handle_complete_document_upload,
        handle_prepare_document_upload,
    )

    session, user, resources = seeded
    agent = await _upload_agent(session)
    store = FakeMultipartStore()
    prepared = await handle_prepare_document_upload(
        session,
        user,
        agent_id=agent.id,
        knowledge_resource_id=resources[0].id,
        filename="guide.md",
        file_size_bytes=1024,
        file_md5="a" * 32,
        file_sha256="b" * 64,
        content_type=None,
        object_store_factory=lambda _space: store,
    )
    upload_session_id = json.loads(prepared.content[0].text)["result"][
        "upload_session_id"
    ]
    store.parts = [
        SimpleNamespace(part_number=1, etag="server-etag", size=2048)
    ]

    result = await handle_complete_document_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=upload_session_id,
        object_store_factory=lambda _space: store,
        client_factory=lambda _instance: FakeUploadClient(),
    )

    assert result.isError is True
    assert json.loads(result.content[0].text)["error"] == (
        "OSS_UPLOAD_INVALID_STATE"
    )
    assert store.completed == []


async def test_complete_failure_persists_finalizing_cleanup_state(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import (
        handle_complete_document_upload,
        handle_prepare_document_upload,
    )

    session, user, resources = seeded
    agent = await _upload_agent(session)
    store = FakeMultipartStore()
    prepared = await handle_prepare_document_upload(
        session,
        user,
        agent_id=agent.id,
        knowledge_resource_id=resources[0].id,
        filename="guide.md",
        file_size_bytes=1024,
        file_md5="a" * 32,
        file_sha256="b" * 64,
        content_type=None,
        object_store_factory=lambda _space: store,
    )
    upload_session_id = json.loads(prepared.content[0].text)["result"][
        "upload_session_id"
    ]
    store.parts = [
        SimpleNamespace(part_number=1, etag="etag-1", size=1024)
    ]
    store.fail_complete = True

    result = await handle_complete_document_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=upload_session_id,
        object_store_factory=lambda _space: store,
        client_factory=lambda _instance: FakeUploadClient(),
    )

    assert result.isError is True
    assert json.loads(result.content[0].text)["error"] == "OSS_UPLOAD_FAILED"
    cleanup = await session.get(PolarRAGUploadCleanup, upload_session_id)
    assert cleanup is not None
    assert cleanup.object_state == "finalizing"


async def test_complete_document_upload_retries_only_polarrag_after_submit_failure(
    seeded,
) -> None:
    from server.mcp.tools.polarrag import (
        handle_complete_document_upload,
        handle_prepare_document_upload,
    )
    from server.polarrag.mcp_upload import resume_upload

    session, user, resources = seeded
    agent = await _upload_agent(session)
    store = FakeMultipartStore()
    upstream = FakeUploadClient(fail=True)
    prepared = await handle_prepare_document_upload(
        session,
        user,
        agent_id=agent.id,
        knowledge_resource_id=resources[0].id,
        filename="guide.md",
        file_size_bytes=1024,
        file_md5="a" * 32,
        file_sha256="b" * 64,
        content_type=None,
        object_store_factory=lambda _space: store,
    )
    upload_session_id = json.loads(prepared.content[0].text)["result"][
        "upload_session_id"
    ]
    store.parts = [
        SimpleNamespace(part_number=1, etag="etag-1", size=1024)
    ]
    first = await handle_complete_document_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=upload_session_id,
        object_store_factory=lambda _space: store,
        client_factory=lambda _instance: upstream,
    )
    await session.commit()
    row = await session.get(PolarRAGUploadSession, upload_session_id)

    assert first.isError is True
    assert json.loads(first.content[0].text) == {
        "error": "POLARRAG_DOCUMENT_SUBMIT_FAILED",
        "message": "POLARRAG_DOCUMENT_SUBMIT_FAILED",
    }
    assert row is not None
    assert row.status == PolarRAGUploadStatus.UPLOADED
    cleanup = await session.get(PolarRAGUploadCleanup, upload_session_id)
    assert cleanup is not None
    assert cleanup.object_state == "object"
    assert cleanup.cleanup_lease_until is None
    assert cleanup.operation_kind is None
    assert cleanup.operation_token is None
    assert cleanup.reconcile_required is True
    assert cleanup.submission_payload_ciphertext is not None
    assert len(store.completed) == 1

    _resource, resumed_upload = await resume_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=upload_session_id,
        resource_scope=None,
        object_store_factory=lambda _space: store,
    )
    assert isinstance(resumed_upload.pop("session_expires_at"), str)
    assert resumed_upload == {
        "upload_session_id": upload_session_id,
        "upload_mode": "multipart",
        "status": "uploaded",
        "part_size_bytes": 8 * 1024 * 1024,
        "part_count": 1,
        "parts": [],
        "uploaded_parts": [],
        "upload_urls_expire_in_seconds": 900,
    }

    upstream.fail = False
    retried = await handle_complete_document_upload(
        session,
        user,
        agent_id=agent.id,
        upload_session_id=upload_session_id,
        object_store_factory=lambda _space: store,
        client_factory=lambda _instance: upstream,
    )

    assert retried.isError is False
    assert len(store.completed) == 1
    assert len(upstream.calls) == 2
