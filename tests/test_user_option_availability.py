from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from server.models import (
    Agent, AgentKnowledgeScopeMode, AgentPolarRAGInstanceBinding, AgentUserAssignment,
    AuthProvider, EnterpriseDirectoryUser, EnterpriseIdentitySource,
    EnterpriseIdentitySourceSpaceBinding, ExternalUserPrincipalMembership, PolarRAGSpace,
    User, UserExternalIdentity, UserStatus,
)
from tests.test_knowledge_visibility_bind import catalog as catalog

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.fixture
async def candidates(setup, catalog):
    factory, admin, _ = setup
    agent_id, binding_id, _, space_id, _ = catalog
    async with factory() as session:
        agent = await session.get(Agent, agent_id)
        agent.knowledge_scope_mode = AgentKnowledgeScopeMode.SCOPED
        binding = await session.get(AgentPolarRAGInstanceBinding, binding_id)
        binding.public_knowledge_resource_ids_json = None
        source = await session.scalar(select(EnterpriseIdentitySource))
        source.status = "active"
        other = EnterpriseIdentitySource.create(name="Other", provider="feishu", tenant_id="other")
        other.status = "active"
        session.add(other)
        await session.flush()
        names = ["disabled", "pas-disabled", "normal", "local", "multi", "other-only"]
        users = {name: User(external_id=f"picker-{name}", display_name="Same name",
                            auth_provider=AuthProvider.BUILTIN if name == "local" else AuthProvider.OIDC,
                            status=UserStatus.DISABLED if name == "pas-disabled" else UserStatus.ACTIVE)
                 for name in names}
        session.add_all(users.values())
        await session.flush()
        for name, user in users.items():
            if name == "local":
                continue
            source_row = other if name == "other-only" else source
            session.add(UserExternalIdentity(user_id=user.id, identity_provider=f"feishu:{source_row.tenant_id}",
                                             external_subject=name))
            session.add(EnterpriseDirectoryUser(identity_source_id=source_row.id, external_user_id=name,
                                                display_name="Same name", status="disabled" if name in ("disabled", "multi") else "active"))
        session.add_all([
            UserExternalIdentity(user_id=users["multi"].id, identity_provider="feishu:visibility", external_subject="second"),
            EnterpriseDirectoryUser(identity_source_id=source.id, external_user_id="second", display_name="Same name", status="active"),
            UserExternalIdentity(user_id=users["disabled"].id, identity_provider="feishu:other", external_subject="disabled-second"),
            EnterpriseDirectoryUser(identity_source_id=other.id, external_user_id="disabled-second", display_name="Same name", status="active"),
        ])
        await session.commit()
        return {name: user.id for name, user in users.items()}, source.id, other.id


