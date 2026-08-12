from __future__ import annotations

from sqlalchemy import select

from server.core.crypto import decrypt
from server.models import (
    AuditLog,
    EnterprisePrincipalAssignment,
    KnowledgeResource,
    PolarRAGInstance,
    PolarRAGSpace,
)
from server.polarrag.contracts import (
    PolarRAGCapabilities,
    PolarRAGKnowledgeBaseRecord,
    PolarRAGSpaceRecord,
)

pytest_plugins = ("tests._admin_api_fixtures",)


class FakeAdminClient:
    def __init__(self) -> None:
        self.oss_bucket = "tenant-a-documents"
        self.oss_endpoint = "oss-cn-hangzhou.aliyuncs.com"

    async def check_capabilities(self):
        return PolarRAGCapabilities(
            version="5faf397",
            search=True,
            protected_document_info=True,
            protected_context=True,
            protected_document_search=True,
            space_catalog=True,
            knowledge_base_catalog=True,
        )

    async def list_spaces(self):
        return [
            PolarRAGSpaceRecord(
                space_id="space-a",
                name="Space A",
                identity_domain="tenant-a",
                status="ACTIVE",
                oss_bucket=self.oss_bucket,
                oss_endpoint=self.oss_endpoint,
            )
        ]

    async def list_knowledge_bases(self, space_id):
        assert space_id == "space-a"
        return [
            PolarRAGKnowledgeBaseRecord(
                space_id="space-a",
                kb_id="public-kb",
                name="Public KB",
                kb_type="PUBLIC",
                identity_domain="tenant-a",
                owner=None,
            )
        ]


class FakeClaimClient(FakeAdminClient):
    def __init__(self) -> None:
        super().__init__()
        self.claimed = False
        self.claim_context = None

    async def list_knowledge_bases(self, space_id):
        assert space_id == "space-a"
        if not self.claimed:
            return []
        return [
            PolarRAGKnowledgeBaseRecord(
                space_id="space-a",
                kb_id="personal-kb",
                name="Personal KB",
                kb_type="PERSONAL",
                identity_domain="tenant-a",
                owner={
                    "provider": "feishu",
                    "type": "user",
                    "id": "ou-owner",
                },
                status="ACTIVE",
            )
        ]

    async def list_unclaimed_knowledge_bases(self, space_id):
        assert space_id == "space-a"
        if self.claimed:
            return []
        return [
            PolarRAGKnowledgeBaseRecord(
                space_id="space-a",
                kb_id="personal-kb",
                name="Personal KB",
                kb_type="PERSONAL",
                identity_domain="tenant-a",
                owner=None,
                status="UNCLAIMED",
            )
        ]

    async def claim_knowledge_base(self, space_id, kb_id, *, acl_context):
        assert (space_id, kb_id) == ("space-a", "personal-kb")
        self.claim_context = acl_context
        self.claimed = True


