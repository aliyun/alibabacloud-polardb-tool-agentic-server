from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from server.models import (
    Agent, AgentPolarRAGInstanceBinding, EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding, IdentitySourceProvider,
    KnowledgeBindingMode, KnowledgeResource, KnowledgeResourceManagementMode,
    KnowledgeResourceSyncStatus, PolarRAGInstance, PolarRAGInstanceStatus, PolarRAGSpace,
)

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.fixture
async def catalog(setup):
    factory, admin, _ = setup
    async with factory() as session:
        agent = Agent(name="visibility-agent", created_by=admin.id)
        instance = PolarRAGInstance(
            name="visibility-instance", scheme="http", host="rag.example.test", port=9200,
            username_ciphertext="username", password_ciphertext="password",
            status=PolarRAGInstanceStatus.ACTIVE, created_by=admin.id,
        )
        source = EnterpriseIdentitySource.create(
            name="visibility-source", provider=IdentitySourceProvider.FEISHU, tenant_id="visibility",
        )
        session.add_all([agent, instance, source])
        await session.flush()
        space = PolarRAGSpace(
            polarrag_instance_id=instance.id, space_id="space", name="Space", identity_domain="domain", enabled=True,
        )
        session.add(space)
        await session.flush()
        binding = AgentPolarRAGInstanceBinding(
            agent_id=agent.id, polarrag_instance_id=instance.id,
            public_knowledge_resource_ids_json="[]", created_by_user_id=admin.id,
        )
        resources = [KnowledgeResource(
            knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
            space_id=space.space_id, kb_id=f"kb-{i}", name=f"KB {i}", kb_type="PUBLIC",
            identity_domain="domain", binding_mode=KnowledgeBindingMode.DOMAIN,
            sync_status=KnowledgeResourceSyncStatus.ACTIVE,
            management_mode=KnowledgeResourceManagementMode.NATIVE,
        ) for i in range(2)]
        session.add_all([binding, *resources, EnterpriseIdentitySourceSpaceBinding(
            identity_source_id=source.id, knowledge_space_id=space.knowledge_space_id,
        )])
        await session.commit()
        return agent.id, binding.id, [r.id for r in resources], space.knowledge_space_id, instance.id


@pytest.mark.parametrize("all_public", [False, True])
async def test_bind_both_selectors_idempotently(client, setup, catalog, all_public):
    http, headers, _ = client
    factory, _, _ = setup
    agent, binding_id, ids, _, _ = catalog
    if all_public:
        async with factory() as session:
            binding = await session.get(AgentPolarRAGInstanceBinding, binding_id)
            binding.public_knowledge_resource_ids_json = None
            await session.commit()
    path = f"/api/admin/agents/{agent}/knowledge-visibility:bind"
    for body in ({"targets": [{"space_id": "space", "kb_id": "kb-0"}]},
                 {"knowledge_resource_ids": ids}, {"knowledge_resource_ids": ids}):
        response = await http.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["agent_id"] == agent
    async with factory() as session:
        binding = await session.get(AgentPolarRAGInstanceBinding, binding_id)
        assert binding.public_knowledge_resource_ids_json is None if all_public else set(json.loads(binding.public_knowledge_resource_ids_json)) == set(ids)


@pytest.mark.parametrize("invalid", ["missing", "disabled", "space", "instance", "unbound"])
async def test_invalid_batch_is_atomic(client, setup, catalog, invalid):
    http, headers, _ = client
    factory, _, _ = setup
    agent, binding_id, ids, space_id, instance_id = catalog
    async with factory() as session:
        resource = await session.get(KnowledgeResource, ids[1])
        if invalid == "disabled":
            resource.enabled = False
        elif invalid == "external":
            resource.management_mode = KnowledgeResourceManagementMode.EXTERNAL_SYNC
        elif invalid == "space":
            space = await session.get(PolarRAGSpace, space_id)
            space.enabled = False
        elif invalid == "instance":
            instance = await session.get(PolarRAGInstance, instance_id)
            instance.status = PolarRAGInstanceStatus.DISABLED
        elif invalid == "unbound":
            await session.delete(await session.get(AgentPolarRAGInstanceBinding, binding_id))
        await session.commit()
    requested = [ids[0], "missing" if invalid == "missing" else ids[1]]
    response = await http.post(f"/api/admin/agents/{agent}/knowledge-visibility:bind", headers=headers,
                               json={"knowledge_resource_ids": requested})
    assert response.status_code == 422, response.text
    async with factory() as session:
        binding = await session.get(AgentPolarRAGInstanceBinding, binding_id)
        assert binding is None or binding.public_knowledge_resource_ids_json == "[]"


@pytest.mark.parametrize("body", [{}, {"targets": []}, {"knowledge_resource_ids": []},
    {"targets": [{"space_id": "space"}]}, {"knowledge_resource_ids": [""]},
    {"knowledge_resource_ids": ["a"] * 101},
    {"targets": [{"space_id": "space", "kb_id": "kb-0"}], "knowledge_resource_ids": ["a"]}])
async def test_invalid_request_shape(client, catalog, body):
    http, headers, _ = client
    response = await http.post(f"/api/admin/agents/{catalog[0]}/knowledge-visibility:bind", headers=headers, json=body)
    assert response.status_code == 422


async def test_member_cannot_bind(client, catalog):
    http, _, headers = client
    response = await http.post(f"/api/admin/agents/{catalog[0]}/knowledge-visibility:bind", headers=headers,
                               json={"knowledge_resource_ids": catalog[2]})
    assert response.status_code == 403


