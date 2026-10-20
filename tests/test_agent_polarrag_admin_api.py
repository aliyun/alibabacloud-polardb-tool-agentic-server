from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import json

from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select

from server.app import create_app
from server.api.agent_polarrag_access import _assign_all_users_in_background

from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentUserAssignment,
    AuditLog,
    AuthProvider,
    Department,
    EnterpriseDirectoryPrincipalType,
    EnterpriseDirectoryMembershipType,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    EnterpriseIdentitySourceStatus,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalType,
    KnowledgeBindingMode,
    KnowledgeResource,
    KnowledgeResourceSyncStatus,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    PolarRAGSpace,
    IdentitySourceProvider,
    User,
    UserStatus,
    UserDepartment,
    UserExternalIdentity,
)
from server.enterprise_identity.service import (
    identity_provider_key,
    upsert_directory_group,
    upsert_directory_membership,
    upsert_external_user,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def test_bind_all_builtin_users_is_dynamic_without_materialized_assignments(client, setup) -> None:
    from server.core.agent_access import has_agent_access, has_group_agent_access
    from server.core.agent_user_token_service import resolve_token

    http, admin_headers, member_headers = client
    factory, admin, member = setup
    agent_id, _instance_id, _member_id = await _seed(setup)
    url = f"/api/agents/{agent_id}/all-subject-assignments"
    for _ in range(2):
        response = await http.post(url, json={"operation": "BIND"}, headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["status"] == "BOUND"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(AgentUserAssignment)) == 0
        later = User(external_id="later", display_name="Later", auth_provider=AuthProvider.BUILTIN)
        oidc = User(external_id="external", display_name="External", auth_provider=AuthProvider.OIDC)
        disabled = User(
            external_id="disabled", display_name="Disabled", auth_provider=AuthProvider.BUILTIN,
            status=UserStatus.DISABLED,
        )
        session.add_all([later, oidc, disabled])
        await session.commit()
        assert await has_agent_access(session, agent_id, later.id)
        assert await has_group_agent_access(session, agent_id, member.id)
        assert not await has_agent_access(session, agent_id, oidc.id)
        assert not await has_agent_access(session, agent_id, disabled.id)
    options = await http.get(
        f"/api/agents/{agent_id}/user-options?binding_status=bound&limit=1&search=Later",
        headers=admin_headers,
    )
    assert options.status_code == 200
    assert options.json()["total"] == 1
    assert options.json()["items"][0]["assignment_id"] is None
    assert options.json()["items"][0]["inherited_source_names"] == ["PAS"]
    direct = await http.post(
        f"/api/agents/{agent_id}/user-assignments", json={"user_id": later.id}, headers=admin_headers,
    )
    assert direct.status_code == 201
    removed = await http.delete(
        f"/api/agents/{agent_id}/user-assignments/{direct.json()['id']}", headers=admin_headers,
    )
    assert removed.status_code == 204
    async with factory() as session:
        assert await has_agent_access(session, agent_id, later.id)
    issued = await http.post(f"/api/me/agent-connections/{agent_id}/token/issue", headers=member_headers)
    assert issued.status_code == 200
    revealed = await http.post(f"/api/me/agent-connections/{agent_id}/token/reveal", headers=member_headers)
    assert revealed.status_code == 200
    token = revealed.json()["token"]
    async with factory() as session:
        assert await resolve_token(session, token) is not None
    response = await http.post(url, json={"operation": "UNBIND"}, headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "UNBOUND"
    async with factory() as session:
        assert not await has_agent_access(session, agent_id, later.id)
        assert await resolve_token(session, token) is None


async def _seed(setup):
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(name="knowledge-agent", created_by=admin.id)
        instance = PolarRAGInstance(
            name="RAG",
            scheme="http",
            host="rag.example.test",
            port=9200,
            username_ciphertext="username",
            password_ciphertext="password",
            status=PolarRAGInstanceStatus.ACTIVE,
            created_by=admin.id,
        )
        session.add_all([agent, instance])
        await session.commit()
        return agent.id, instance.id, member.id


async def test_admin_binds_instance_and_assigns_user(client, setup) -> None:
    http, admin_headers, member_headers = client
    agent_id, instance_id, member_id = await _seed(setup)

    forbidden = await http.post(
        f"/api/agents/{agent_id}/polarrag-bindings",
        json={"polarrag_instance_id": instance_id},
        headers=member_headers,
    )
    assert forbidden.status_code == 403

    binding = await http.post(
        f"/api/agents/{agent_id}/polarrag-bindings",
        json={"polarrag_instance_id": instance_id},
        headers=admin_headers,
    )
    assert binding.status_code == 201
    assert binding.json()["instance_name"] == "RAG"
    assert binding.json()["public_knowledge_resource_ids"] is None
    duplicate = await http.post(
        f"/api/agents/{agent_id}/polarrag-bindings",
        json={"polarrag_instance_id": instance_id},
        headers=admin_headers,
    )
    assert duplicate.status_code == 409

    assignment = await http.post(
        f"/api/agents/{agent_id}/user-assignments",
        json={"user_id": member_id},
        headers=admin_headers,
    )
    assert assignment.status_code == 201
    assert assignment.json()["user_name"] == "Member"
    assert assignment.json()["token"] is None
    assert "token_ciphertext" not in assignment.text

    listed = await http.get(
        f"/api/agents/{agent_id}/user-assignments",
        headers=admin_headers,
    )
    assert [row["id"] for row in listed.json()["items"]] == [
        assignment.json()["id"]
    ]


async def test_user_assignment_candidates_and_bulk_assignment_are_paginated(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(name="bulk-user-agent", created_by=admin.id)
        extra_users = [
            User(
                external_id=f"member-{index}",
                display_name=f"Member {index}",
                auth_provider=AuthProvider.BUILTIN,
            )
            for index in range(3)
        ]
        session.add_all([agent, *extra_users])
        await session.commit()
        agent_id = agent.id

    candidates = await http.get(
        f"/api/agents/{agent_id}/user-options",
        params={"search": "Member", "offset": 0, "limit": 2},
        headers=admin_headers,
    )
    assert candidates.status_code == 200
    assert candidates.json()["total"] == 4
    assert len(candidates.json()["items"]) == 2

    created = await http.post(
        f"/api/agents/{agent_id}/user-assignments/bulk",
        headers=admin_headers,
    )
    assert created.status_code in {200, 202}
    assert created.json()["status"] in {"running", "completed"}

    status = await http.get(
        f"/api/agents/{agent_id}/user-assignments/bulk/status",
        headers=admin_headers,
    )
    assert status.status_code == 200
    assert status.json()["status"] == "completed"

    assignments = await http.get(
        f"/api/agents/{agent_id}/user-assignments",
        params={"search": "Member 1", "offset": 0, "limit": 2},
        headers=admin_headers,
    )
    assert assignments.status_code == 200
    assert assignments.json()["total"] == 1
    assert [item["user_name"] for item in assignments.json()["items"]] == [
        "Member 1"
    ]
    assert member.id


async def test_bulk_assignment_status_and_claim_are_shared_across_app_instances(
    client, setup
) -> None:
    http_one, admin_headers, _member_headers = client
    factory, admin, _member = setup
    async with factory() as session:
        agent = Agent(name="shared-bulk-agent", created_by=admin.id)
        agent.bulk_assignment_status = "running"
        agent.bulk_assignment_worker_id = "first-process"
        agent.bulk_assignment_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        session.add(agent)
        await session.commit()
        agent_id = agent.id

    app_two = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app_two), base_url="http://second-app"
    ) as http_two:
        first = await http_one.get(
            f"/api/agents/{agent_id}/user-assignments/bulk/status",
            headers=admin_headers,
        )
        second = await http_two.get(
            f"/api/agents/{agent_id}/user-assignments/bulk/status",
            headers=admin_headers,
        )
        assert first.json()["status"] == second.json()["status"] == "running"

        duplicate = await http_two.post(
            f"/api/agents/{agent_id}/user-assignments/bulk",
            headers=admin_headers,
        )
        assert duplicate.status_code == 202
        assert duplicate.json()["status"] == "running"

        async with factory() as session:
            stored = await session.get(Agent, agent_id)
            assert stored is not None
            stored.bulk_assignment_lease_until = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
        restarted = await http_two.post(
            f"/api/agents/{agent_id}/user-assignments/bulk",
            headers=admin_headers,
        )
        assert restarted.status_code in {200, 202}
        for _ in range(20):
            await asyncio.sleep(0)
            status = await http_one.get(
                f"/api/agents/{agent_id}/user-assignments/bulk/status",
                headers=admin_headers,
            )
            if status.json()["status"] == "completed":
                break
        assert status.json()["status"] == "completed"


async def test_reclaimed_bulk_assignment_rolls_back_stale_worker_result(
    setup,
) -> None:
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(
            name="fenced-bulk-agent",
            created_by=admin.id,
            bulk_assignment_status="running",
            bulk_assignment_worker_id="old-worker",
            bulk_assignment_lease_until=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(agent)
        await session.flush()
        session.add_all(
            [
                AgentUserAssignment(
                    agent_id=agent.id,
                    user_id=user_id,
                    created_by_user_id=admin.id,
                    is_direct=False,
                )
                for user_id in (admin.id, member.id)
            ]
        )
        await session.commit()
        agent_id = agent.id

    old_started = asyncio.Event()
    release_old = asyncio.Event()

    @asynccontextmanager
    async def old_worker_factory():
        async with factory() as session:
            execute = session.execute

            async def execute_with_pause(statement, *args, **kwargs):
                result = await execute(statement, *args, **kwargs)
                descriptions = getattr(statement, "column_descriptions", ())
                if (
                    len(descriptions) == 1
                    and descriptions[0].get("expr") is User.id
                ):
                    old_started.set()
                    await release_old.wait()
                return result

            session.execute = execute_with_pause
            yield session

    old_task = asyncio.create_task(
        _assign_all_users_in_background(
            old_worker_factory,
            agent_id,
            admin.id,
            "old-worker",
        )
    )
    await old_started.wait()

    async with factory() as session:
        already_updated = await session.scalar(
            select(AgentUserAssignment).where(
                AgentUserAssignment.agent_id == agent_id,
                AgentUserAssignment.user_id == admin.id,
            )
        )
        assert already_updated is not None
        already_updated.is_direct = True
        stored = await session.get(Agent, agent_id)
        assert stored is not None
        stored.bulk_assignment_worker_id = "new-worker"
        stored.bulk_assignment_lease_until = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()

    await _assign_all_users_in_background(
        factory,
        agent_id,
        admin.id,
        "new-worker",
    )
    release_old.set()
    await old_task

    async with factory() as session:
        stored = await session.get(Agent, agent_id)
        assert stored is not None
        assert stored.bulk_assignment_status == "completed"
        assert stored.bulk_assignment_created_count == 1
        assert stored.bulk_assignment_worker_id is None


async def test_admin_updates_agent_public_kb_scope_with_audit(
    client,
    setup,
) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, _member = setup
    async with factory() as session:
        agent = Agent(name="scoped-agent", created_by=admin.id)
        instances = [
            PolarRAGInstance(
                name=f"RAG {index}",
                scheme="http",
                host=f"rag-{index}.example.test",
                port=9200,
                username_ciphertext="username",
                password_ciphertext="password",
                status=PolarRAGInstanceStatus.ACTIVE,
                created_by=admin.id,
            )
            for index in (1, 2)
        ]
        session.add_all([agent, *instances])
        await session.flush()
        spaces = [
            PolarRAGSpace(
                polarrag_instance_id=instance.id,
                space_id=f"space-{index}",
                name=f"Space {index}",
                identity_domain="tenant-a",
                enabled=True,
            )
            for index, instance in enumerate(instances, 1)
        ]
        session.add_all(spaces)
        await session.flush()

        def resource(
            *,
            space_index: int,
            kb_id: str,
            kb_type: str,
            active: bool = True,
        ) -> KnowledgeResource:
            space = spaces[space_index]
            return KnowledgeResource(
                knowledge_space_id=space.knowledge_space_id,
                polarrag_instance_id=space.polarrag_instance_id,
                space_id=space.space_id,
                kb_id=kb_id,
                name=kb_id,
                kb_type=kb_type,
                identity_domain=space.identity_domain,
                binding_mode=(
                    KnowledgeBindingMode.DOMAIN
                    if kb_type == "PUBLIC"
                    else KnowledgeBindingMode.OWNER
                ),
                sync_status=(
                    KnowledgeResourceSyncStatus.ACTIVE
                    if active
                    else KnowledgeResourceSyncStatus.UPSTREAM_DISABLED
                ),
                enabled=active,
            )

        selected = resource(space_index=0, kb_id="public-selected", kb_type="PUBLIC")
        other_public = resource(space_index=0, kb_id="public-other", kb_type="PUBLIC")
        personal = resource(space_index=0, kb_id="personal", kb_type="PERSONAL")
        inactive = resource(
            space_index=0,
            kb_id="inactive",
            kb_type="PUBLIC",
            active=False,
        )
        cross_instance = resource(space_index=1, kb_id="cross", kb_type="PUBLIC")
        session.add_all(
            [selected, other_public, personal, inactive, cross_instance]
        )
        await session.commit()
        agent_id = agent.id
        instance_id = instances[0].id

    binding = (
        await http.post(
            f"/api/agents/{agent_id}/polarrag-bindings",
            json={"polarrag_instance_id": instance_id},
            headers=admin_headers,
        )
    ).json()

    options = await http.get(
        f"/api/agents/{agent_id}/polarrag-bindings/{binding['id']}/public-resources",
        headers=admin_headers,
    )
    assert options.status_code == 200
    assert options.json() == {
        "items": [
            {
                "knowledge_resource_id": other_public.id,
                "space_id": "space-1",
                "kb_id": "public-other",
                "name": "public-other",
                "knowledge_space_name": "Space 1",
            },
            {
                "knowledge_resource_id": selected.id,
                "space_id": "space-1",
                "kb_id": "public-selected",
                "name": "public-selected",
                "knowledge_space_name": "Space 1",
            },
        ],
        "total": 2,
        "offset": 0,
        "limit": 50,
    }

    paged_options = await http.get(
        f"/api/agents/{agent_id}/polarrag-bindings/{binding['id']}/public-resources",
        params={"offset": 1, "limit": 1, "search": "selected"},
        headers=admin_headers,
    )
    assert paged_options.status_code == 200
    assert paged_options.json() == {
        "items": [],
        "total": 1,
        "offset": 1,
        "limit": 1,
    }

    updated = await http.put(
        f"/api/agents/{agent_id}/polarrag-bindings/{binding['id']}/public-resources",
        json={"public_knowledge_resource_ids": [selected.id]},
        headers=admin_headers,
    )
    assert updated.status_code == 200
    assert updated.json()["public_knowledge_resource_ids"] == [selected.id]

    for invalid_id in (personal.id, inactive.id, cross_instance.id):
        rejected = await http.put(
            f"/api/agents/{agent_id}/polarrag-bindings/{binding['id']}/public-resources",
            json={"public_knowledge_resource_ids": [invalid_id]},
            headers=admin_headers,
        )
        assert rejected.status_code == 422

    restored = await http.put(
        f"/api/agents/{agent_id}/polarrag-bindings/{binding['id']}/public-resources",
        json={"public_knowledge_resource_ids": None},
        headers=admin_headers,
    )
    assert restored.status_code == 200
    assert restored.json()["public_knowledge_resource_ids"] is None

    async with factory() as session:
        audits = list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.action
                        == "agent_polarrag_binding.public_scope.update"
                    )
                    .order_by(AuditLog.created_at)
                )
            ).scalars()
        )
    assert len(audits) == 2
    assert [
        json.loads(json.loads(row.metadata_json or "{}")["client_info"])
        for row in audits
    ] == [
        {"public_scope": "selected", "resource_count": 1},
        {"public_scope": "all", "resource_count": 0},
    ]


