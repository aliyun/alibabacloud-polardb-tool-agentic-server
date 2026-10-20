import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event, select

from server.core.knowledge_subjects import (
    batch_group_knowledge_subjects, batch_user_knowledge_subjects, knowledge_subject_members, user_knowledge_subjects,
)
from server.models import (
    Agent, AgentGroupAssignment, AgentKnowledgePolicy, AgentKnowledgeScope, AgentKnowledgeScopeBinding,
    AgentKnowledgeScopeMode, AgentPolarRAGInstanceBinding, AgentUserAssignment, AuthProvider, Department,
    EnterpriseDirectoryGroup, EnterpriseDirectoryMembership, EnterpriseDirectoryUser, EnterpriseIdentitySource,
    EnterprisePrincipalAssignment, ExternalUserPrincipalMembership, User, UserDepartment, UserExternalIdentity,
)
from tests.test_knowledge_visibility_bind import catalog as catalog

pytest_plugins = ("tests._admin_api_fixtures",)


@pytest.fixture
async def option_catalog(setup, catalog):
    factory, admin, _ = setup
    agent_id, binding_id, resource_ids, _, _ = catalog
    async with factory() as session:
        agent = await session.get(Agent, agent_id)
        agent.knowledge_scope_mode = AgentKnowledgeScopeMode.SCOPED
        binding = await session.get(AgentPolarRAGInstanceBinding, binding_id)
        binding.public_knowledge_resource_ids_json = None
        source = await session.scalar(select(EnterpriseIdentitySource))
        source.status = "stale"
        source.last_synced_at = datetime.now(UTC)
        department = Department(name="Department")
        session.add(department)
        names = ["direct", "department", "child", "source", "excluded", "external", "local", "expired", "multi",
                 "enterprise", "enterprise-expired"]
        users = {name: User(external_id=name, display_name=name, auth_provider=AuthProvider.BUILTIN)
                 for name in names}
        session.add_all(users.values())
        await session.flush()
        assignments = [
            AgentGroupAssignment.for_department(agent_id=agent_id, department_id=department.id,
                                                 created_by_user_id=admin.id),
            AgentGroupAssignment.for_identity_source_group(agent_id=agent_id, identity_source_id=source.id,
                                                           external_group_id="parent", created_by_user_id=admin.id),
            AgentGroupAssignment.for_identity_source_all_users(agent_id=agent_id, identity_source_id=source.id,
                                                               created_by_user_id=admin.id),
            AgentGroupAssignment.for_enterprise_group(agent_id=agent_id, identity_domain="domain", provider="sharepoint",
                                                      principal_id="enterprise-group", created_by_user_id=admin.id),
        ]
        session.add_all(assignments)
        session.add(AgentUserAssignment(agent_id=agent_id, user_id=users["direct"].id, is_direct=True))
        session.add(UserDepartment(user_id=users["department"].id, department_id=department.id))
        for name in names[2:9]:
            session.add(UserExternalIdentity(user_id=users[name].id, identity_provider="feishu:visibility",
                                             external_subject=name))
            if name not in ("local", "expired"):
                session.add(EnterpriseDirectoryUser(identity_source_id=source.id, external_user_id=name,
                                                    display_name=name, status="active"))
        session.add(UserExternalIdentity(user_id=users["multi"].id, identity_provider="feishu:visibility",
                                         external_subject="multi-second"))
        session.add(EnterpriseDirectoryUser(identity_source_id=source.id, external_user_id="multi-second",
                                            display_name="second", status="active"))
        for name in ("parent", "child"):
            session.add(EnterpriseDirectoryGroup(identity_source_id=source.id, external_group_id=name,
                                                 display_name=name, principal_type="group", status="active"))
        session.add(EnterpriseDirectoryMembership(identity_source_id=source.id, external_group_id="parent",
                                                  member_type="group", external_member_id="child"))
        for name in ("child", "excluded", "external", "multi"):
            session.add(EnterpriseDirectoryMembership(identity_source_id=source.id, external_group_id="child",
                                                      member_type="user", external_member_id=name))
        for name in ("local", "expired"):
            session.add(ExternalUserPrincipalMembership(
                identity_source_id=source.id, external_user_id=name, principal_type="group", principal_id="parent",
                expires_at=datetime.now(UTC) + timedelta(days=1 if name == "local" else -1),
            ))
        for name in ("enterprise", "enterprise-expired"):
            session.add(EnterprisePrincipalAssignment(
                pas_user_id=users[name].id, identity_domain="domain", provider="sharepoint",
                principal_type="group", principal_id="enterprise-group", source="admin_managed", status="active",
                valid_until=datetime.now(UTC) + timedelta(days=1 if name == "enterprise" else -1),
            ))
        await session.flush()
        scopes = [
            ("manual", "MANUAL", assignments[0].id, None),
            ("group", "MANUAL", assignments[1].id, None),
            ("external", "EXTERNAL_SYNC", None, users["external"].id),
            ("direct", "MANUAL", None, users["direct"].id),
            ("enterprise", "EXTERNAL_SYNC", assignments[3].id, None),
        ]
        for name, origin, group_id, user_id in scopes:
            scope = AgentKnowledgeScope(agent_id=agent_id, name=name, origin=origin,
                                        knowledge_resource_ids_json=json.dumps(resource_ids),
                                        created_by_user_id=admin.id)
            session.add(scope)
            await session.flush()
            session.add(AgentKnowledgeScopeBinding(scope_id=scope.id, group_assignment_id=group_id, user_id=user_id))
        session.add_all([
            AgentKnowledgePolicy(agent_id=agent_id, knowledge_resource_id=resource_ids[0], select_all=True,
                                 excluded_subjects_json=json.dumps([["GROUP", source.id, "parent"]])),
            AgentKnowledgePolicy(agent_id=agent_id, knowledge_resource_id=resource_ids[1], select_all=False,
                                 excluded_subjects_json=json.dumps([["USER", users["excluded"].id]])),
        ])
        await session.commit()
        return {name: user.id for name, user in users.items()}, source.id


