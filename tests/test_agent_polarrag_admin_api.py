from __future__ import annotations

from server.models import (
    Agent,
    AgentGroupAssignment,
    AuthProvider,
    Department,
    EnterprisePrincipalAssignment,
    EnterprisePrincipalSource,
    EnterprisePrincipalType,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    User,
    UserDepartment,
)

pytest_plugins = ("tests._admin_api_fixtures",)


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
    assert [row["id"] for row in listed.json()] == [assignment.json()["id"]]


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
        json={"password": "password"},
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
    assert options.json() == [
        {
            "group_kind": "department",
            "department_id": department_id,
            "department_name": "Engineering",
            "identity_domain": None,
            "provider": None,
            "principal_id": None,
            "member_count": 1,
        },
        {
            "group_kind": "enterprise",
            "department_id": None,
            "department_name": None,
            "identity_domain": "mcp-e2e-domain",
            "provider": "feishu",
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
    assert [row["group_kind"] for row in listed.json()] == [
        "department",
        "enterprise",
    ]

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
    assert listed.json() == []

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
    ).json() == []
    connections = await http.get(
        "/api/me/agent-connections",
        headers=member_headers,
    )
    assert connections.json()[0]["assignment_id"] == assignment_id
    assert connections.json()[0]["token"]["status"] == "active"
