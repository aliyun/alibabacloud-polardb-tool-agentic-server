from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from server.core.crypto import encrypt
from server.models import (
    AuditLog,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalType,
    KnowledgeBindingMode,
    KnowledgeResource,
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
        self.calls.append(
            ("list_documents", {"space_id": space_id, **kwargs})
        )
        return {
            "documents": [
                {
                    "doc_id": "doc-a",
                    "kb_id": self.kb_id,
                    "filename": "guide.md",
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


async def test_member_upload_uses_server_resource_oss_and_identity(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, member_headers = client
    factory, _admin, _member = setup
    resource_id = await _seed_upload_resource(setup)
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
        "knowledge_resource_id": resource_id,
        "space_id": "attacker-space",
        "kb_id": "attacker-kb",
        "oss_path": "oss://attacker/secret",
        "acl_context": '{"identity_domain":"attacker"}',
    }

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
        ],
        "actor": {"provider": "feishu", "type": "user", "id": "ou-member"},
    }
    assert "acl" not in upstream.calls[0]
    async with factory() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "polarrag.doc_upload")
            )
        ).scalar_one()
        assert audit.status.value == "success"
        assert "oss-ak" not in (audit.metadata_json or "")
        context = json.loads(json.loads(audit.metadata_json)["client_info"])
        assert context["knowledge_resource_ids"] == [resource_id]
        assert context["space_ids"] == ["space-a"]
        assert context["kb_ids"] == ["public-kb"]


async def test_submit_failure_deletes_uploaded_object_and_hides_upstream_body(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
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
        data={"knowledge_resource_id": resource_id},
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
    for filename in ("../guide.md", "folder\\guide.md", "README"):
        response = await http.post(
            "/api/me/polarrag/documents",
            data={"knowledge_resource_id": resource_id},
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
                {"provider": "feishu", "type": "group", "id": "group-a"},
            ]
        }
    )

    assert actor == {"provider": "feishu", "type": "user", "id": "ou-member"}
    with pytest.raises(ValueError):
        document_actor({"principals": []})
    with pytest.raises(ValueError):
        document_actor(
            {"principals": [{"provider": "feishu", "type": "user"}]}
        )


async def test_member_finds_and_mutates_only_documents_in_selected_resource(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, member_headers = client
    factory, _admin, _member = setup
    resource_id = await _seed_upload_resource(setup)
    upstream = FakeDocumentManagementClient()
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    listed = await http.post(
        "/api/me/polarrag/documents/_list",
        json={
            "knowledge_resource_id": resource_id,
            "size": 20,
            "after_doc_id": "doc-before",
        },
        headers=member_headers,
    )
    found = await http.post(
        "/api/me/polarrag/documents/_find",
        json={
            "knowledge_resource_id": resource_id,
            "filename": "guide",
            "limit": 20,
        },
        headers=member_headers,
    )
    forbidden = await http.post(
        "/api/me/polarrag/documents/_find",
        json={
            "knowledge_resource_id": resource_id,
            "filename": "guide",
        },
        headers=admin_headers,
    )
    injected = await http.post(
        "/api/me/polarrag/documents/_find",
        json={
            "knowledge_resource_id": resource_id,
            "filename": "guide",
            "acl_context": {"identity_domain": "attacker"},
        },
        headers=member_headers,
    )
    deleted = await http.request(
        "DELETE",
        "/api/me/polarrag/documents/doc-a",
        json={"knowledge_resource_id": resource_id},
        headers=member_headers,
    )
    rechunked = await http.post(
        "/api/me/polarrag/documents/doc-a/rechunk",
        json={
            "knowledge_resource_id": resource_id,
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
                "acl_context": trusted_context,
            },
        ),
    ]
    async with factory() as session:
        logs = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action.in_(
                        {"polarrag.doc_delete", "polarrag.doc_rechunk"}
                    )
                )
            )
        ).scalars()
        contexts = {
            log.action: json.loads(json.loads(log.metadata_json)["client_info"])
            for log in logs
        }
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


async def test_document_mutation_hides_cross_resource_document(
    client,
    setup,
    monkeypatch,
) -> None:
    http, _admin_headers, member_headers = client
    resource_id = await _seed_upload_resource(setup)
    upstream = FakeDocumentManagementClient(kb_id="another-kb")
    monkeypatch.setattr(
        "server.api.polarrag_documents.client_from_instance",
        lambda _instance: upstream,
    )

    response = await http.request(
        "DELETE",
        "/api/me/polarrag/documents/doc-a",
        json={"knowledge_resource_id": resource_id},
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
        json={"knowledge_resource_id": resource_id},
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
    assert store.sign_part_url(
        "prefix/object.md",
        "upload-id",
        3,
        expires_seconds=900,
    ) == "https://upload.example.test/signed"
    assert bucket.signed == (
        "PUT",
        "prefix/object.md",
        900,
        {"params": {"uploadId": "upload-id", "partNumber": "3"}},
    )
    assert await store.list_multipart_parts(
        "prefix/object.md", "upload-id"
    ) == [OssMultipartPart(part_number=1, etag="etag-1", size=7)]
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