@pytest.mark.parametrize("filter_name", ["available", "bound"])
@pytest.mark.parametrize("source_state", ["stale", "active", "expired", "inactive-parent"])
async def test_paginated_users_match_existing_status_resolver(
    client, setup, catalog, option_catalog, filter_name, source_state,
):
    http, headers, _ = client
    users, _ = option_catalog
    agent_id, _, resources, _, _ = catalog
    async with setup[0]() as session:
        source = await session.get(EnterpriseIdentitySource, option_catalog[1])
        if source_state == "expired":
            source.last_synced_at = datetime.now(UTC) - timedelta(days=365)
        elif source_state == "active":
            source.status = "active"
        elif source_state == "inactive-parent":
            group = await session.scalar(select(EnterpriseDirectoryGroup).where(
                EnterpriseDirectoryGroup.external_group_id == "parent",
            ))
            group.status = "disabled"
        await session.commit()
    subjects = [{"type": "USER", "user_id": uid} for uid in users.values()]
    status = await http.post(f"/api/admin/agents/{agent_id}/knowledge-binding-status", headers=headers,
                             json={"subjects": subjects, "knowledge_resource_ids": resources})
    assert status.status_code == 200, status.text
    states = status.json()["items"]
    expected = {}
    for index, (name, uid) in enumerate(users.items()):
        count = sum(index in row["bound_subject_indexes"] for row in states)
        partial = any(index in row["partial_subject_indexes"] for row in states)
        state = "UNBOUND" if count == 0 else "BOUND" if count == 2 and not partial else "PARTIAL"
        expected[uid] = (state, sorted({label for row in states
                                      for label in row["inherited_subject_sources"].get(str(index), [])}))
    # Independent checks guard the legacy oracle as well as the optimized path.
    assert expected[users["external"]][0] == "BOUND"
    assert expected[users["expired"]][0] == "UNBOUND"
    assert expected[users["enterprise"]][0] == "BOUND"
    assert expected[users["enterprise-expired"]][0] == "UNBOUND"
    if source_state != "inactive-parent":
        assert expected[users["excluded"]][0] == "UNBOUND"
    assert expected[users["child"]][0] == ("UNBOUND" if source_state == "expired" else "PARTIAL")
    seen = {}
    offset = 0
    for _ in range(20):
        response = await http.post(f"/api/admin/agents/{agent_id}/knowledge-subject-options", headers=headers,
                                  json={"category": "users", "knowledge_resource_ids": resources,
                                        "binding_status": filter_name, "limit": 2, "offset": offset})
        assert response.status_code == 200, response.text
        body = response.json()
        for item in body["items"]:
            uid = item["subject"]["user_id"]
            assert uid not in seen
            seen[uid] = (item["status"], item["inheritance_sources"])
        if body["next_offset"] is None:
            break
        assert len(body["items"]) == 2
        assert body["next_offset"] > offset
        offset = body["next_offset"]
    else:
        pytest.fail("Pagination did not terminate")
    relevant = {uid: value for uid, value in seen.items() if uid in users.values()}
    assert relevant == {uid: value for uid, value in expected.items()
                        if (value[0] != "BOUND" if filter_name == "available" else value[0] != "UNBOUND")}
    response = await http.post(f"/api/admin/agents/{agent_id}/knowledge-subject-options", headers=headers,
                              json={"category": "users", "knowledge_resource_ids": resources,
                                    "binding_status": filter_name, "search": "  CHILD  ", "limit": 2})
    assert response.status_code == 200, response.text
    expected_search = ([] if source_state == "expired" and filter_name == "bound"
                       else [("child", "UNBOUND" if source_state == "expired" else "PARTIAL")])
    assert [(item["label"], item["status"]) for item in response.json()["items"]] == expected_search
    assert response.json()["next_offset"] is None


