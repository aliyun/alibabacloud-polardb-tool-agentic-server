from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from sqlalchemy import select

from server.core.crypto import encrypt
from server.models import (
    Agent,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    AuditLog,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalType,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding,
    EnterpriseIdentitySourceStatus,
    IdentitySourceProvider,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceManagementMode,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
)
from server.polarrag.contracts import (
    PolarRAGErrorCode,
    PolarRAGUpstreamError,
)
from server.polarrag.upload import document_actor, validate_filename
from server.polarrag.oss import OssMultipartPart, OssObjectStore

pytest_plugins = ("tests._admin_api_fixtures",)


class FakeObjectStore:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.deleted: list[str] = []

    async def put(self, key: str, content) -> None:
        self.uploads.append((key, content.read()))

    async def delete(self, key: str) -> None:
        self.deleted.append(key)


class FakeSubmitClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    async def submit_document(self, space_id, kb_id, **kwargs):
        self.calls.append({"space_id": space_id, "kb_id": kb_id, **kwargs})
        if self.fail:
            raise RuntimeError("upstream secret response")
        return {"doc_id": "polarrag-doc-id", "status": "DISPATCHED"}


class FakeBatchSubmitClient(FakeSubmitClient):
    def __init__(self) -> None:
        super().__init__()
        self.bulk_calls: list[dict] = []

    async def submit_document(self, *_args, **_kwargs):
        raise AssertionError("batch upload must not use the single-document route")

    async def submit_documents(self, space_id, documents, *, acl_context):
        self.bulk_calls.append(
            {
                "space_id": space_id,
                "documents": documents,
                "acl_context": acl_context,
            }
        )
        results = []
        for document in documents:
            assert "doc_id" not in document
            if document["filename"] == "denied.md":
                results.append({"success": False, "doc_id": "doc-denied.md"})
            else:
                results.append(
                    {
                        "success": True,
                        "document": {
                            "doc_id": f"doc-{document['filename']}",
                            "space_id": space_id,
                            "kb_id": document["kb_id"],
                            "filename": document["filename"],
                            "status": "DISPATCHED",
                        },
                    }
                )
        return results


class FailingBatchSubmitClient(FakeBatchSubmitClient):
    async def submit_documents(self, space_id, documents, *, acl_context):
        await super().submit_documents(
            space_id,
            documents,
            acl_context=acl_context,
        )
        raise RuntimeError("upstream response lost")


class FailingObjectStore(FakeObjectStore):
    def __init__(self, *, fail_put: bool = True, fail_delete: bool = False) -> None:
        super().__init__()
        self.fail_put = fail_put
        self.fail_delete = fail_delete

    async def put(self, key: str, content) -> None:
        await super().put(key, content)
        if self.fail_put:
            raise RuntimeError("OSS response lost")

    async def delete(self, key: str) -> None:
        await super().delete(key)
        if self.fail_delete:
            raise RuntimeError("OSS delete failed")


class FakeDocumentManagementClient:
    def __init__(self, *, kb_id: str = "public-kb") -> None:
        self.kb_id = kb_id
        self.calls: list[tuple[str, dict]] = []

    async def find_by_name(self, space_id, **kwargs):
        self.calls.append(("find_by_name", {"space_id": space_id, **kwargs}))
        source = {
            "doc_id": "doc-a",
            "kb_id": self.kb_id,
            "filename": "guide.md",
            "file_size_bytes": 1536,
            "created_at": "2026-08-12T03:04:05Z",
            "status": "COMPLETED",
            "chunk_count": 12,
            "active_generation": 0,
            "acl_read_tokens": ["secret"],
        }
        return {
            "hits": {
                "hits": [
                    {"_source": source},
                    {"_source": source},
                ]
            }
        }

    async def list_documents(self, space_id, **kwargs):
        self.calls.append(("list_documents", {"space_id": space_id, **kwargs}))
        return {
            "documents": [
                {
                    "doc_id": "doc-a",
                    "kb_id": self.kb_id,
                    "filename": "guide.md",
                    "file_size_bytes": 1536,
                    "created_at": "2026-08-12T03:04:05Z",
                    "status": "COMPLETED",
                    "chunk_count": 12,
                    "oss_path": "oss://secret/path",
                }
            ],
            "has_more": True,
            "next_after_doc_id": "doc-a",
        }

    async def document_info(self, space_id, doc_id, **kwargs):
        self.calls.append(
            (
                "document_info",
                {"space_id": space_id, "doc_id": doc_id, **kwargs},
            )
        )
        return {
            "doc_id": doc_id,
            "space_id": space_id,
            "kb_id": self.kb_id,
            "filename": "guide.md",
            "status": "COMPLETED",
            "chunk_count": 12,
            "active_generation": 0,
            "acl_read_tokens": ["secret"],
        }

    async def delete_document(self, space_id, doc_id, **kwargs):
        self.calls.append(
            (
                "delete_document",
                {"space_id": space_id, "doc_id": doc_id, **kwargs},
            )
        )
        return {"doc_id": doc_id, "task_id": "delete-task", "status": "DELETING"}

    async def rechunk_document(self, space_id, doc_id, **kwargs):
        self.calls.append(
            (
                "rechunk_document",
                {"space_id": space_id, "doc_id": doc_id, **kwargs},
            )
        )
        return {
            "success": True,
            "doc_id": doc_id,
            "status": "RECHUNKING",
            "noop": False,
            "target_generation": 1,
        }


