from __future__ import annotations

import pytest
from sqlalchemy import select

from server.core.crypto import decrypt
from server.models import (
    AuditLog,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalStatus,
    EnterprisePrincipalType,
    KnowledgeResource,
    PolarRAGInstance,
    PolarRAGSpace,
    AuthProvider,
    User,
)
from server.polarrag.contracts import (
    PolarRAGCapabilities,
    PolarRAGErrorCode,
    PolarRAGKnowledgeBaseRecord,
    PolarRAGSpaceRecord,
    PolarRAGUpstreamError,
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


class MissingIdentityDomainClient(FakeAdminClient):
    async def list_spaces(self):
        return [
            PolarRAGSpaceRecord(
                space_id="legacy-space",
                name="Legacy Space",
                identity_domain=None,
                status="ACTIVE",
            )
        ]


class FakeClaimClient(FakeAdminClient):
    def __init__(self) -> None:
        super().__init__()
        self.claimed = False
        self.claim_owner = None

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

    async def claim_knowledge_base(self, space_id, kb_id, *, owner):
        assert (space_id, kb_id) == ("space-a", "personal-kb")
        self.claim_owner = owner
        self.claimed = True


class RetryableClaimClient(FakeClaimClient):
    def __init__(self) -> None:
        super().__init__()
        self.claim_calls = 0
        self.fail_sync_once = True

    async def list_knowledge_bases(self, space_id):
        if self.claimed and self.fail_sync_once:
            self.fail_sync_once = False
            raise PolarRAGUpstreamError(
                PolarRAGErrorCode.UNAVAILABLE,
                retryable=True,
            )
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
                    "provider": "polarrag",
                    "type": "user",
                    "id": self.claim_owner,
                },
                status="ACTIVE",
            )
        ]

    async def claim_knowledge_base(self, space_id, kb_id, *, owner):
        self.claim_calls += 1
        await super().claim_knowledge_base(
            space_id,
            kb_id,
            owner=owner,
        )


class MismatchedCatalogClient(FakeAdminClient):
    async def list_unclaimed_knowledge_bases(self, space_id):
        assert space_id == "space-a"
        return [
            PolarRAGKnowledgeBaseRecord(
                space_id="space-a",
                kb_id="foreign-personal-kb",
                name="Foreign Personal KB",
                kb_type="PERSONAL",
                identity_domain="tenant-b",
                owner=None,
                status="UNCLAIMED",
            )
        ]


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


async def test_admin_lists_unconfigured_space_but_cannot_enable_it(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: MissingIdentityDomainClient(),
    )

    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "Legacy RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "secret",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    instance_id = created.json()["id"]

    spaces = await http.get(
        f"/api/polarrag/instances/{instance_id}/spaces",
        headers=admin_headers,
    )
    assert spaces.status_code == 200
    items = spaces.json()["items"]
    assert len(items) == 1
    assert items[0]["space_id"] == "legacy-space"
    assert items[0]["identity_domain"] is None
    assert items[0]["enabled"] is False

    enabled = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "legacy-space"},
        headers=admin_headers,
    )
    assert enabled.status_code == 409
    assert enabled.json()["detail"]["code"] == "POLARRAG_IDENTITY_DOMAIN_UNAVAILABLE"


async def test_instance_create_rolls_back_when_required_audit_fails(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, _member = setup
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: FakeAdminClient(),
    )

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit storage unavailable")

    monkeypatch.setattr("server.api.polarrag.log_audit", fail_audit)

    with pytest.raises(RuntimeError, match="audit storage unavailable"):
        await http.post(
            "/api/polarrag/instances",
            json={
                "name": "Atomic RAG",
                "scheme": "https",
                "host": "rag.example.test",
                "port": 9200,
                "username": "shared",
                "password": "not-returned",
                "tls_verify": True,
            },
            headers=admin_headers,
        )

    async with factory() as session:
        assert (
            await session.execute(select(PolarRAGInstance))
        ).scalar_one_or_none() is None


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

    native_spoof = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "polarrag",
            "principal_type": "user",
            "principal_id": member.id,
        },
        headers=admin_headers,
    )
    assert native_spoof.status_code == 422

    native_group = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "polarrag",
            "principal_type": "group",
            "principal_id": member.external_id,
        },
        headers=admin_headers,
    )
    assert native_group.status_code == 422

    native = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "polarrag",
            "principal_type": "user",
            "principal_id": member.external_id,
        },
        headers=admin_headers,
    )
    assert native.status_code == 201
    assert native.json()["principal_id"] == member.external_id

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
        assert len(assignments) == 2


