from __future__ import annotations

from sqlalchemy import select

from server.models import (
    Agent,
    AgentStatus,
    AgentUserAssignment,
    AuditLog,
    UserWorkspace,
)

pytest_plugins = ("tests._admin_api_fixtures",)


async def _grant_agent(factory, *, admin_id: str, user_id: str, name: str) -> Agent:
    async with factory() as session:
        agent = Agent(name=name, created_by=admin_id)
        session.add(agent)
        await session.flush()
        session.add(
            AgentUserAssignment(
                agent_id=agent.id,
                user_id=user_id,
                is_direct=True,
                created_by_user_id=admin_id,
            )
        )
        await session.commit()
        return agent


async def test_workspace_reports_no_agent_access(client, setup) -> None:
    http, _admin_headers, member_headers = client

    response = await http.get("/api/me/workspace", headers=member_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "no_agent_access"
    assert response.json()["default_agent"] is None
    assert response.json()["available_agents"] == []


async def test_workspace_auto_selects_exactly_one_active_agent(
    client,
    setup,
) -> None:
    http, _admin_headers, member_headers = client
    factory, admin, member = setup
    agent = await _grant_agent(
        factory,
        admin_id=admin.id,
        user_id=member.id,
        name="only-agent",
    )

    response = await http.get("/api/me/workspace", headers=member_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["default_agent"] == {
        "id": agent.id,
        "name": "only-agent",
    }
    async with factory() as session:
        workspace = await session.scalar(
            select(UserWorkspace).where(UserWorkspace.user_id == member.id)
        )
        audit = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "user_workspace.default_agent.select"
            )
        )
        assert workspace is not None
        assert workspace.default_agent_id == agent.id
        assert audit is not None
        assert audit.target_id == agent.id


async def test_workspace_requires_selection_and_accepts_authorized_agent(
    client,
    setup,
) -> None:
    http, _admin_headers, member_headers = client
    factory, admin, member = setup
    first = await _grant_agent(
        factory,
        admin_id=admin.id,
        user_id=member.id,
        name="first-agent",
    )
    second = await _grant_agent(
        factory,
        admin_id=admin.id,
        user_id=member.id,
        name="second-agent",
    )

    response = await http.get("/api/me/workspace", headers=member_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "selection_required"
    assert response.json()["default_agent"] is None
    assert [row["id"] for row in response.json()["available_agents"]] == [
        first.id,
        second.id,
    ]

    selected = await http.put(
        "/api/me/workspace/default-agent",
        headers=member_headers,
        json={"agent_id": second.id},
    )

    assert selected.status_code == 200
    assert selected.json()["status"] == "ready"
    assert selected.json()["default_agent"]["id"] == second.id


async def test_workspace_rejects_an_unavailable_agent(client, setup) -> None:
    http, admin_headers, member_headers = client
    factory, admin, _member = setup
    agent = await _grant_agent(
        factory,
        admin_id=admin.id,
        user_id=admin.id,
        name="admin-agent",
    )

    response = await http.put(
        "/api/me/workspace/default-agent",
        headers=member_headers,
        json={"agent_id": agent.id},
    )

    assert response.status_code == 404
    admin_response = await http.put(
        "/api/me/workspace/default-agent",
        headers=admin_headers,
        json={"agent_id": agent.id},
    )
    assert admin_response.status_code == 200


async def test_workspace_retains_a_disabled_default_without_switching(
    client,
    setup,
) -> None:
    http, _admin_headers, member_headers = client
    factory, admin, member = setup
    first = await _grant_agent(
        factory,
        admin_id=admin.id,
        user_id=member.id,
        name="first-agent",
    )
    second = await _grant_agent(
        factory,
        admin_id=admin.id,
        user_id=member.id,
        name="second-agent",
    )
    selected = await http.put(
        "/api/me/workspace/default-agent",
        headers=member_headers,
        json={"agent_id": first.id},
    )
    assert selected.status_code == 200

    async with factory() as session:
        stored = await session.get(Agent, first.id)
        assert stored is not None
        stored.status = AgentStatus.DISABLED
        await session.commit()

    response = await http.get("/api/me/workspace", headers=member_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "default_agent_unavailable"
    assert response.json()["default_agent"] is None
    assert response.json()["available_agents"] == [
        {"id": second.id, "name": "second-agent"}
    ]
    async with factory() as session:
        workspace = await session.scalar(
            select(UserWorkspace).where(UserWorkspace.user_id == member.id)
        )
        assert workspace is not None
        assert workspace.default_agent_id == first.id