class FakeDocumentPermissionClient(FakeDocumentManagementClient):
    supports_plugin_107 = True

    async def list_document_permissions(self, space_id, doc_ids, **kwargs):
        self.calls.append(
            (
                "list_document_permissions",
                {"space_id": space_id, "doc_ids": doc_ids, **kwargs},
            )
        )
        return {
            "documents": [
                {
                    "doc_id": "doc-a",
                    "identity_domain": "tenant-a",
                    "state": "ACTIVE",
                    "updating": False,
                    "acl_version": 3,
                    "acl_set_id": "acl-set-a",
                    "grants": [
                        {
                            "principal": {
                                "provider": "feishu",
                                "type": "group",
                                "id": "group-member",
                            },
                            "actions": ["READ"],
                        }
                    ],
                }
            ]
        }


async def _seed_upload_resource(setup):
    factory, admin, member = setup
    async with factory() as session:
        session.add(
            EnterprisePrincipalAssignment.create(
                pas_user_id=member.id,
                identity_domain="tenant-a",
                provider="feishu",
                principal_type=EnterprisePrincipalType.USER,
                principal_id="ou-member",
                source=EnterprisePrincipalSource.ADMIN_MANAGED,
            )
        )
        session.add(
            EnterprisePrincipalAssignment.create(
                pas_user_id=member.id,
                identity_domain="tenant-a",
                provider="feishu",
                principal_type=EnterprisePrincipalType.GROUP,
                principal_id="group-member",
                source=EnterprisePrincipalSource.ADMIN_MANAGED,
            )
        )
        instance = PolarRAGInstance(
            name="RAG",
            scheme="https",
            host="rag.example.test",
            port=9200,
            username_ciphertext=encrypt("service-user"),
            password_ciphertext=encrypt("service-password"),
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
            oss_access_key_id_ciphertext=encrypt("oss-ak"),
            oss_access_key_secret_ciphertext=encrypt("oss-sk"),
            oss_object_prefix="pas/documents",
            oss_config_validated=True,
            enabled=True,
        )
        session.add(space)
        await session.flush()
        source = EnterpriseIdentitySource.create(
            name="Upload test directory",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="upload-test",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        session.add(source)
        await session.flush()
        session.add(EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=source.id,
            knowledge_space_id=space.knowledge_space_id,
        ))
        resource = KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id,
            polarrag_instance_id=instance.id,
            space_id=space.space_id,
            kb_id="public-kb",
            name="Public KB",
            kb_type="PUBLIC",
            identity_domain=space.identity_domain,
            binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            enabled=True,
        )
        session.add(resource)
        await session.commit()
        return resource.id


async def _seed_document_agent(
    setup,
    resource_id: str,
    *,
    public_knowledge_resource_ids_json: str | None = None,
) -> str:
    factory, admin, member = setup
    async with factory() as session:
        resource = await session.get(KnowledgeResource, resource_id)
        assert resource is not None
        agent = Agent(name=f"document-agent-{resource_id}", created_by=admin.id)
        session.add(agent)
        await session.flush()
        session.add_all(
            [
                AgentUserAssignment(
                    agent_id=agent.id,
                    user_id=member.id,
                    created_by_user_id=admin.id,
                ),
                AgentPolarRAGInstanceBinding(
                    agent_id=agent.id,
                    polarrag_instance_id=resource.polarrag_instance_id,
                    created_by_user_id=admin.id,
                    public_knowledge_resource_ids_json=(public_knowledge_resource_ids_json),
                ),
            ]
        )
        await session.commit()
        return agent.id


async def _seed_additional_upload_resource(
    setup,
    source_resource_id: str,
    kb_id: str,
) -> str:
    factory, _admin, _member = setup
    async with factory() as session:
        source = await session.get(KnowledgeResource, source_resource_id)
        assert source is not None
        resource = KnowledgeResource(
            knowledge_space_id=source.knowledge_space_id,
            polarrag_instance_id=source.polarrag_instance_id,
            space_id=source.space_id,
            kb_id=kb_id,
            name=kb_id,
            kb_type="PUBLIC",
            identity_domain=source.identity_domain,
            binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            enabled=True,
        )
        session.add(resource)
        await session.commit()
        return resource.id