async def test_admin_registers_encrypted_instance_and_enables_trusted_space(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, member_headers = client
    factory, _admin, _member = setup
    fake = FakeAdminClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    secret = "do-not-return"

    forbidden = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": secret,
            "tls_verify": True,
        },
        headers=member_headers,
    )
    assert forbidden.status_code == 403

    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": secret,
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    assert secret not in created.text
    assert "username" not in created.json()
    instance_id = created.json()["id"]

    spaces = await http.get(
        f"/api/polarrag/instances/{instance_id}/spaces",
        headers=admin_headers,
    )
    assert spaces.status_code == 200
    discovered_space = spaces.json()["items"][0]
    assert discovered_space["identity_domain"] == "tenant-a"
    assert discovered_space["oss_bucket"] == "tenant-a-documents"
    assert discovered_space["oss_endpoint"] == "oss-cn-hangzhou.aliyuncs.com"
    assert discovered_space["enabled"] is False
    assert discovered_space["knowledge_space_id"] is None

    enabled = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "space-a"},
        headers=admin_headers,
    )
    assert enabled.status_code == 200
    assert enabled.json()["sync"]["active"] == 1

    spaces = await http.get(
        f"/api/polarrag/instances/{instance_id}/spaces",
        headers=admin_headers,
    )
    enabled_space = spaces.json()["items"][0]
    assert enabled_space["enabled"] is True
    assert enabled_space["knowledge_space_id"] == enabled.json()[
        "knowledge_space_id"
    ]
    assert enabled_space["last_synced_at"] is not None
    assert enabled_space["knowledge_resources"] == [
        {
            "knowledge_resource_id": enabled_space["knowledge_resources"][0][
                "knowledge_resource_id"
            ],
            "name": "Public KB",
            "kb_type": "PUBLIC",
            "binding_mode": "domain",
            "sync_status": "active",
            "enabled": True,
        }
    ]

    validation_calls: list[dict[str, str]] = []

    async def validate_oss(**kwargs) -> None:
        validation_calls.append(kwargs)

    monkeypatch.setattr(
        "server.api.polarrag.validate_oss_write_access",
        validate_oss,
    )
    oss_payload = {
        "access_key_id": "  test-access-key  ",
        "access_key_secret": "test-secret-key",
        "object_prefix": "pas/documents",
    }
    forbidden_oss = await http.put(
        f"/api/polarrag/spaces/{enabled_space['knowledge_space_id']}/oss-config",
        json=oss_payload,
        headers=member_headers,
    )
    assert forbidden_oss.status_code == 403

    configured_oss = await http.put(
        f"/api/polarrag/spaces/{enabled_space['knowledge_space_id']}/oss-config",
        json=oss_payload,
        headers=admin_headers,
    )
    assert configured_oss.status_code == 200
    assert configured_oss.json()["bucket"] == "tenant-a-documents"
    assert configured_oss.json()["endpoint"] == "oss-cn-hangzhou.aliyuncs.com"
    assert configured_oss.json()["validated"] is True
    assert "access_key" not in configured_oss.text
    assert "test-secret-key" not in configured_oss.text
    assert validation_calls == [
        {
            "endpoint": "oss-cn-hangzhou.aliyuncs.com",
            "bucket": "tenant-a-documents",
            "access_key_id": "test-access-key",
            "access_key_secret": "test-secret-key",
            "object_prefix": "pas/documents",
        }
    ]

    fake.oss_endpoint = "oss-cn-beijing.aliyuncs.com"
    refreshed_spaces = await http.get(
        f"/api/polarrag/instances/{instance_id}/spaces",
        headers=admin_headers,
    )
    assert refreshed_spaces.status_code == 200
    assert refreshed_spaces.json()["items"][0]["oss_bucket"] == "tenant-a-documents"
    assert refreshed_spaces.json()["items"][0]["oss_endpoint"] == (
        "oss-cn-beijing.aliyuncs.com"
    )

    disabled = await http.delete(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a",
        headers=admin_headers,
    )
    assert disabled.status_code == 204

    async with factory() as session:
        instance = await session.get(PolarRAGInstance, instance_id)
        assert decrypt(instance.username_ciphertext) == "shared"
        assert decrypt(instance.password_ciphertext) == secret
        space = (
            await session.execute(select(PolarRAGSpace))
        ).scalar_one()
        resource = (
            await session.execute(select(KnowledgeResource))
        ).scalar_one()
        assert space.identity_domain == "tenant-a"
        assert space.oss_bucket == "tenant-a-documents"
        assert space.oss_endpoint == "oss-cn-beijing.aliyuncs.com"
        assert space.oss_config_validated is False
        assert space.oss_last_error_code == "OSS_CATALOG_CHANGED"
        assert decrypt(space.oss_access_key_id_ciphertext) == "test-access-key"
        assert decrypt(space.oss_access_key_secret_ciphertext) == "test-secret-key"
        assert resource.kb_id == "public-kb"
        assert space.enabled is False
        assert resource.enabled is False