@pytest.mark.parametrize(
    ("auth_provider", "external_id"),
    [
        ("builtin", "builtin-user "),
        ("oidc", "oidc:user "),
    ],
)
async def test_principal_api_preserves_native_user_external_id_whitespace(
    client,
    setup,
    auth_provider,
    external_id,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, member = setup
    async with factory() as session:
        stored_member = await session.get(User, member.id)
        assert stored_member is not None
        stored_member.external_id = external_id
        if auth_provider == "oidc":
            stored_member.auth_provider = AuthProvider.OIDC
        await session.commit()

    response = await http.post(
        f"/api/polarrag/users/{member.id}/principals",
        json={
            "identity_domain": "tenant-a",
            "provider": "polarrag",
            "principal_type": "user",
            "principal_id": external_id,
        },
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert response.json()["principal_id"] == external_id


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
    async with factory() as session:
        spoof = EnterprisePrincipalAssignment(
            pas_user_id=member.id,
            identity_domain="tenant-a",
            provider="polarrag",
            principal_type=EnterprisePrincipalType.USER,
            principal_id="another-user",
            source=EnterprisePrincipalSource.ADMIN_MANAGED,
            status=EnterprisePrincipalStatus.ACTIVE,
            user_principal_key="spoofed-native-owner",
        )
        session.add(spoof)
        await session.commit()
        spoof_id = spoof.id

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
    spoofed_owner = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"principal_assignment_id": spoof_id},
        headers=admin_headers,
    )
    assert spoofed_owner.status_code == 422
    rejected = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"principal_assignment_id": wrong_domain.json()["id"]},
        headers=admin_headers,
    )
    assert rejected.status_code == 422
    assert fake.claim_owner is None

    pending = await http.get(
        f"/api/polarrag/instances/{instance_id}/unclaimed-knowledge-bases",
        headers=admin_headers,
    )
    assert pending.status_code == 200
    assert pending.json()["items"] == [
        {
            "space_id": "space-a",
            "space_name": "Space A",
            "identity_domain": "tenant-a",
            "kb_id": "personal-kb",
            "name": "Personal KB",
            "kb_type": "PERSONAL",
            "status": "UNCLAIMED",
        }
    ]
    assert {
        "principal_assignment_id": principal_id,
        "pas_user_id": member.id,
        "user_name": "Member",
        "user_external_id": member.external_id,
        "identity_domain": "tenant-a",
        "provider": "feishu",
        "principal_id": "ou-owner",
    } in pending.json()["owner_candidates"]

    claimed = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"principal_assignment_id": principal_id},
        headers=admin_headers,
    )
    assert claimed.status_code == 200
    assert claimed.json() == {
        "kb_id": "personal-kb",
        "status": "ACTIVE",
        "sync": {
            "knowledge_bases": 1,
            "active": 1,
            "disabled": 0,
            "owner_unresolved": 0,
        },
    }
    assert fake.claim_owner == member.external_id
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


async def test_admin_assigns_unclaimed_kb_to_active_native_pas_user(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    _factory, _admin, member = setup
    fake = FakeClaimClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "RAG native owner",
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

    pending = await http.get(
        f"/api/polarrag/instances/{instance_id}/unclaimed-knowledge-bases",
        headers=admin_headers,
    )

    assert pending.status_code == 200
    native_owner = next(
        candidate
        for candidate in pending.json()["owner_candidates"]
        if candidate["pas_user_id"] == member.id
        and candidate["provider"] == "polarrag"
        and candidate["identity_domain"] == "tenant-a"
    )
    assert native_owner["principal_assignment_id"] is None
    claimed = await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/knowledge-bases/personal-kb/claim",
        json={"pas_user_id": member.id},
        headers=admin_headers,
    )

    assert claimed.status_code == 200
    assert fake.claim_owner == member.external_id


async def test_claim_retry_recovers_after_upstream_success_and_sync_failure(
    client,
    setup,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    factory, _admin, member = setup
    fake = RetryableClaimClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    instance = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "Retry RAG",
            "scheme": "https",
            "host": "rag.example.test",
            "port": 9200,
            "username": "shared",
            "password": "not-returned",
            "tls_verify": True,
        },
        headers=admin_headers,
    )
    instance_id = instance.json()["id"]
    await http.post(
        f"/api/polarrag/instances/{instance_id}/spaces/enable",
        json={"space_id": "space-a"},
        headers=admin_headers,
    )
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
    endpoint = (
        f"/api/polarrag/instances/{instance_id}/spaces/space-a/"
        "knowledge-bases/personal-kb/claim"
    )

    first = await http.post(
        endpoint,
        json={"principal_assignment_id": principal.json()["id"]},
        headers=admin_headers,
    )
    second = await http.post(
        endpoint,
        json={"principal_assignment_id": principal.json()["id"]},
        headers=admin_headers,
    )

    assert first.status_code == 503
    assert second.status_code == 200
    assert fake.claim_calls == 1
    async with factory() as session:
        resource = (
            await session.execute(
                select(KnowledgeResource).where(
                    KnowledgeResource.kb_id == "personal-kb"
                )
            )
        ).scalar_one()
        assert resource.owner_pas_user_id == member.id


async def test_unclaimed_catalog_rejects_identity_boundary_as_upstream_error(
    client,
    monkeypatch,
) -> None:
    http, admin_headers, _member_headers = client
    fake = MismatchedCatalogClient()
    monkeypatch.setattr(
        "server.api.polarrag.client_from_instance",
        lambda _instance: fake,
    )
    created = await http.post(
        "/api/polarrag/instances",
        json={
            "name": "Boundary RAG",
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

    response = await http.get(
        f"/api/polarrag/instances/{instance_id}/unclaimed-knowledge-bases",
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "POLARRAG_INVALID_RESPONSE",
            "message": "POLARRAG_INVALID_RESPONSE",
        }
    }