@pytest.mark.parametrize("use_pair", [False, True])
async def test_member_upload_uses_server_resource_oss_and_identity(
    client,
    setup,
    monkeypatch,
    use_pair,
) -> None:
    http, admin_headers, member_headers = client
    factory, _admin, member = setup
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FakeObjectStore()
    upstream = FakeSubmitClient()
    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: store,
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )
    files = {"file": ("guide.md", b"trusted upload body", "text/markdown")}
    data = {
        "agent_id": agent_id,
        "knowledge_resource_id": resource_id,
        "oss_path": "oss://attacker/secret",
        "acl_context": '{"identity_domain":"attacker"}',
    }

    if use_pair:
        data.pop("knowledge_resource_id")
        async with factory() as session:
            selected_resource = await session.get(KnowledgeResource, resource_id)
            data.update(space_id=selected_resource.space_id, kb_id=selected_resource.kb_id)

    resources = await http.get("/api/me/resources", headers=member_headers)
    assert resources.status_code == 200
    assert resources.json()["knowledge_resources"][0]["upload_ready"] is True

    forbidden = await http.post(
        "/api/me/polarrag/documents",
        data=data,
        files=files,
        headers=admin_headers,
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["detail"]["code"] == "ADMIN_DOCUMENT_UPLOAD_FORBIDDEN"

    uploaded = await http.post(
        "/api/me/polarrag/documents",
        data=data,
        files=files,
        headers=member_headers,
    )
    assert uploaded.status_code == 201
    assert uploaded.json()["doc_id"] == "polarrag-doc-id"
    assert uploaded.json()["status"] == "DISPATCHED"
    assert uploaded.json()["filename"] == "guide.md"
    assert "oss" not in uploaded.text.lower()
    assert store.uploads[0][1] == b"trusted upload body"
    assert store.uploads[0][0].startswith("pas/documents/")
    assert upstream.calls[0]["space_id"] == "space-a"
    assert upstream.calls[0]["kb_id"] == "public-kb"
    assert "doc_id" not in upstream.calls[0]
    assert upstream.calls[0]["acl_context"] == {
        "identity_domain": "tenant-a",
        "principals": [
            {"provider": "feishu", "type": "group", "id": "group-member"},
            {"provider": "feishu", "type": "user", "id": "ou-member"},
            {
                "provider": "polarrag",
                "type": "user",
                "id": member.external_id,
            },
        ],
        "actor": {
            "provider": "polarrag",
            "type": "user",
            "id": member.external_id,
        },
    }
    assert "acl" not in upstream.calls[0]
    async with factory() as session:
        audit = (await session.execute(select(AuditLog).where(AuditLog.action == "polarrag.doc_upload"))).scalar_one()
        assert audit.status.value == "success"
        assert "oss-ak" not in (audit.metadata_json or "")
        context = json.loads(json.loads(audit.metadata_json)["client_info"])
        assert context["knowledge_resource_ids"] == [resource_id]
        assert context["space_ids"] == ["space-a"]
    assert context["kb_ids"] == ["public-kb"]


@pytest.mark.parametrize("coordinates", [
    {"space_id": "space-a"}, {"kb_id": "public-kb"},
    {"space_id": "space-a", "kb_id": "public-kb"},
])
async def test_single_upload_rejects_mixed_selectors_before_side_effects(client, setup, monkeypatch, coordinates):
    http, _, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FakeObjectStore()
    monkeypatch.setattr("server.api.polarrag_documents.object_store_from_space", lambda _space: store)
    response = await http.post(
        "/api/me/polarrag/documents",
        data={"agent_id": agent_id, "knowledge_resource_id": resource_id, **coordinates},
        files={"file": ("guide.md", b"body", "text/markdown")}, headers=member_headers,
    )
    assert response.status_code == 422
    assert store.uploads == []


async def test_platform_single_upload_delegates_with_absent_pair_selectors(setup, monkeypatch):
    from server.api.platform_polarrag import upload_knowledge_base_document

    factory, _, member = setup
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FakeObjectStore()
    upstream = FakeSubmitClient()
    monkeypatch.setattr("server.api.polarrag_documents.object_store_from_space", lambda _space: store)
    monkeypatch.setattr("server.api.polarrag_documents.client_from_instance", lambda _instance: upstream)
    async with factory() as session:
        agent = await session.get(Agent, agent_id)
        result = await upload_knowledge_base_document(
            resource_id,
            file=UploadFile(filename="guide.md", file=io.BytesIO(b"platform upload")),
            context=SimpleNamespace(agent=agent, user=member),
            session=session,
        )
        assert result == {"doc_id": "polarrag-doc-id", "status": "DISPATCHED", "filename": "guide.md"}
        assert store.uploads[0][1] == b"platform upload"
        assert upstream.calls[0]["space_id"] == "space-a"
        assert upstream.calls[0]["kb_id"] == "public-kb"
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "polarrag.doc_upload"))
        assert audit is not None and audit.status.value == "success"


@pytest.mark.parametrize("use_pair", [False, True])
async def test_member_batch_uploads_documents_to_different_kbs_in_one_space(
    client,
    setup,
    monkeypatch,
    use_pair,
) -> None:
    http, _admin_headers, member_headers = client
    resource_a = await _seed_upload_resource(setup)
    resource_b = await _seed_additional_upload_resource(setup, resource_a, "secondary-kb")
    agent_id = await _seed_document_agent(setup, resource_a)
    store = FakeObjectStore()
    upstream = FakeBatchSubmitClient()
    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: store,
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.post(
        "/api/me/polarrag/documents/batch",
        data={"agent_id": agent_id, **({"space_id": "space-a", "kb_ids": ["public-kb", "secondary-kb", "public-kb"]}
               if use_pair else {"knowledge_resource_ids": [resource_a, resource_b, resource_a]})},
        files=[
            ("files", ("a.md", b"alpha", "text/markdown")),
            ("files", ("b.md", b"beta", "text/markdown")),
            ("files", ("c.md", b"gamma", "text/markdown")),
        ],
        headers=member_headers,
    )

    assert response.status_code == 201
    assert response.json() == {
        "accepted": [
            {
                "index": 0,
                "knowledge_resource_id": resource_a,
                "kb_id": "public-kb",
                "doc_id": "doc-a.md",
                "filename": "a.md",
                "status": "DISPATCHED",
            },
            {
                "index": 1,
                "knowledge_resource_id": resource_b,
                "kb_id": "secondary-kb",
                "doc_id": "doc-b.md",
                "filename": "b.md",
                "status": "DISPATCHED",
            },
            {
                "index": 2,
                "knowledge_resource_id": resource_a,
                "kb_id": "public-kb",
                "doc_id": "doc-c.md",
                "filename": "c.md",
                "status": "DISPATCHED",
            },
        ],
        "failed": [],
    }
    assert len(upstream.bulk_calls) == 1
    assert [document["kb_id"] for document in upstream.bulk_calls[0]["documents"]] == [
        "public-kb",
        "secondary-kb",
        "public-kb",
    ]
    assert [body for _key, body in store.uploads] == [b"alpha", b"beta", b"gamma"]