async def test_principal_api_enforces_provider_and_user_uniqueness(
    client,
    setup,
) -> None:
    http, admin_headers, member_headers = client
    factory, _admin, member = setup
    invalid = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "caller-controlled",
            "principal_type": "user",
            "principal_id": "id-1",
        },
        headers=admin_headers,
    )
    assert invalid.status_code == 422

    blank = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": " ",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": " ",
        },
        headers=admin_headers,
    )
    assert blank.status_code == 422

    created = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": "ou-1",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201

    second_user = await http.post(
        "/api/users",
        json={
            "username": "second",
            "password": "strong-password",
        },
        headers=admin_headers,
    )
    assert second_user.status_code == 201
    conflict = await http.post(
        f"/api/polarrag/users/{second_user.json()['id']}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": "ou-1",
        },
        headers=admin_headers,
    )
    assert conflict.status_code == 409

    async with factory() as session:
        assignments = (
            await session.execute(select(EnterprisePrincipalAssignment))
        ).scalars().all()
        assert len(assignments) == 1


async def test_admin_assigns_unclaimed_kb_owner_and_synchronizes_catalog(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, member_headers = client
    factory, admin, member = setup
    fake = FakeClaimClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "not-returned",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    instance_id = created.json()["id"]
    enabled = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "space-a"},
        headers=admin_headers,
    )
    assert enabled.status_code == 200
    principal = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": "ou-owner",
        },
        headers=admin_headers,
    )
    principal_id = principal.json()["id"]

    forbidden = await http.get(
        f"/api/polarrag/instances/{instance_id}/unclaimed-knowledge-bases",
        headers=member_headers,
    )
    assert forbidden.status_code == 403

    wrong_domain = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-b",
            "provider": "feishu",
            "principal_type": "user",
            "principal_id": "ou-wrong-domain",
        },
        headers=admin_headers,
    )
    assert wrong_domain.status_code == 201
    rejected = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"principal_assignment_id": wrong_domain.json()["id"]},
        headers=admin_headers,
    )
    assert rejected.status_code == 422
    assert fake.claim_context is None

    pending = await http.get(
        f"/api/polarrag/instances/{instance_id}/unclaimed-knowledge-bases",
        headers=admin_headers,
    )
    assert pending.status_code == 200
    assert pending.json() == {
        "items": [
            {
                "space_id": "space-a",
                "space_name": "Space A",
                "identity_domain": "tenant-a",
                "kb_id": "personal-kb",
                "name": "Personal KB",
                "kb_type": "PERSONAL",
                "status": "UNCLAIMED",
            }
        ],
        "owner_candidates": [
            {
                "principal_assignment_id": principal_id,
                "pas_user_id": member.id,
                "user_name": "Member",
                "identity_domain": "tenant-a",
                "provider": "feishu",
                "principal_id": "ou-owner",
            }
        ],
    }

    claimed = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"principal_assignment_id": principal_id},
        headers=admin_headers,
    )
    assert claimed.status_code == 200
    assert claimed.json() == {
        "kb_id": "personal-kb",
        "status": "ACTIVE",
        "sync": {"active": 1, "disabled": 0, "owner_unresolved": 0},
    }
    assert fake.claim_context == {
        "identity_domain": "tenant-a",
        "actor": {"provider": "feishu", "type": "user", "id": "ou-owner"},
        "principals": [
            {"provider": "feishu", "type": "user", "id": "ou-owner"}
        ],
    }
    async with factory() as session:
        resource = (await session.execute(select(KnowledgeResource))).scalar_one()
        assert resource.kb_id == "personal-kb"
        assert resource.enabled is True
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "polarrag.knowledge_base.claim"
                )
            )
        ).scalar_one()
        assert audit.actor_user_id == admin.id
        assert audit.target_id == "space-a/personal-kb"
