from types import SimpleNamespace

from server.api.features import options
from server.models import (
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_knowledge_options_page_each_candidate_collection(setup):
    factory, admin, _member = setup
    async with factory() as session:
        instance = PolarRAGInstance(
            name="Options RAG",
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
            space_id="options-space",
            name="Options Space",
            identity_domain="options-domain",
            enabled=True,
        )
        session.add(space)
        await session.flush()
        session.add_all(
            KnowledgeResource(
                knowledge_space_id=space.knowledge_space_id,
                polarrag_instance_id=instance.id,
                space_id=space.space_id,
                kb_id=f"kb-{index}",
                name=f"Option KB {index}",
                kb_type="PUBLIC",
                identity_domain=space.identity_domain,
                binding_mode=KnowledgeBindingMode.DOMAIN,
                sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            )
            for index in range(3)
        )
        await session.commit()

    payload = await options(
        connection_offset=0,
        connection_limit=1,
        connection_search=None,
        resource_offset=1,
        resource_limit=1,
        resource_search="Option KB",
        user_offset=0,
        user_limit=1,
        user_search=None,
        _admin=admin,
        service=SimpleNamespace(
            repository=SimpleNamespace(session_factory=factory)
        ),
    )

    assert payload["connection_total"] == 1
    assert payload["connection_offset"] == 0
    assert payload["connection_limit"] == 1
    assert len(payload["connections"]) == 1
    assert payload["resource_total"] == 3
    assert payload["resource_offset"] == 1
    assert payload["resource_limit"] == 1
    assert [item["name"] for item in payload["resources"]] == ["Option KB 1"]
    assert payload["user_total"] == 2
    assert payload["user_offset"] == 0
    assert payload["user_limit"] == 1
    assert len(payload["users"]) == 1
