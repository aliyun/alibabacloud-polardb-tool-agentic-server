from __future__ import annotations

import pytest


from tests.test_knowledge_visibility_bind import catalog as catalog

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_public_scope_pair_replacement(client, catalog):
    http, headers, _ = client
    agent, binding, ids, _, _ = catalog
    path = f"/api/agents/{agent}/polarrag-bindings/{binding}/public-resources"
    for value, expected in [([{"space_id": "space", "kb_id": "kb-0"}], [ids[0]]), ([], []), (None, None)]:
        response = await http.put(path, headers=headers, json={"targets": value})
        assert response.status_code == 200, response.text
        assert response.json()["public_knowledge_resource_ids"] == expected
    response = await http.put(path, headers=headers, json={"targets": None, "public_knowledge_resource_ids": []})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("suffix", "extra"),
    [
        ("knowledge-binding-status", {}),
        ("knowledge-subject-options", {"category": "users"}),
        ("knowledge-all-users-status", {}),
        ("knowledge-all-users", {"operation": "BIND"}),
    ],
)
async def test_admin_knowledge_endpoints_accept_pairs(client, setup, catalog, suffix, extra):
    from server.models import AgentPolarRAGInstanceBinding

    http, headers, _ = client
    async with setup[0]() as session:
        binding = await session.get(AgentPolarRAGInstanceBinding, catalog[1])
        binding.public_knowledge_resource_ids_json = None
        await session.commit()
    if suffix == "knowledge-binding-status":
        extra = {"subjects": [{"type": "USER", "user_id": setup[2].id}]}
    response = await http.post(
        f"/api/admin/agents/{catalog[0]}/{suffix}",
        headers=headers,
        json={"targets": [{"space_id": "space", "kb_id": "kb-0"}], **extra},
    )
    assert response.status_code == 200, response.text
    if suffix != "knowledge-subject-options":
        assert response.json()["items"][0]["knowledge_resource_id"] == catalog[2][0]


@pytest.mark.parametrize("suffix", [
    "knowledge-binding-status", "knowledge-subject-options",
    "knowledge-all-users-status", "knowledge-all-users",
])
async def test_admin_knowledge_endpoints_reject_mixed_selectors(client, catalog, suffix):
    http, headers, _ = client
    extra = {
        "knowledge-binding-status": {"subjects": [{"type": "USER", "user_id": "member"}]},
        "knowledge-subject-options": {"category": "users"},
        "knowledge-all-users-status": {},
        "knowledge-all-users": {"operation": "BIND"},
    }[suffix]
    response = await http.post(
        f"/api/admin/agents/{catalog[0]}/{suffix}", headers=headers,
        json={"targets": [{"space_id": "space", "kb_id": "kb-0"}],
              "knowledge_resource_ids": catalog[2][:1], **extra},
    )
    assert response.status_code == 422


async def test_management_mode_pair_alias(client, catalog):
    http, headers, member = client
    path = "/api/polarrag/spaces/space/knowledge-bases/kb-0/management-mode"
    response = await http.put(path, headers=member, json={"management_mode": "EXTERNAL_SYNC"})
    assert response.status_code == 403
    response = await http.put(path, headers=headers, json={"management_mode": "EXTERNAL_SYNC"})
    assert response.status_code == 200, response.text
    assert response.json() == {"knowledge_resource_id": catalog[2][0], "management_mode": "EXTERNAL_SYNC"}


async def test_pair_lookup_rejects_ambiguity_and_unbound_instance(setup, catalog):
    from fastapi import HTTPException
    from server.models import KnowledgeResource, KnowledgeResourceSyncStatus, PolarRAGInstance, PolarRAGInstanceStatus, PolarRAGSpace
    from server.polarrag.resource_selector import resolve_knowledge_pair
    factory, admin, _ = setup
    async with factory() as session:
        instance = PolarRAGInstance(name="second", scheme="http", host="second.test", port=9200,
            username_ciphertext="u", password_ciphertext="p", status=PolarRAGInstanceStatus.ACTIVE, created_by=admin.id)
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(polarrag_instance_id=instance.id, space_id="space", name="second", identity_domain="second")
        session.add(space)
        await session.flush()
        session.add(KnowledgeResource(knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
            space_id="space", kb_id="kb-0", name="second", kb_type="PUBLIC", identity_domain="second",
            sync_status=KnowledgeResourceSyncStatus.ACTIVE))
        await session.commit()
        with pytest.raises(HTTPException) as error:
            await resolve_knowledge_pair(session, "space", "kb-0")
        assert error.value.status_code == 409
        assert await resolve_knowledge_pair(session, "space", "kb-0", instance_ids=[catalog[4]]) == catalog[2][0]
        with pytest.raises(HTTPException) as error:
            await resolve_knowledge_pair(session, "space", "kb-0", instance_ids=[])
        assert error.value.status_code == 404


@pytest.mark.parametrize("model_name", ["DocumentResourceRequest", "FindDocumentsRequest", "ListDocumentsRequest", "RechunkDocumentRequest"])
@pytest.mark.parametrize("selector", [{}, {"kb_id": "kb"}, {"space_id": "space"},
    {"knowledge_resource_id": "id", "space_id": "space", "kb_id": "kb"}])
def test_document_selector_shape(model_name, selector):
    from pydantic import ValidationError
    from server.api import polarrag_documents
    model = getattr(polarrag_documents, model_name)
    body = {"agent_id": "agent", **selector}
    if model_name == "FindDocumentsRequest":
        body["filename"] = "guide"
    with pytest.raises(ValidationError):
        model.model_validate(body)