@pytest.mark.parametrize("selector", [
    {"space_id": "space-a", "kb_ids": ["public-kb", "secondary-kb"]},
    {"space_id": "space-a", "kb_ids": ["public-kb"], "knowledge_resource_ids": ["resource"]},
    {"kb_ids": ["public-kb"]},
    {"space_id": "space-a", "kb_ids": ["missing-kb"]},
])
async def test_batch_pair_selector_rejects_invalid_target_before_upload(client, setup, monkeypatch, selector):
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FakeObjectStore()
    monkeypatch.setattr("server.api.polarrag_documents.object_store_from_space", lambda _space: store)
    response = await http.post(
        "/api/me/polarrag/documents/batch",
        data={"agent_id": agent_id, **selector},
        files=[("files", ("a.md", b"alpha", "text/markdown"))],
        headers=member_headers,
    )
    assert response.status_code in {404, 422}, response.text
    assert store.uploads == []


async def test_batch_upload_prevalidates_every_file_before_oss_side_effects(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FakeObjectStore()
    upstream = FakeBatchSubmitClient()
    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: store,
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.post(
        "/api/me/polarrag/documents/batch",
        data={
            "agent_id": agent_id,
            "knowledge_resource_ids": [resource_id, resource_id],
        },
        files=[
            ("files", ("valid.md", b"valid", "text/markdown")),
            ("files", ("../invalid.md", b"invalid", "text/markdown")),
        ],
        headers=member_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_UPLOAD_FILE"
    assert store.uploads == []
    assert upstream.bulk_calls == []


async def test_batch_upload_continues_after_upstream_submit_failure(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FakeObjectStore()
    upstream = FakeBatchSubmitClient()
    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: store,
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.post(
        "/api/me/polarrag/documents/batch",
        data={
            "agent_id": agent_id,
            "knowledge_resource_ids": [resource_id, resource_id],
        },
        files=[
            ("files", ("denied.md", b"denied", "text/markdown")),
            ("files", ("accepted.md", b"accepted", "text/markdown")),
        ],
        headers=member_headers,
    )

    assert response.status_code == 207
    payload = response.json()
    assert payload["accepted"] == [
        {
            "index": 1,
            "knowledge_resource_id": resource_id,
            "kb_id": "public-kb",
            "doc_id": "doc-accepted.md",
            "filename": "accepted.md",
            "status": "DISPATCHED",
        }
    ]
    assert payload["failed"] == [
        {
            "index": 0,
            "knowledge_resource_id": resource_id,
            "kb_id": "public-kb",
            "filename": "denied.md",
            "status_code": 502,
            "code": "POLARRAG_DOCUMENT_SUBMIT_FAILED",
            "message": "PolarRAG did not accept the uploaded document.",
        }
    ]
    assert store.deleted == [store.uploads[0][0]]
    assert len(upstream.bulk_calls) == 1


async def test_batch_upload_preserves_oss_objects_when_bulk_result_is_unknown(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FakeObjectStore()
    upstream = FailingBatchSubmitClient()
    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: store,
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.post(
        "/api/me/polarrag/documents/batch",
        data={
            "agent_id": agent_id,
            "knowledge_resource_ids": [resource_id, resource_id],
        },
        files=[
            ("files", ("a.md", b"a", "text/markdown")),
            ("files", ("b.md", b"b", "text/markdown")),
        ],
        headers=member_headers,
    )

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "POLARRAG_BATCH_SUBMIT_RESULT_UNKNOWN"
    assert store.deleted == []


async def test_batch_upload_cleans_up_after_oss_put_result_is_unknown(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FailingObjectStore()
    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: store,
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: FakeBatchSubmitClient(),
    )

    response = await http.post(
        "/api/me/polarrag/documents/batch",
        data={"agent_id": agent_id, "knowledge_resource_ids": [resource_id]},
        files=[("files", ("a.md", b"a", "text/markdown"))],
        headers=member_headers,
    )

    assert response.status_code == 207
    assert response.json()["failed"][0]["code"] == "OSS_UPLOAD_FAILED"
    assert store.deleted == [store.uploads[0][0]]


async def test_batch_upload_logs_failed_cleanup_for_definitive_rejection(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FailingObjectStore(fail_put=False, fail_delete=True)
    cleanup_errors: list[str] = []
    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: store,
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: FakeBatchSubmitClient(),
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.logger.exception",
        lambda message, *_args: cleanup_errors.append(message),
    )

    response = await http.post(
        "/api/me/polarrag/documents/batch",
        data={"agent_id": agent_id, "knowledge_resource_ids": [resource_id]},
        files=[("files", ("denied.md", b"denied", "text/markdown"))],
        headers=member_headers,
    )

    assert response.status_code == 207
    assert store.deleted == [store.uploads[0][0]]
    assert cleanup_errors == [
        "polarrag.batch_upload.oss_cleanup_failed index=%d knowledge_resource_id=%s"
    ]


@pytest.mark.parametrize("use_pair", [False, True])
async def test_member_upload_rejects_kb_outside_selected_agent_scope(
    client,
    setup,
    monkeypatch,
    use_pair,
) -> None:
    http, _admin_headers, member_headers = client
    _factory, _admin, _member = setup
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id, public_knowledge_resource_ids_json="[]")

    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: FakeObjectStore(),
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: FakeSubmitClient(),
    )

    async with _factory() as session:
        resource = await session.get(KnowledgeResource, resource_id)
        selector = ({"space_id": resource.space_id, "kb_id": resource.kb_id} if use_pair
                    else {"knowledge_resource_id": resource_id})
    response = await http.post(
        "/api/me/polarrag/documents",
        data={
            "agent_id": agent_id,
            **selector,
        },
        files={"file": ("guide.md", b"body", "text/markdown")},
        headers=member_headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "KNOWLEDGE_RESOURCE_NOT_ACCESSIBLE"


async def test_external_sync_resource_rejects_upload_before_side_effects(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    updated = await http.put(
        f"/api/polarrag/knowledge-resources/{resource_id}/management-mode",
        json={"management_mode": KnowledgeResourceManagementMode.EXTERNAL_SYNC.value},
        headers=admin_headers,
    )
    assert updated.status_code == 200
    store = FakeObjectStore()
    upstream = FakeSubmitClient()
    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: store,
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.post(
        "/api/me/polarrag/documents",
        data={"agent_id": agent_id, "knowledge_resource_id": resource_id},
        files={"file": ("guide.md", b"body", "text/markdown")},
        headers=member_headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "EXTERNAL_SYNC_RESOURCE_READ_ONLY"
    assert store.uploads == []
    assert upstream.calls == []


async def test_submit_failure_deletes_uploaded_object_and_hides_upstream_body(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    store = FakeObjectStore()
    upstream = FakeSubmitClient(fail=True)
    monkeypatch.setattr(
        "server.api.polarrag_documents.object_store_from_space",
        lambda _space: store,
    )
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.post(
        "/api/me/polarrag/documents",
        data={"agent_id": agent_id, "knowledge_resource_id": resource_id},
        files={"file": ("guide.md", io.BytesIO(b"body"), "text/markdown")},
        headers=member_headers,
    )

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "POLARRAG_DOCUMENT_SUBMIT_FAILED"
    assert "secret" not in response.text
    assert store.deleted == [store.uploads[0][0]]


async def test_upload_rejects_unsafe_or_extensionless_filename(
    client,
    setup,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    for filename in ("../guide.md", "folder\\guide.md", "README"):
        response = await http.post(
            "/api/me/polarrag/documents",
            data={"agent_id": agent_id, "knowledge_resource_id": resource_id},
            files={"file": (filename, b"body", "application/octet-stream")},
            headers=member_headers,
        )
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_UPLOAD_FILE"


def test_document_actor_requires_user_principal() -> None:
    actor = document_actor(
        {
            "principals": [
                {"provider": "feishu", "type": "user", "id": "ou-member"},
                {"provider": "polarrag", "type": "user", "id": "pas-user"},
                {"provider": "feishu", "type": "group", "id": "group-a"},
            ]
        }
    )

    assert actor == {"provider": "polarrag", "type": "user", "id": "pas-user"}
    with pytest.raises(ValueError):
        document_actor({"principals": []})
    with pytest.raises(ValueError):
        document_actor({"principals": [{"provider": "feishu", "type": "user"}]})


@pytest.mark.parametrize("use_pair", [False, True])
async def test_member_finds_and_mutates_only_documents_in_selected_resource(
    client,
    setup,
    monkeypatch,
    use_pair,
) -> None:
    http, admin_headers, member_headers = client
    factory, _admin, member = setup
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    async with factory() as session:
        resource = await session.get(KnowledgeResource, resource_id)
        selector = ({"space_id": resource.space_id, "kb_id": resource.kb_id} if use_pair
                    else {"knowledge_resource_id": resource_id})
    upstream = FakeDocumentManagementClient()
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    listed = await http.post(
        "/api/me/polarrag/documents/_list",
        json={
            "agent_id": agent_id,
            **selector,
            "size": 20,
            "after_doc_id": "doc-before",
        },
        headers=member_headers,
    )
    found = await http.post(
        "/api/me/polarrag/documents/_find",
        json={
            "agent_id": agent_id,
            **selector,
            "filename": "guide",
            "limit": 20,
        },
        headers=member_headers,
    )
    forbidden = await http.post(
        "/api/me/polarrag/documents/_find",
        json={
            "agent_id": agent_id,
            **selector,
            "filename": "guide",
        },
        headers=admin_headers,
    )
    injected = await http.post(
        "/api/me/polarrag/documents/_find",
        json={
            "agent_id": agent_id,
            **selector,
            "filename": "guide",
            "acl_context": {"identity_domain": "attacker"},
        },
        headers=member_headers,
    )
    deleted = await http.request(
        "DELETE",
        "/api/me/polarrag/documents/doc-a",
        json={"agent_id": agent_id, **selector},
        headers=member_headers,
    )
    rechunked = await http.post(
        "/api/me/polarrag/documents/doc-a/rechunk",
        json={
            "agent_id": agent_id,
            **selector,
            "chunk_strategy": "hybrid",
            "chunk_max_tokens": 384,
        },
        headers=member_headers,
    )

    assert listed.json() == {
        "documents": [
            {
                "doc_id": "doc-a",
                "kb_id": "public-kb",
                "filename": "guide.md",
                "file_size_bytes": 1536,
                "created_at": "2026-08-12T03:04:05Z",
                "status": "COMPLETED",
                "chunk_count": 12,
            }
        ],
        "has_more": True,
        "next_after_doc_id": "doc-a",
    }
    assert "oss" not in listed.text.lower()
    assert found.status_code == 200
    assert found.json() == {
        "documents": [
            {
                "doc_id": "doc-a",
                "kb_id": "public-kb",
                "filename": "guide.md",
                "file_size_bytes": 1536,
                "created_at": "2026-08-12T03:04:05Z",
                "status": "COMPLETED",
                "chunk_count": 12,
                "active_generation": 0,
            }
        ]
    }
    assert "acl" not in found.text.lower()
    assert forbidden.status_code == 403
    assert injected.status_code == 422
    assert deleted.json() == {
        "doc_id": "doc-a",
        "task_id": "delete-task",
        "status": "DELETING",
    }
    assert rechunked.json() == {
        "doc_id": "doc-a",
        "status": "RECHUNKING",
        "noop": False,
        "target_generation": 1,
    }
    trusted_context = {
        "identity_domain": "tenant-a",
        "principals": [
            {"provider": "feishu", "type": "group", "id": "group-member"},
            {"provider": "feishu", "type": "user", "id": "ou-member"},
            {
                "provider": "polarrag",
                "type": "user",
                "id": member.external_id,
            },
        ],
    }
    assert upstream.calls == [
        (
            "list_documents",
            {
                "space_id": "space-a",
                "kb_id": "public-kb",
                "size": 20,
                "after_doc_id": "doc-before",
                "acl_context": trusted_context,
                "page": 1,
            },
        ),
        (
            "find_by_name",
            {
                "space_id": "space-a",
                "kb_id": "public-kb",
                "filename": "guide",
                "limit": 20,
                "acl_context": trusted_context,
            },
        ),
        (
            "document_info",
            {
                "space_id": "space-a",
                "doc_id": "doc-a",
                "acl_context": trusted_context,
            },
        ),
        (
            "delete_document",
            {
                "space_id": "space-a",
                "doc_id": "doc-a",
                "acl_context": trusted_context,
            },
        ),
        (
            "document_info",
            {
                "space_id": "space-a",
                "doc_id": "doc-a",
                "acl_context": trusted_context,
            },
        ),
        (
            "rechunk_document",
            {
                "space_id": "space-a",
                "doc_id": "doc-a",
                "chunk_strategy": "hybrid",
                "chunk_max_tokens": 384,
                "force": False,
                "acl_context": trusted_context,
            },
        ),
    ]
    async with factory() as session:
        logs = (
            await session.execute(
                select(AuditLog).where(AuditLog.action.in_({"polarrag.doc_delete", "polarrag.doc_rechunk"}))
            )
        ).scalars()
        contexts = {log.action: json.loads(json.loads(log.metadata_json)["client_info"]) for log in logs}
    assert set(contexts) == {
        "polarrag.doc_delete",
        "polarrag.doc_rechunk",
    }
    assert all(
        context["knowledge_resource_ids"] == [resource_id]
        and context["space_ids"] == ["space-a"]
        and context["kb_ids"] == ["public-kb"]
        for context in contexts.values()
    )


async def test_force_rechunk_requires_plugin_107(client, setup, monkeypatch) -> None:
    http, _admin_headers, member_headers = client
    factory, _admin, _member = setup
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    upstream = FakeDocumentManagementClient()
    monkeypatch.setattr("server.api.polarrag_documents.client_from_instance", lambda _instance: upstream)
    request = {"agent_id": agent_id, "knowledge_resource_id": resource_id, "force": True}

    async def member_access(session, _user, _agent_id, _resource_id, _space_id=None, _kb_id=None):
        resource = await session.get(KnowledgeResource, resource_id)
        instance = await session.get(PolarRAGInstance, resource.polarrag_instance_id)
        return SimpleNamespace(resources=[resource], instance=instance, acl_context={"identity_domain": "tenant-a", "principals": []})

    monkeypatch.setattr("server.api.polarrag_documents._member_access", member_access)

    async with factory() as session:
        resource = await session.get(KnowledgeResource, resource_id)
        instance = await session.get(PolarRAGInstance, resource.polarrag_instance_id)
        instance.plugin_version = "t-polardb-rag-1.0.6-20260914211000.al8.x86_64"
        await session.commit()

    unsupported = await http.post(
        "/api/me/polarrag/documents/doc-a/rechunk", json=request, headers=member_headers,
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"]["code"] == "POLARRAG_VERSION_UNSUPPORTED"
    assert upstream.calls == []

    async with factory() as session:
        resource = await session.get(KnowledgeResource, resource_id)
        instance = await session.get(PolarRAGInstance, resource.polarrag_instance_id)
        instance.plugin_version = "t-polardb-rag-1.0.7-20260914211000.al8.x86_64"
        await session.commit()

    supported = await http.post(
        "/api/me/polarrag/documents/doc-a/rechunk", json=request, headers=member_headers,
    )
    assert supported.status_code == 200
    assert upstream.calls[-1][0] == "rechunk_document"
    assert upstream.calls[-1][1]["force"] is True


async def test_member_document_list_includes_plugin_permission_details(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    _factory, _admin, member = setup
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    upstream = FakeDocumentPermissionClient()
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.post(
        "/api/me/polarrag/documents/_list",
        json={
            "agent_id": agent_id,
            "knowledge_resource_id": resource_id,
            "size": 20,
        },
        headers=member_headers,
    )
    found = await http.post(
        "/api/me/polarrag/documents/_find",
        json={
            "agent_id": agent_id,
            "knowledge_resource_id": resource_id,
            "filename": "guide",
        },
        headers=member_headers,
    )

    assert response.status_code == 200
    assert response.json()["documents"][0]["acl"] == {
        "identity_domain": "tenant-a",
        "state": "ACTIVE",
        "updating": False,
        "acl_version": 3,
        "acl_set_id": "acl-set-a",
        "grants": [
            {
                "principal": {
                    "provider": "feishu",
                    "type": "group",
                    "id": "group-member",
                },
                "actions": ["READ"],
            }
        ],
    }
    permission_call = upstream.calls[1]
    assert permission_call[0] == "list_document_permissions"
    assert permission_call[1]["doc_ids"] == ["doc-a"]
    assert permission_call[1]["acl_context"]["actor"] == {
        "provider": "polarrag",
        "type": "user",
        "id": member.external_id,
    }
    assert found.status_code == 200
    assert found.json()["documents"][0]["acl"]["grants"][0]["actions"] == [
        "READ"
    ]


async def test_document_mutation_hides_cross_resource_document(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    upstream = FakeDocumentManagementClient(kb_id="another-kb")
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.request(
        "DELETE",
        "/api/me/polarrag/documents/doc-a",
        json={"agent_id": agent_id, "knowledge_resource_id": resource_id},
        headers=member_headers,
    )

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "DOCUMENT_NOT_ACCESSIBLE"
    assert [name for name, _kwargs in upstream.calls] == ["document_info"]


async def test_document_mutation_maps_upstream_permission_without_leaking_body(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    agent_id = await _seed_document_agent(setup, resource_id)
    upstream = FakeDocumentManagementClient()

    async def denied(*_args, **_kwargs):
        raise PolarRAGUpstreamError(
            PolarRAGErrorCode.DOCUMENT_NOT_ACCESSIBLE,
            status_code=403,
        )

    upstream.delete_document = denied
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.request(
        "DELETE",
        "/api/me/polarrag/documents/doc-a",
        json={"agent_id": agent_id, "knowledge_resource_id": resource_id},
        headers=member_headers,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == {
        "code": "DOCUMENT_PERMISSION_DENIED",
        "message": "PolarRAG denied the required document permission.",
    }
    assert "secret" not in response.text.lower()


def test_validate_filename_rejects_cross_platform_paths() -> None:
    assert validate_filename("guide.md") == "guide.md"
    for filename in ("../guide.md", "folder/guide.md", "folder\\guide.md"):
        with pytest.raises(ValueError):
            validate_filename(filename)


async def test_oss_multipart_store_signs_exact_part_and_maps_sdk_results() -> None:
    class FakeBucket:
        def __init__(self) -> None:
            self.signed: tuple | None = None
            self.completed: tuple | None = None
            self.aborted: tuple | None = None

        def init_multipart_upload(self, key):
            assert key == "prefix/object.md"
            return SimpleNamespace(upload_id="upload-id")

        def sign_url(self, method, key, expires, **kwargs):
            self.signed = (method, key, expires, kwargs)
            return "https://upload.example.test/signed"

        def list_parts(self, key, upload_id, marker="", max_parts=1000, headers=None):
            assert (key, upload_id, marker, max_parts, headers) == (
                "prefix/object.md",
                "upload-id",
                "",
                1000,
                None,
            )
            return SimpleNamespace(
                is_truncated=False,
                parts=[SimpleNamespace(part_number=1, etag="etag-1", size=7)],
            )

        def complete_multipart_upload(self, key, upload_id, parts, headers=None):
            self.completed = (key, upload_id, parts, headers)

        def abort_multipart_upload(self, key, upload_id, headers=None):
            self.aborted = (key, upload_id, headers)

    bucket = FakeBucket()
    store = object.__new__(OssObjectStore)
    store._bucket = bucket

    assert await store.initiate_multipart("prefix/object.md") == "upload-id"
    assert (
        store.sign_part_url(
            "prefix/object.md",
            "upload-id",
            3,
            expires_seconds=900,
        )
        == "https://upload.example.test/signed"
    )
    assert bucket.signed == (
        "PUT",
        "prefix/object.md",
        900,
        {"params": {"uploadId": "upload-id", "partNumber": "3"}},
    )
    assert await store.list_multipart_parts("prefix/object.md", "upload-id") == [
        OssMultipartPart(part_number=1, etag="etag-1", size=7)
    ]
    await store.complete_multipart(
        "prefix/object.md",
        "upload-id",
        [OssMultipartPart(part_number=1, etag="etag-1", size=7)],
    )
    assert bucket.completed is not None
    assert bucket.completed[:2] == ("prefix/object.md", "upload-id")
    assert bucket.completed[2][0].part_number == 1
    assert bucket.completed[2][0].etag == "etag-1"
    await store.abort_multipart("prefix/object.md", "upload-id")
    assert bucket.aborted == ("prefix/object.md", "upload-id", None)


async def test_oss_abort_treats_missing_multipart_as_already_cleaned(
    monkeypatch,
) -> None:
    from server.polarrag import oss as oss_module

    class MissingUpload(Exception):
        pass

    class MissingBucket:
        def abort_multipart_upload(self, *_args, **_kwargs):
            raise MissingUpload

    monkeypatch.setattr(
        oss_module.oss2.exceptions,
        "NoSuchUpload",
        MissingUpload,
    )
    store = object.__new__(OssObjectStore)
    store._bucket = MissingBucket()

    await store.abort_multipart("prefix/object.md", "missing-upload")


@pytest.fixture
async def platform_batch_upload(client, setup, monkeypatch):
    from mcp.server.auth.provider import AccessToken
    from server.api.platform_oauth import platform_resource_url

    http, _, _ = client
    factory, _, member = setup
    resource_a = await _seed_upload_resource(setup)
    resource_b = await _seed_additional_upload_resource(setup, resource_a, "secondary-kb")
    agent_id = await _seed_document_agent(setup, resource_a)
    store = FakeObjectStore()
    upstream = FakeBatchSubmitClient()
    monkeypatch.setattr("server.api.polarrag_documents.object_store_from_space", lambda _: store)
    monkeypatch.setattr("server.api.polarrag_documents.client_from_instance", lambda _: upstream)

    async def load_token(self, token):
        if token != "platform-test-token":
            return None
        return AccessToken(
            token=token, client_id="test", subject=f"user:{member.id}",
            scopes=["polarrag"], resource=platform_resource_url(), claims={"agent_id": agent_id},
        )

    monkeypatch.setattr("server.api.platform_oauth.PASAuthProvider.load_access_token", load_token)
    return SimpleNamespace(
        http=http, factory=factory, member=member, agent_id=agent_id,
        resource_ids=[resource_a, resource_b], store=store, upstream=upstream,
        headers={"Authorization": "Bearer platform-test-token"},
    )


@pytest.mark.parametrize("partial", [False, True])
async def test_platform_space_batch_upload_preserves_file_kb_order(platform_batch_upload, partial):
    env = platform_batch_upload
    names = ["denied.md" if partial else "a.md", "b.md", "c.md"]
    response = await env.http.post(
        "/api/v1/spaces/space-a/documents/batch",
        data={"kb_ids": ["secondary-kb", "public-kb", "secondary-kb"]},
        files=[("files", (name, body, "text/markdown")) for name, body in zip(names, [b"alpha", b"beta", b"gamma"])],
        headers=env.headers,
    )
    assert response.status_code == (207 if partial else 201), response.text
    payload = response.json()
    assert [row["index"] for row in payload["accepted"]] == ([1, 2] if partial else [0, 1, 2])
    expected_ids = [env.resource_ids[1], env.resource_ids[0], env.resource_ids[1]]
    for row in payload["accepted"]:
        assert row["knowledge_resource_id"] == expected_ids[row["index"]]
        assert row["filename"] == names[row["index"]]
    assert [body for _, body in env.store.uploads] == [b"alpha", b"beta", b"gamma"]
    assert len(env.upstream.bulk_calls) == 1
    bulk = env.upstream.bulk_calls[0]
    assert [row["kb_id"] for row in bulk["documents"]] == ["secondary-kb", "public-kb", "secondary-kb"]
    assert bulk["acl_context"]["actor"]["id"] == env.member.external_id
    if partial:
        assert payload["failed"][0]["index"] == 0
        assert payload["failed"][0]["knowledge_resource_id"] == env.resource_ids[1]
        assert payload["failed"][0]["code"] == "POLARRAG_DOCUMENT_SUBMIT_FAILED"
        assert env.store.deleted == [env.store.uploads[0][0]]
    else:
        assert payload["failed"] == []


@pytest.mark.parametrize("file_count,kb_count", [(1, 2), (2, 1), (21, 21), (1, 0)])
async def test_platform_space_batch_upload_rejects_invalid_counts(platform_batch_upload, file_count, kb_count):
    env = platform_batch_upload
    response = await env.http.post(
        "/api/v1/spaces/space-a/documents/batch",
        data={"kb_ids": ["public-kb"] * kb_count},
        files=[("files", (f"{i}.md", b"content", "text/markdown")) for i in range(file_count)],
        headers=env.headers,
    )
    assert response.status_code == 422, response.text
    if 0 < kb_count <= 20 and file_count <= 20:
        assert response.json()["detail"]["code"] == "BATCH_UPLOAD_LENGTH_MISMATCH"
    assert env.store.uploads == []
    assert env.upstream.bulk_calls == []


@pytest.mark.parametrize("denial", ["token", "user", "agent", "kb"])
async def test_platform_space_batch_upload_preserves_authorization(platform_batch_upload, denial):
    from server.models import User, UserStatus

    env = platform_batch_upload
    headers = env.headers if denial != "token" else {}
    async with env.factory() as session:
        if denial == "user":
            user = await session.get(User, env.member.id)
            user.status = UserStatus.DISABLED
        elif denial == "agent":
            assignment = await session.scalar(select(AgentUserAssignment).where(AgentUserAssignment.agent_id == env.agent_id))
            await session.delete(assignment)
        elif denial == "kb":
            binding = await session.scalar(select(AgentPolarRAGInstanceBinding).where(AgentPolarRAGInstanceBinding.agent_id == env.agent_id))
            binding.public_knowledge_resource_ids_json = json.dumps([env.resource_ids[0]])
        await session.commit()
    response = await env.http.post(
        "/api/v1/spaces/space-a/documents/batch",
        data={"kb_ids": ["public-kb", "secondary-kb"]},
        files=[("files", ("a.md", b"a", "text/markdown")), ("files", ("b.md", b"b", "text/markdown"))],
        headers=headers,
    )
    assert response.status_code == (404 if denial == "kb" else 401), response.text
    assert env.store.uploads == []
    assert env.upstream.bulk_calls == []