async def test_admin_force_revoke_never_returns_plaintext(
    client, setup
) -> None:
    http, admin_headers, member_headers = client
    agent_id, instance_id, member_id = await _seed(setup)
    await http.post(
        f"/api/agents/{agent_id}/polarrag-bindings",
        json={"polarrag_instance_id": instance_id},
        headers=admin_headers,
    )
    assignment = (
        await http.post(
            f"/api/agents/{agent_id}/user-assignments",
            json={"user_id": member_id},
            headers=admin_headers,
        )
    ).json()
    issued = await http.post(
        f"/api/me/agent-connections/{assignment['id']}/token/issue",
        headers=member_headers,
    )
    assert issued.status_code == 200
    revealed = await http.post(
        f"/api/me/agent-connections/{assignment['id']}/token/reveal",
        headers=member_headers,
    )
    plaintext = revealed.json()["token"]

    revoked = await http.post(
        f"/api/agents/{agent_id}/user-assignments/{assignment['id']}/token/revoke",
        headers=admin_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["token"]["status"] == "revoked"
    assert plaintext not in revoked.text


async def test_admin_assigns_department_and_registered_enterprise_group(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(name="group-agent", created_by=admin.id)
        department = Department(name="Engineering")
        bob = User(
            external_id="bob",
            display_name="Bob",
            auth_provider=AuthProvider.BUILTIN,
        )
        session.add_all([agent, department, bob])
        await session.flush()
        session.add_all(
            [
                UserDepartment(
                    user_id=member.id,
                    department_id=department.id,
                ),
                EnterprisePrincipalAssignment.create(
                    pas_user_id=bob.id,
                    identity_domain="mcp-e2e-domain",
                    provider="feishu",
                    principal_type=EnterprisePrincipalType.GROUP,
                    principal_id="finance-e2e",
                    source=EnterprisePrincipalSource.ADMIN_MANAGED,
                ),
            ]
        )
        await session.commit()
        agent_id = agent.id
        department_id = department.id

    options = await http.get(
        f"/api/agents/{agent_id}/group-options",
        headers=admin_headers,
    )
    assert options.status_code == 200
    assert options.json()["items"] == [
        {
            "group_kind": "department",
            "department_id": department_id,
            "department_name": "Engineering",
                "identity_domain": None,
                "provider": None,
                "identity_source_id": None,
                "identity_source_name": None,
                "external_group_id": None,
                "external_group_name": None,
                "principal_id": None,
            "member_count": 1,
        },
        {
            "group_kind": "enterprise",
            "department_id": None,
            "department_name": None,
                "identity_domain": "mcp-e2e-domain",
                "provider": "feishu",
                "identity_source_id": None,
                "identity_source_name": None,
                "external_group_id": None,
                "external_group_name": None,
                "principal_id": "finance-e2e",
            "member_count": 1,
        },
    ]

    department_grant = await http.post(
        f"/api/agents/{agent_id}/group-assignments",
        json={
            "group_kind": "department",
            "department_id": department_id,
        },
        headers=admin_headers,
    )
    assert department_grant.status_code == 201
    enterprise_grant = await http.post(
        f"/api/agents/{agent_id}/group-assignments",
        json={
            "group_kind": "enterprise",
            "identity_domain": "mcp-e2e-domain",
            "provider": "feishu",
            "principal_id": "finance-e2e",
        },
        headers=admin_headers,
    )
    assert enterprise_grant.status_code == 201

    listed = await http.get(
        f"/api/agents/{agent_id}/group-assignments",
        headers=admin_headers,
    )
    assert [row["group_kind"] for row in listed.json()["items"]] == [
        "department",
        "enterprise",
    ]
    searched = await http.get(
        f"/api/agents/{agent_id}/group-assignments",
        params={"search": "finance", "offset": 0, "limit": 20},
        headers=admin_headers,
    )
    assert searched.status_code == 200
    assert searched.json()["total"] == 1
    assert searched.json()["items"][0]["group_kind"] == "enterprise"

    unknown = await http.post(
        f"/api/agents/{agent_id}/group-assignments",
        json={
            "group_kind": "enterprise",
            "identity_domain": "mcp-e2e-domain",
            "provider": "feishu",
            "principal_id": "not-registered",
        },
        headers=admin_headers,
    )
    assert unknown.status_code == 404


async def test_admin_assigns_synced_identity_source_group(client, setup) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, _member = setup
    async with factory() as session:
        agent = Agent(name="synced-group-agent", created_by=admin.id)
        source = EnterpriseIdentitySource.create(
            name="Feishu directory",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-001",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        session.add_all([agent, source])
        await session.flush()
        user = await upsert_external_user(
            session,
            source,
            external_user_id="ou-001",
            display_name="Alice",
            email=None,
        )
        group = await upsert_directory_group(
            session,
            source,
            external_group_id="oc-engineering",
            display_name="Engineering",
        )
        department = await upsert_directory_group(
            session,
            source,
            external_group_id="od-research",
            display_name="Research",
            principal_type=EnterpriseDirectoryPrincipalType.DEPARTMENT,
        )
        await upsert_directory_membership(
            session,
            source,
            external_group_id=group.external_group_id,
            member_type=EnterpriseDirectoryMembershipType.USER,
            external_member_id="ou-001",
        )
        await session.commit()
        agent_id = agent.id
        source_id = source.id
        assert user.id

    options = await http.get(
        f"/api/agents/{agent_id}/group-options",
        headers=admin_headers,
    )
    option = next(
        item
        for item in options.json()["items"]
        if item["group_kind"] == "identity_source"
        and item["external_group_id"] == group.external_group_id
    )
    assert option["identity_source_id"] == source_id
    assert option["identity_source_name"] == "Feishu directory"
    assert option["external_group_id"] == "oc-engineering"
    assert option["external_group_name"] == "Engineering"
    assert option["member_count"] == 1
    assert any(
        item["group_kind"] == "identity_source"
        and item["external_group_id"] == department.external_group_id
        and item["external_group_name"] == "Research"
        for item in options.json()["items"]
    )

    created = await http.post(
        f"/api/agents/{agent_id}/group-assignments",
        json={
            "group_kind": "identity_source",
            "identity_source_id": source_id,
            "principal_id": "oc-engineering",
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    assert created.json()["identity_source_id"] == source_id


async def test_admin_assigns_all_synchronized_identity_source_users(
    client, setup
) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, _member = setup
    async with factory() as session:
        agent = Agent(name="synced-source-agent", created_by=admin.id)
        source = EnterpriseIdentitySource.create(
            name="Feishu directory",
            provider=IdentitySourceProvider.FEISHU,
            tenant_id="tenant-001",
        )
        source.status = EnterpriseIdentitySourceStatus.ACTIVE
        session.add_all([agent, source])
        await session.flush()
        await upsert_external_user(
            session,
            source,
            external_user_id="ou-001",
            display_name="Alice",
            email=None,
        )
        await session.commit()
        agent_id = agent.id
        source_id = source.id

    options = await http.get(
        f"/api/agents/{agent_id}/group-options",
        headers=admin_headers,
    )
    option = next(
        item
        for item in options.json()["items"]
        if item["group_kind"] == "identity_source_all"
    )
    assert option == {
        "group_kind": "identity_source_all",
        "department_id": None,
        "department_name": None,
        "identity_domain": None,
        "provider": None,
        "identity_source_id": source_id,
        "identity_source_name": "Feishu directory",
        "external_group_id": None,
        "external_group_name": None,
        "principal_id": None,
        "member_count": 1,
    }

    source_options = await http.get(
        f"/api/agents/{agent_id}/group-options",
        params={"category": "identity_source", "offset": 0, "limit": 20},
        headers=admin_headers,
    )
    assert source_options.status_code == 200
    assert source_options.json()["total"] == 1
    assert {
        item["group_kind"] for item in source_options.json()["items"]
    } == {"identity_source_all"}

    created = await http.post(
        f"/api/agents/{agent_id}/group-assignments",
        json={
            "group_kind": "identity_source_all",
            "identity_source_id": source_id,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    assert created.json()["principal_id"] is None
    assert created.json()["member_count"] == 1

    source_assignments = await http.get(
        f"/api/agents/{agent_id}/group-assignments",
        params={"category": "identity_source", "offset": 0, "limit": 20},
        headers=admin_headers,
    )
    assert source_assignments.status_code == 200
    assert source_assignments.json()["total"] == 1
    assert source_assignments.json()["items"][0]["group_kind"] == "identity_source_all"

    regular_groups = await http.get(
        f"/api/agents/{agent_id}/group-assignments",
        params={"category": "group", "offset": 0, "limit": 20},
        headers=admin_headers,
    )
    assert regular_groups.status_code == 200
    assert regular_groups.json()["total"] == 0


async def test_enterprise_user_options_page_mapped_users_from_fresh_stale_source(client, setup) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(name="directory-user-options", created_by=admin.id)
        source = EnterpriseIdentitySource.create(
            name="Stale directory", provider=IdentitySourceProvider.FEISHU, tenant_id="tenant-options",
        )
        source.status = EnterpriseIdentitySourceStatus.STALE
        source.last_synced_at = datetime.now(UTC)
        second = User(external_id="second-directory-user", display_name="Second")
        session.add_all([agent, source, second])
        await session.flush()
        for external_id, name in [("ou-a", "Alice"), ("ou-b", "Bob"), ("ou-unmapped", "Unmapped")]:
            session.add(EnterpriseDirectoryUser(
                identity_source_id=source.id, external_user_id=external_id, display_name=name,
            ))
        session.add_all([
            UserExternalIdentity(
                user_id=member.id, identity_provider=identity_provider_key(source), external_subject="ou-a",
            ),
            UserExternalIdentity(
                user_id=second.id, identity_provider=identity_provider_key(source), external_subject="ou-b",
            ),
            AgentUserAssignment(agent_id=agent.id, user_id=member.id, created_by_user_id=admin.id),
        ])
        await session.commit()
        agent_id, source_id = agent.id, source.id

    path = f"/api/agents/{agent_id}/enterprise-user-options"
    params = {"identity_source_ids": source_id, "offset": 0, "limit": 1}
    unbound = await http.get(path, params=params, headers=admin_headers)
    assert unbound.status_code == 200
    assert unbound.json()["total"] == 1
    assert [row["external_user_id"] for row in unbound.json()["items"]] == ["ou-b"]

    bound = await http.get(path, params={**params, "binding_status": "bound"}, headers=admin_headers)
    assert bound.status_code == 200
    assert bound.json()["total"] == 1
    assert [row["external_user_id"] for row in bound.json()["items"]] == ["ou-a"]

    async with factory() as session:
        source = await session.get(EnterpriseIdentitySource, source_id)
        source.last_synced_at = datetime.now(UTC) - timedelta(days=8)
        await session.commit()
    expired = await http.get(path, params=params, headers=admin_headers)
    assert expired.status_code == 200
    assert expired.json()["total"] == 0


async def test_group_options_fetch_is_bounded_by_requested_page(client, setup) -> None:
    http, admin_headers, _member_headers = client
    factory, admin, _member = setup
    async with factory() as session:
        agent = Agent(name="bounded-group-options", created_by=admin.id)
        session.add(agent)
        session.add_all([Department(name=f"Department {index:03d}") for index in range(25)])
        await session.commit()
        agent_id = agent.id

    statements: list[tuple[str, object]] = []

    def record_statement(_conn, _cursor, statement, parameters, _context, _many):
        if "UNION ALL" in statement and " LIMIT " in statement:
            statements.append((statement, parameters))

    event.listen(factory.kw["bind"].sync_engine, "before_cursor_execute", record_statement)
    try:
        response = await http.get(
            f"/api/agents/{agent_id}/group-options",
            params={"offset": 0, "limit": 2},
            headers=admin_headers,
        )
    finally:
        event.remove(factory.kw["bind"].sync_engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert len(statements) == 1
    assert 2 in statements[0][1]


async def test_direct_assignment_upgrades_group_shadow_and_can_be_removed(
    client, setup
) -> None:
    http, admin_headers, member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(name="mixed-grant-agent", created_by=admin.id)
        department = Department(name="Engineering")
        session.add_all([agent, department])
        await session.flush()
        session.add_all(
            [
                UserDepartment(
                    user_id=member.id,
                    department_id=department.id,
                ),
                AgentGroupAssignment.for_department(
                    agent_id=agent.id,
                    department_id=department.id,
                    created_by_user_id=admin.id,
                ),
            ]
        )
        await session.commit()
        agent_id = agent.id

    issued = await http.post(
        f"/api/me/agent-connections/{agent_id}/token/issue",
        headers=member_headers,
    )
    assert issued.status_code == 200
    assignment_id = issued.json()["assignment_id"]

    listed = await http.get(
        f"/api/agents/{agent_id}/user-assignments",
        headers=admin_headers,
    )
    assert listed.json()["items"] == []

    assigned = await http.post(
        f"/api/agents/{agent_id}/user-assignments",
        json={"user_id": member.id},
        headers=admin_headers,
    )
    assert assigned.status_code == 201
    assert assigned.json()["id"] == assignment_id

    removed = await http.delete(
        f"/api/agents/{agent_id}/user-assignments/{assignment_id}",
        headers=admin_headers,
    )
    assert removed.status_code == 204
    assert (
        await http.get(
            f"/api/agents/{agent_id}/user-assignments",
            headers=admin_headers,
        )
    ).json()["items"] == []
    connections = await http.get(
        "/api/me/agent-connections",
        headers=member_headers,
    )
    assert connections.json()["items"][0]["assignment_id"] == assignment_id
    assert connections.json()["items"][0]["token"]["status"] == "active"
    options = await http.get(
        f"/api/agents/{agent_id}/user-options?binding_status=bound&search=Member&limit=1",
        headers=admin_headers,
    )
    assert options.json()["total"] == 1
    assert options.json()["items"][0]["assignment_id"] is None
    assert options.json()["items"][0]["inherited_source_names"] == ["Engineering"]


async def test_dingtalk_user_identity_source_name_is_not_pas(setup):
    from server.api.agent_polarrag_access import _user_identity_source_names

    factory, _, member = setup
    async with factory() as session:
        source = EnterpriseIdentitySource.create(
            name='DingTalk directory', provider=IdentitySourceProvider.DINGTALK, tenant_id='ding-corp',
        )
        session.add(source)
        session.add(UserExternalIdentity(user_id=member.id, identity_provider='dingtalk:ding-corp',
                                         external_subject='ding-user'))
        await session.commit()
        assert await _user_identity_source_names(session, [member.id]) == {member.id: ['DingTalk directory']}
