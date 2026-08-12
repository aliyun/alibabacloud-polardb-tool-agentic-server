from __future__ import annotations

from server.models import (
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

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_my_resources_returns_only_user_visible_knowledge_resources(
    client,
    setup,
) -> None:
    http, _admin_headers, member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        session.add(
            EnterprisePrincipalAssignment.create(
                pas_user_id=member.id,
                identity_domain="tenant-a",
                provider="feishu",
                principal_type=EnterprisePrincipalType.USER,
                principal_id="member-a",
                source=EnterprisePrincipalSource.ADMIN_MANAGED,
            )
        )
        instance = PolarRAGInstance(
            name="RAG A",
            scheme="http",
            host="rag.example.test",
            port=9200,
            username_ciphertext="username",
            password_ciphertext="password",
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
            enabled=True,
        )
        session.add(space)
        await session.flush()
        session.add_all(
            [
                KnowledgeResource(
                    knowledge_space_id=space.knowledge_space_id,
                    polarrag_instance_id=instance.id,
                    space_id=space.space_id,
                    kb_id="public-kb",
                    name="Public KB",
                    kb_type="PUBLIC",
                    identity_domain="tenant-a",
                    binding_mode=KnowledgeBindingMode.DOMAIN,
                    sync_status=KnowledgeResourceSyncStatus.ACTIVE,
                    enabled=True,
                ),
                KnowledgeResource(
                    knowledge_space_id=space.knowledge_space_id,
                    polarrag_instance_id=instance.id,
                    space_id=space.space_id,
                    kb_id="owner-kb",
                    name="Another user's KB",
                    kb_type="PERSONAL",
                    identity_domain="tenant-a",
                    binding_mode=KnowledgeBindingMode.OWNER,
                    owner_pas_user_id=admin.id,
                    sync_status=KnowledgeResourceSyncStatus.ACTIVE,
                    enabled=True,
                ),
            ]
        )
        await session.commit()

    response = await http.get("/api/me/resources", headers=member_headers)

    assert response.status_code == 200
    assert response.json()["database_instances"] == []
    assert response.json()["knowledge_resources"] == [
        {
            "knowledge_resource_id": response.json()["knowledge_resources"][0][
                "knowledge_resource_id"
            ],
            "knowledge_space_id": space.knowledge_space_id,
            "knowledge_space_name": "Space A",
            "polarrag_instance_id": instance.id,
            "polarrag_instance_name": "RAG A",
            "name": "Public KB",
                "kb_type": "PUBLIC",
                "usage": None,
                "upload_ready": False,
            }
        ]


async def test_my_resources_requires_an_authenticated_user(client) -> None:
    http, _admin_headers, _member_headers = client

    response = await http.get("/api/me/resources")

    assert response.status_code == 401