async def test_audit_failure_rolls_back(client, setup, catalog, monkeypatch):
    from server.api import agent_knowledge_scopes
    async def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    monkeypatch.setattr(agent_knowledge_scopes, "_audit_agent", fail)
    http, headers, _ = client
    response = await http.post(f"/api/admin/agents/{catalog[0]}/knowledge-visibility:bind", headers=headers,
                               json={"knowledge_resource_ids": catalog[2]})
    assert response.status_code == 503
    async with setup[0]() as session:
        assert await session.scalar(select(AgentPolarRAGInstanceBinding.public_knowledge_resource_ids_json)
                                    .where(AgentPolarRAGInstanceBinding.id == catalog[1])) == "[]"


async def test_ambiguous_pair_requires_resource_id(client, setup, catalog):
    http, headers, _ = client
    factory, admin, _ = setup
    agent, _, ids, _, _ = catalog
    async with factory() as session:
        instance = PolarRAGInstance(
            name="duplicate-instance", scheme="http", host="second.example.test", port=9200,
            username_ciphertext="username", password_ciphertext="password",
            status=PolarRAGInstanceStatus.ACTIVE, created_by=admin.id,
        )
        session.add(instance)
        await session.flush()
        space = PolarRAGSpace(polarrag_instance_id=instance.id, space_id="space", name="Other", identity_domain="other")
        session.add(space)
        await session.flush()
        session.add_all([
            AgentPolarRAGInstanceBinding(agent_id=agent, polarrag_instance_id=instance.id),
            KnowledgeResource(knowledge_space_id=space.knowledge_space_id, polarrag_instance_id=instance.id,
                              space_id="space", kb_id="kb-0", name="Duplicate", kb_type="PUBLIC", identity_domain="other",
                              sync_status=KnowledgeResourceSyncStatus.ACTIVE),
        ])
        await session.commit()
    path = f"/api/admin/agents/{agent}/knowledge-visibility:bind"
    response = await http.post(path, headers=headers, json={"targets": [{"space_id": "space", "kb_id": "kb-0"}]})
    assert response.status_code == 409
    response = await http.post(path, headers=headers, json={"knowledge_resource_ids": [ids[0]]})
    assert response.status_code == 200
    assert response.json()["agent_id"] == agent
    assert response.json()["knowledge_resource_ids"] == [ids[0]]
    assert response.json()["statuses"]["PUBLIC"] in {"BOUND", "PARTIAL"}


async def test_personal_kb_binding_remains_supported(client, setup, catalog):
    http, headers, _ = client
    factory, _, _ = setup
    agent, binding_id, ids, _, _ = catalog
    async with factory() as session:
        resource = await session.get(KnowledgeResource, ids[0])
        binding = await session.get(AgentPolarRAGInstanceBinding, binding_id)
        resource.kb_type = "PERSONAL"
        binding.personal_knowledge_resource_ids_json = "[]"
        await session.commit()
    response = await http.post(
        f"/api/admin/agents/{agent}/knowledge-visibility:bind",
        headers=headers,
        json={"targets": [{"space_id": "space", "kb_id": "kb-0"}]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["knowledge_resource_ids"] == [ids[0]]
    async with factory() as session:
        binding = await session.get(AgentPolarRAGInstanceBinding, binding_id)
        assert json.loads(binding.personal_knowledge_resource_ids_json) == [ids[0]]


async def test_missing_identity_space_binding_rejected(client, setup, catalog):
    from sqlalchemy import delete
    http, headers, _ = client
    async with setup[0]() as session:
        await session.execute(delete(EnterpriseIdentitySourceSpaceBinding))
        await session.commit()
    response = await http.post(f"/api/admin/agents/{catalog[0]}/knowledge-visibility:bind", headers=headers,
                               json={"knowledge_resource_ids": catalog[2]})
    assert response.status_code == 422


async def test_binding_does_not_grant_scoped_user_access(client, setup, catalog):
    from server.core.agent_knowledge_scope import resolve_agent_knowledge_resource_scope
    from server.models import AgentKnowledgeScopeMode, AuditLog
    http, headers, _ = client
    factory, _, member = setup
    async with factory() as session:
        agent = await session.get(Agent, catalog[0])
        agent.knowledge_scope_mode = AgentKnowledgeScopeMode.SCOPED
        await session.commit()
    response = await http.post(f"/api/admin/agents/{catalog[0]}/knowledge-visibility:bind", headers=headers,
                               json={"knowledge_resource_ids": catalog[2]})
    assert response.status_code == 200
    async with factory() as session:
        scope = await resolve_agent_knowledge_resource_scope(session, catalog[0], member.id)
        resource = await session.get(KnowledgeResource, catalog[2][0])
        assert not scope.allows(resource)
        audit = await session.scalar(select(AuditLog).where(AuditLog.action == "agent_knowledge_binding.visibility"))
        assert audit is not None
        assert audit.target_id == catalog[0]


async def test_binding_ignores_management_mode(client, setup, catalog):
    http, headers, _ = client
    factory, _, _ = setup
    agent, _, ids, _, _ = catalog
    async with factory() as session:
        resource = await session.get(KnowledgeResource, ids[1])
        resource.management_mode = KnowledgeResourceManagementMode.EXTERNAL_SYNC
        await session.commit()
    result = await http.post(f"/api/admin/agents/{agent}/knowledge-visibility:bind",
                            headers=headers, json={"knowledge_resource_ids": ids[:2]})
    assert result.status_code == 200, result.text
    for operation in ("BIND", "UNBIND"):
        result = await http.post(f"/api/admin/agents/{agent}/knowledge-all-users", headers=headers,
                                json={"knowledge_resource_ids": ids[:2], "operation": operation})
        assert result.status_code == 200, result.text
    result = await http.post(f"/api/admin/agents/{agent}/knowledge-visibility:unbind",
                            headers=headers, json={"knowledge_resource_ids": ids[:2]})
    assert result.status_code == 200, result.text