async def test_batched_keys_preserve_multiple_identities_and_inheritance(setup, option_catalog):
    async with setup[0]() as session:
        ids = list(option_catalog[0].values())
        statements = []
        engine = setup[0].kw["bind"].sync_engine

        def record(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            actual = await batch_user_knowledge_subjects(session, ids)
        finally:
            event.remove(engine, "before_cursor_execute", record)
        assert len(statements) <= 10
        assert actual == {uid: await user_knowledge_subjects(session, uid) for uid in ids}


@pytest.mark.parametrize("extra_users", [40, 540])
@pytest.mark.parametrize("exclusion_type", ["USER", "DEPARTMENT", "GROUP", "IDENTITY_SOURCE"])
async def test_binding_status_user_exclusions_query_budget(
    client, setup, catalog, option_catalog, extra_users, exclusion_type,
):
    http, headers, _ = client
    factory, admin, _ = setup
    agent_id, _, resources, _, _ = catalog
    users, source_id = option_catalog
    async with factory() as session:
        department_id = await session.scalar(select(UserDepartment.department_id).where(
            UserDepartment.user_id == users["department"],
        ))
        exclusions = {key for key in (("USER", users["direct"]), ("DEPARTMENT", department_id),
                                     ("GROUP", source_id, "parent"), ("IDENTITY_SOURCE", source_id))
                      if key[0] == exclusion_type}
        expected_excluded = [index for index, uid in enumerate(users.values())
                             if await user_knowledge_subjects(session, uid) & exclusions]
        bulk = [User(external_id=f"status-bulk-{i}", display_name=f"Bulk {i}",
                     auth_provider=AuthProvider.BUILTIN) for i in range(extra_users)]
        session.add_all(bulk)
        await session.flush()
        ids = [*users.values(), *[user.id for user in bulk]]
        scopes = select(AgentKnowledgeScope.id).where(AgentKnowledgeScope.agent_id == agent_id)
        await session.execute(delete(AgentKnowledgeScopeBinding).where(AgentKnowledgeScopeBinding.scope_id.in_(scopes)))
        await session.execute(delete(AgentKnowledgeScope).where(AgentKnowledgeScope.agent_id == agent_id))
        scope = AgentKnowledgeScope(agent_id=agent_id, name="Direct users", origin="MANUAL",
                                    knowledge_resource_ids_json=json.dumps([resources[0]]),
                                    created_by_user_id=admin.id)
        session.add(scope)
        await session.flush()
        session.add_all([AgentKnowledgeScopeBinding(scope_id=scope.id, user_id=uid) for uid in ids])
        policy = await session.scalar(select(AgentKnowledgePolicy).where(
            AgentKnowledgePolicy.agent_id == agent_id,
            AgentKnowledgePolicy.knowledge_resource_id == resources[0],
        ))
        policy.select_all = False
        policy.excluded_subjects_json = json.dumps(sorted(exclusions))
        await session.commit()

    statements = []
    engine = factory.kw["bind"].sync_engine

    def record(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        response = await http.post(f"/api/admin/agents/{agent_id}/knowledge-binding-status", headers=headers,
                                   json={"subjects": [{"type": "USER", "user_id": uid} for uid in ids],
                                         "knowledge_resource_ids": [resources[0]]})
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["excluded_subject_indexes"] == expected_excluded
    assert item["bound_subject_indexes"] == [index for index in range(len(ids)) if index not in expected_excluded]
    assert item["direct_bound_subject_indexes"] == list(range(len(ids)))
    assert item["partial_subject_indexes"] == []
    assert item["status"] == "PARTIAL"
    assert len(statements) < 50
    assert max(len(parameters) for _, parameters in statements) < 520


@pytest.mark.parametrize("inactive_parent", [False, True])
async def test_batched_group_keys_isolate_sources_and_terminate_cycles(setup, option_catalog, inactive_parent):
    source_id = option_catalog[1]
    async with setup[0]() as session:
        source = EnterpriseIdentitySource.create(name="Other source", provider="feishu", tenant_id="other")
        session.add(source)
        await session.flush()
        session.add_all([
            *[EnterpriseDirectoryGroup(identity_source_id=source.id, external_group_id=group_id,
                                       display_name=group_id, principal_type="group", status="active")
              for group_id in ("child", "other-parent")],
            *[EnterpriseDirectoryMembership(identity_source_id=source.id, external_group_id=parent,
                                            member_type="group", external_member_id=child)
              for parent, child in (("other-parent", "child"), ("child", "other-parent"))],
            EnterpriseDirectoryMembership(identity_source_id=source_id, external_group_id="child",
                                           member_type="group", external_member_id="parent"),
        ])
        if inactive_parent:
            parent = await session.scalar(select(EnterpriseDirectoryGroup).where(
                EnterpriseDirectoryGroup.identity_source_id == source_id,
                EnterpriseDirectoryGroup.external_group_id == "parent",
            ))
            parent.status = "disabled"
        await session.commit()
        statements = []
        engine = setup[0].kw["bind"].sync_engine

        def record(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            actual = await batch_group_knowledge_subjects(session, {(source_id, "child"), (source.id, "child")})
        finally:
            event.remove(engine, "before_cursor_execute", record)
        expected_first = {("IDENTITY_SOURCE", source_id), ("GROUP", source_id, "child")}
        if not inactive_parent:
            expected_first.add(("GROUP", source_id, "parent"))
        assert actual == {
            ("GROUP", source_id, "child"): expected_first,
            ("GROUP", source.id, "child"): {
                ("IDENTITY_SOURCE", source.id), ("GROUP", source.id, "child"), ("GROUP", source.id, "other-parent"),
            },
        }
        assert len(statements) <= 2


@pytest.mark.parametrize("alternative_path", [False, True])
async def test_disabled_intermediate_group_blocks_only_its_path(setup, option_catalog, alternative_path):
    users, source_id = option_catalog
    async with setup[0]() as session:
        edge = await session.scalar(select(EnterpriseDirectoryMembership).where(
            EnterpriseDirectoryMembership.identity_source_id == source_id,
            EnterpriseDirectoryMembership.external_group_id == "parent",
            EnterpriseDirectoryMembership.member_type == "group",
        ))
        edge.external_member_id = "middle"
        session.add(EnterpriseDirectoryGroup(
            identity_source_id=source_id, external_group_id="middle", display_name="middle",
            principal_type="group", status="disabled",
        ))
        session.add(EnterpriseDirectoryMembership(
            identity_source_id=source_id, external_group_id="middle", member_type="group", external_member_id="child",
        ))
        if alternative_path:
            session.add(EnterpriseDirectoryMembership(
                identity_source_id=source_id, external_group_id="parent", member_type="group", external_member_id="child",
            ))
        await session.flush()
        members = set(await session.scalars(await knowledge_subject_members(session, ("GROUP", source_id, "parent"))))
        assert (users["external"] in members) == alternative_path
        assert users["local"] in members


async def test_many_direct_exclusions_support_search_and_pagination(client, setup, catalog, option_catalog):
    http, headers, _ = client
    agent_id, _, resources, _, _ = catalog
    async with setup[0]() as session:
        users = [User(external_id=f"excluded-{i}", display_name=f"Excluded {i:03d}",
                      auth_provider=AuthProvider.BUILTIN) for i in range(501)]
        session.add_all(users)
        await session.flush()
        policy = await session.scalar(select(AgentKnowledgePolicy).where(
            AgentKnowledgePolicy.agent_id == agent_id,
            AgentKnowledgePolicy.knowledge_resource_id == resources[0],
        ))
        policy.excluded_subjects_json = json.dumps([["USER", user.id] for user in users])
        await session.commit()
    payload = {"category": "users", "knowledge_resource_ids": [resources[0]],
               "binding_status": "available", "search": "Excluded", "limit": 20}
    seen = set()
    for _ in range(2):
        response = await http.post(f"/api/admin/agents/{agent_id}/knowledge-subject-options",
                                   headers=headers, json=payload)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == 20
        for item in body["items"]:
            uid = item["subject"]["user_id"]
            assert uid not in seen
            seen.add(uid)
            assert item["status"] == "UNBOUND"
        assert body["next_offset"] is not None
        payload["offset"] = body["next_offset"]