@pytest.mark.parametrize("source_state", ["active", "stale", "expired"])
async def test_candidate_filter_precedes_search_and_pagination(client, setup, catalog, candidates, source_state):
    http, headers, _ = client
    users, source_id, _ = candidates
    agent_id, _, resource_ids, _, _ = catalog
    async with setup[0]() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        source.status = "active" if source_state == "active" else "stale"
        source.last_synced_at = datetime.now(UTC) - timedelta(days=365 if source_state == "expired" else 0)
        await session.commit()
    expected = {users["local"]} if source_state == "expired" else {users[n] for n in ("local", "normal", "multi")}
    for mode in ("unbound", "all"):
        seen = set()
        for offset in range(len(expected)):
            response = await http.get(f"/api/agents/{agent_id}/user-options", headers=headers,
                                      params={"search": "Same name", "binding_status": mode, "offset": offset, "limit": 1})
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["total"] == len(expected)
            assert len(page["items"]) == 1
            seen.add(page["items"][0]["id"])
        assert seen == expected
    seen, offset = set(), 0
    while True:
        response = await http.post(f"/api/admin/agents/{agent_id}/knowledge-subject-options", headers=headers,
                                   json={"category": "users", "knowledge_resource_ids": resource_ids,
                                         "search": "Same name", "limit": 1, "offset": offset})
        assert response.status_code == 200, response.text
        page = response.json()
        assert len(page["items"]) == 1
        seen.add(page["items"][0]["subject"]["user_id"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert seen == expected


@pytest.mark.parametrize("name", ["disabled", "pas-disabled"])
async def test_disabled_bindings_remain_visible_and_removable(client, setup, catalog, candidates, name):
    http, headers, _ = client
    users, _, _ = candidates
    agent_id, _, resource_ids, _, _ = catalog
    user_id = users[name]
    async with setup[0]() as session:
        assignment = AgentUserAssignment(agent_id=agent_id, user_id=user_id, is_direct=True)
        session.add(assignment)
        await session.commit()
        assignment_id = assignment.id
    operation = {"operation": "BIND", "subjects": [{"type": "USER", "user_id": user_id}],
                 "targets": [{"knowledge_resource_id": rid} for rid in resource_ids]}
    batch = f"/api/admin/agents/{agent_id}/knowledge-bindings:batch"
    response = await http.post(batch, headers=headers, json={"operations": [operation]})
    assert response.status_code == 200, response.text
    for mode in ("bound", "all"):
        page = (await http.get(f"/api/agents/{agent_id}/user-options", headers=headers,
                              params={"binding_status": mode, "search": f"picker-{name}"})).json()
        assert page["total"] == 1
        assert page["items"][0]["availability"] == "disabled"
        assert page["items"][0]["assignment_id"] == assignment_id
    kb_path = f"/api/admin/agents/{agent_id}/knowledge-subject-options"
    body = {"category": "users", "knowledge_resource_ids": resource_ids, "search": f"picker-{name}"}
    page = (await http.post(kb_path, headers=headers, json={**body, "binding_status": "bound"})).json()
    assert len(page["items"]) == 1
    assert page["items"][0]["availability"] == "disabled"
    assert (await http.post(kb_path, headers=headers, json=body)).json()["items"] == []
    response = await http.post(batch, headers=headers, json={"operations": [{**operation, "operation": "UNBIND"}]})
    assert response.status_code == 200, response.text
    assert (await http.post(kb_path, headers=headers, json={**body, "binding_status": "bound"})).json()["items"] == []
    assert (await http.delete(f"/api/agents/{agent_id}/user-assignments/{assignment_id}", headers=headers)).status_code == 204
    page = (await http.get(f"/api/agents/{agent_id}/user-options", headers=headers,
                          params={"binding_status": "all", "search": f"picker-{name}"})).json()
    assert page["items"] == []
    async with setup[0]() as session:
        user = await session.get(User, user_id)
        assert user.status == (UserStatus.DISABLED if name == "pas-disabled" else UserStatus.ACTIVE)


async def test_another_in_scope_identity_keeps_user_available(client, setup, catalog, candidates):
    http, headers, _ = client
    users, _, other_id = candidates
    agent_id, _, _, space_id, _ = catalog
    async with setup[0]() as session:
        session.add(EnterpriseIdentitySourceSpaceBinding(identity_source_id=other_id, knowledge_space_id=space_id))
        await session.commit()
    page = (await http.get(f"/api/agents/{agent_id}/user-options", headers=headers,
                          params={"search": "picker-disabled"})).json()
    assert [row["id"] for row in page["items"]] == [users["disabled"]]


async def test_source_specific_picker_keeps_disabled_history(client, setup, catalog, candidates):
    http, headers, _ = client
    users, source_id, _ = candidates
    agent_id = catalog[0]
    path = f"/api/agents/{agent_id}/enterprise-user-options"
    page = (await http.get(path, headers=headers, params={"identity_source_ids": source_id})).json()
    assert {row["id"] for row in page["items"]} == {users["normal"], users["multi"]}
    async with setup[0]() as session:
        session.add(AgentUserAssignment(agent_id=agent_id, user_id=users["disabled"], is_direct=True))
        source = await session.get(EnterpriseIdentitySource, source_id)
        source.status = "stale"
        source.last_synced_at = datetime.now(UTC) - timedelta(days=365)
        await session.commit()
    page = (await http.get(path, headers=headers, params={"identity_source_ids": source_id, "binding_status": "bound"})).json()
    assert page["total"] == 1
    assert page["items"][0]["id"] == users["disabled"]
    assert page["items"][0]["availability"] == "disabled"


async def test_local_principals_do_not_override_disabled_directory_users(client, setup, catalog, candidates):
    http, headers, _ = client
    users, source_id, _ = candidates
    async with setup[0]() as session:
        # The first identity has no directory entry; the second is explicitly disabled.
        session.add(UserExternalIdentity(user_id=users["normal"], identity_provider="feishu:visibility",
                                        external_subject="local-principal"))
        directory = await session.scalar(select(EnterpriseDirectoryUser).where(
            EnterpriseDirectoryUser.identity_source_id == source_id,
            EnterpriseDirectoryUser.external_user_id == "normal",
        ))
        await session.delete(directory)
        for external_id in ("local-principal", "disabled"):
            session.add(ExternalUserPrincipalMembership(identity_source_id=source_id,
                        external_user_id=external_id, principal_type="group", principal_id="local-group",
                        expires_at=datetime.now(UTC) + timedelta(days=1)))
        await session.commit()
    response = await http.get(f"/api/agents/{catalog[0]}/user-options", headers=headers,
                              params={"search": "Same name"})
    assert {row["id"] for row in response.json()["items"]} == {users[n] for n in ("local", "normal", "multi")}


async def test_expired_source_history_is_unavailable_not_disabled(client, setup, catalog, candidates):
    http, headers, _ = client
    users, source_id, _ = candidates
    async with setup[0]() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        source.status = "stale"
        source.last_synced_at = datetime.now(UTC) - timedelta(days=365)
        session.add(AgentUserAssignment(agent_id=catalog[0], user_id=users["normal"], is_direct=True))
        await session.commit()
    response = await http.get(f"/api/agents/{catalog[0]}/user-options", headers=headers,
                              params={"search": "picker-normal", "binding_status": "bound"})
    assert response.json()["items"][0]["availability"] == "unavailable"


async def test_kb_candidates_use_selected_spaces_not_other_agent_spaces(client, setup, catalog, candidates):
    http, headers, _ = client
    users, _, other_id = candidates
    agent_id, _, resource_ids, _, instance_id = catalog
    async with setup[0]() as session:
        space = PolarRAGSpace(polarrag_instance_id=instance_id, space_id="other-space", name="Other",
                              identity_domain="other", enabled=True)
        session.add(space)
        await session.flush()
        session.add(EnterpriseIdentitySourceSpaceBinding(identity_source_id=other_id,
                                                         knowledge_space_id=space.knowledge_space_id))
        await session.commit()
    response = await http.get(f"/api/agents/{agent_id}/user-options", headers=headers,
                              params={"search": "picker-disabled"})
    assert [row["id"] for row in response.json()["items"]] == [users["disabled"]]
    response = await http.post(f"/api/admin/agents/{agent_id}/knowledge-subject-options", headers=headers,
                               json={"category": "users", "knowledge_resource_ids": resource_ids,
                                     "search": "picker-disabled"})
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []
