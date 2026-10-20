import json

import pytest

from server.core.agent_knowledge_scope import resolve_agent_global_resource_scope, resolve_agent_knowledge_resource_scope
from server.models import (
    Agent, AgentKnowledgeScopeMode, AgentPolarRAGInstanceBinding, EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding, IdentitySourceProvider, KnowledgeBindingMode,
    KnowledgeResource, KnowledgeResourceManagementMode, KnowledgeResourceSyncStatus,
    PolarRAGInstance, PolarRAGInstanceStatus, PolarRAGSpace,
)

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.mark.parametrize("kb_type", ["PUBLIC", "PERSONAL"])
async def test_all_visibility_exclusions_preserve_future_synced_resources(client, setup, kb_type):
    http, headers, _ = client
    factory, admin, _ = setup
    async with factory() as session:
        agent = Agent(name="Visibility", created_by=admin.id, knowledge_scope_mode=AgentKnowledgeScopeMode.LEGACY_ALL)
        source = EnterpriseIdentitySource.create(
            name="Visibility", provider=IdentitySourceProvider.FEISHU, tenant_id="visibility",
        )
        instance = PolarRAGInstance(
            name="Visibility", scheme="http", host="rag.example.test", port=9200,
            username_ciphertext="username", password_ciphertext="password",
            status=PolarRAGInstanceStatus.ACTIVE, created_by=admin.id,
        )
        session.add_all([agent, source, instance])
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id, space_id="visibility", name="Visibility",
            identity_domain="visibility", enabled=True,
        )
        session.add(space)
        await session.flush()
        binding = AgentPolarRAGInstanceBinding(
            agent_id=agent.id, polarrag_instance_id=instance.id, created_by_user_id=admin.id,
        )
        session.add_all([binding, EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=source.id, knowledge_space_id=space.knowledge_space_id,
        )])

        def resource(name, mode):
            return KnowledgeResource(
                knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
                space_id=space.space_id, kb_id=name, name=name, kb_type=kb_type,
                identity_domain=space.identity_domain,
                binding_mode=KnowledgeBindingMode.OWNER if kb_type == "PERSONAL" else KnowledgeBindingMode.DOMAIN,
                sync_status=KnowledgeResourceSyncStatus.ACTIVE, management_mode=mode,
            )

        first = resource("excluded", KnowledgeResourceManagementMode.NATIVE)
        session.add(first)
        await session.commit()
        agent_id, first_id, binding_id = agent.id, first.id, binding.id

    path = f"/api/admin/agents/{agent_id}/knowledge-visibility"
    for _ in range(2):
        response = await http.post(path + ":unbind", headers=headers, json={"knowledge_resource_ids": [first_id]})
        assert response.status_code == 200
        assert response.json()["statuses"][kb_type] == "PARTIAL"
    if kb_type == "PUBLIC":
        response = await http.get(f"/api/agents/{agent_id}/polarrag-bindings", headers=headers)
        assert response.status_code == 200
        assert response.json()["items"][0]["public_knowledge_resource_ids"] is None
        assert response.json()["items"][0]["public_excluded_resource_ids"] == [first_id]
    other_type = "PERSONAL" if kb_type == "PUBLIC" else "PUBLIC"
    async with factory() as session:
        changed = await session.get(KnowledgeResource, first_id)
        changed.kb_type = other_type
        await session.commit()
        scope = await resolve_agent_global_resource_scope(session, agent_id)
        assert scope.allows(changed)
    rebound = await http.post(path + ":bind", headers=headers, json={"knowledge_resource_ids": [first_id]})
    assert rebound.status_code == 200
    async with factory() as session:
        changed = await session.get(KnowledgeResource, first_id)
        changed.kb_type = kb_type
        await session.commit()
        scope = await resolve_agent_global_resource_scope(session, agent_id)
        assert scope.allows(changed)
    await http.post(path + ":unbind", headers=headers, json={"knowledge_resource_ids": [first_id]})
    async with factory() as session:
        future = resource("future-sync", KnowledgeResourceManagementMode.EXTERNAL_SYNC)
        future_native = resource("future-native", KnowledgeResourceManagementMode.NATIVE)
        session.add_all([future, future_native])
        await session.commit()
        future_id, native_id = future.id, future_native.id
        for scope in (
            await resolve_agent_global_resource_scope(session, agent_id),
            await resolve_agent_knowledge_resource_scope(session, agent_id, admin.id),
        ):
            assert not scope.allows(first)
            assert scope.allows(future)
            assert scope.allows(future_native)

    options = f"/api/admin/agents/{agent_id}/knowledge-resource-options"
    for state, ids in (("bound", {future_id, native_id}), ("unbound", {first_id})):
        response = await http.get(options, headers=headers, params={"binding_status": state})
        assert response.status_code == 200
        assert {item["knowledge_resource_id"] for item in response.json()["items"]} == ids
        assert response.json()["total"] == len(ids)
    response = await http.post(path + ":bind", headers=headers, json={"knowledge_resource_ids": [future_id]})
    assert response.status_code == 200
    assert response.json()["statuses"][kb_type] == "PARTIAL"
    # Manual visibility is independent of external management. Repeated
    # exclusion/rebinding must preserve the all-resources policy and metadata.
    for operation in ("unbind", "unbind", "bind", "bind"):
        response = await http.post(path + ":" + operation, headers=headers, json={"knowledge_resource_ids": [future_id]})
        assert response.status_code == 200
        assert response.json()["statuses"][kb_type] == "PARTIAL"
        async with factory() as session:
            synced = await session.get(KnowledgeResource, future_id)
            binding = await session.get(AgentPolarRAGInstanceBinding, binding_id)
            assert synced.management_mode == KnowledgeResourceManagementMode.EXTERNAL_SYNC
            assert getattr(binding, f"{kb_type.lower()}_knowledge_resource_ids_json") is None
            excluded = json.loads(getattr(binding, f"{kb_type.lower()}_excluded_resource_ids_json"))
            assert set(excluded) == ({first_id, future_id} if operation == "unbind" else {first_id})
            scope = await resolve_agent_global_resource_scope(session, agent_id)
            assert scope.allows(synced) == (operation == "bind")
            assert scope.allows(future_native)
    for _ in range(2):
        response = await http.post(path + ":bind", headers=headers, json={"knowledge_resource_ids": [first_id]})
        assert response.status_code == 200
        assert response.json()["statuses"][kb_type] == "BOUND"

    await http.post(path + ":unbind", headers=headers, json={"knowledge_resource_ids": [first_id]})
    response = await http.post(path, headers=headers, json={"kb_type": kb_type, "operation": "UNBIND"})
    assert response.json()["statuses"][kb_type] == "UNBOUND"
    response = await http.post(path + ":bind", headers=headers, json={"knowledge_resource_ids": [first_id]})
    assert response.json()["statuses"][kb_type] == "PARTIAL"
    async with factory() as session:
        binding = await session.get(AgentPolarRAGInstanceBinding, binding_id)
        raw = getattr(binding, f"{kb_type.lower()}_knowledge_resource_ids_json")
        assert json.loads(raw) == [first_id]
        scope = await resolve_agent_global_resource_scope(session, agent_id)
        assert scope.allows(first)
        assert not scope.allows(future)
    response = await http.post(path, headers=headers, json={"kb_type": kb_type, "operation": "BIND"})
    assert response.json()["statuses"][kb_type] == "BOUND"
    response = await http.get(options, headers=headers, params={"binding_status": "bound"})
    assert response.json()["total"] == 3
    if kb_type == "PUBLIC":
        await http.post(path + ":unbind", headers=headers, json={"knowledge_resource_ids": [first_id]})
        response = await http.put(
            f"/api/agents/{agent_id}/polarrag-bindings/{binding_id}/public-resources",
            headers=headers, json={"public_knowledge_resource_ids": None},
        )
        assert response.status_code == 200
        assert response.json()["public_excluded_resource_ids"] == []
        response = await http.get(options, headers=headers, params={"binding_status": "bound", "limit": 1, "offset": 1})
        assert response.json()["total"] == 3
        assert len(response.json()["items"]) == 1
