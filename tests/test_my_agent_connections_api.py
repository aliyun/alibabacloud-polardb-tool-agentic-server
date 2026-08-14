from __future__ import annotations

from datetime import timedelta

from server.core import agent_user_token_service
from server.models.base import utc_now
from server.models import (
    Agent,
    AgentGroupAssignment,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    AuthProvider,
    Department,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
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
        await session.flush()
        session.add(
            AgentPolarRAGInstanceBinding(
                agent_id=agent.id,
                polarrag_instance_id=instance.id,
                created_by_user_id=admin.id,
            )
        )
        assignment = AgentUserAssignment(
            agent_id=agent.id,
            user_id=member.id,
            created_by_user_id=admin.id,
        )
        session.add(assignment)
        await session.commit()
        return assignment.id


async def test_user_manages_only_own_agent_token(client, setup) -> None:
    http, admin_headers, member_headers = client
    assignment_id = await _seed(setup)

    listed = await http.get(
        "/api/me/agent-connections", headers=member_headers
    )
    assert listed.status_code == 200
    assert listed.json()[0]["assignment_id"] == assignment_id
    assert listed.json()[0]["polarrag_instances"][0]["name"] == "RAG"
    assert "host" not in listed.text

    issued = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/issue",
        headers=member_headers,
    )
    assert issued.status_code == 200
    assert issued.headers["cache-control"] == "no-store"
    assert issued.json()["token"] is None
    assert issued.json()["expires_at"] is None
    assert "issued_at" not in issued.json()

    summary = await http.get(
        "/api/me/agent-connections", headers=member_headers
    )
    assert "pas_user_agent_" in summary.text

    hidden = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/reveal",
        json={"password": "password"},
        headers=admin_headers,
    )
    assert hidden.status_code == 404

    revealed = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/reveal",
        json={"password": "password"},
        headers=member_headers,
    )
    assert revealed.status_code == 200
    assert revealed.headers["cache-control"] == "no-store"
    plaintext = revealed.json()["token"]
    assert plaintext.startswith("pas_user_agent_")

    regenerated = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/regenerate",
        json={"confirmed": True},
        headers=member_headers,
    )
    assert regenerated.status_code == 200
    assert regenerated.json()["token"] is None
    replacement = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/reveal",
        json={"password": "password"},
        headers=member_headers,
    )
    assert replacement.json()["token"] != plaintext

    revoked = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/revoke",
        headers=member_headers,
    )
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["token"] is None


async def test_user_sets_expiry_when_issuing_and_regenerating(
    client, setup
) -> None:
    http, _admin_headers, member_headers = client
    assignment_id = await _seed(setup)
    first_expiry = (utc_now() + timedelta(hours=1)).replace(microsecond=0)

    issued = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/issue",
        json={"expires_at": first_expiry.isoformat()},
        headers=member_headers,
    )

    assert issued.status_code == 200
    assert issued.json()["expires_at"] == first_expiry.isoformat().replace(
        "+00:00", "Z"
    )
    summary = await http.get(
        "/api/me/agent-connections", headers=member_headers
    )
    assert summary.json()[0]["token"]["expires_at"] == issued.json()[
        "expires_at"
    ]

    replacement_expiry = (utc_now() + timedelta(days=1)).replace(
        microsecond=0
    )
    regenerated = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/regenerate",
        json={
            "confirmed": True,
            "expires_at": replacement_expiry.isoformat(),
        },
        headers=member_headers,
    )

    assert regenerated.status_code == 200
    assert regenerated.json()["expires_at"] == (
        replacement_expiry.isoformat().replace("+00:00", "Z")
    )


async def test_user_token_confirmation_is_strict(client, setup) -> None:
    http, _admin_headers, member_headers = client
    assignment_id = await _seed(setup)
    response = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/regenerate",
        json={"confirmed": False},
        headers=member_headers,
    )
    assert response.status_code == 422


async def test_oidc_user_receives_token_once_on_issue_and_regenerate(
    client,
    setup,
) -> None:
    http, _admin_headers, member_headers = client
    factory, _admin, member = setup
    assignment_id = await _seed(setup)
    async with factory() as session:
        stored = await session.get(type(member), member.id)
        assert stored is not None
        stored.auth_provider = AuthProvider.OIDC
        stored.password_hash = None
        await session.commit()

    issued = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/issue",
        headers=member_headers,
    )
    assert issued.status_code == 200
    assert issued.headers["cache-control"] == "no-store"
    first = issued.json()["token"]
    assert first.startswith("pas_user_agent_")

    listed = await http.get(
        "/api/me/agent-connections",
        headers=member_headers,
    )
    assert first not in listed.text
    assert listed.json()[0]["password_reveal_available"] is False

    reveal = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/reveal",
        json={"password": "irrelevant"},
        headers=member_headers,
    )
    assert reveal.status_code == 409

    regenerated = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/regenerate",
        json={"confirmed": True},
        headers=member_headers,
    )
    assert regenerated.status_code == 200
    assert regenerated.headers["cache-control"] == "no-store"
    replacement = regenerated.json()["token"]
    assert replacement.startswith("pas_user_agent_")
    assert replacement != first

    for _ in range(3):
        response = await http.post(
            f"/api/me/agent-connections/{assignment_id}/token/regenerate",
            json={"confirmed": True},
            headers=member_headers,
        )
        assert response.status_code == 200
    limited = await http.post(
        f"/api/me/agent-connections/{assignment_id}/token/regenerate",
        json={"confirmed": True},
        headers=member_headers,
    )
    assert limited.status_code == 429


async def test_department_member_can_issue_but_loses_access_immediately(
    client, setup
) -> None:
    http, _admin_headers, member_headers = client
    factory, admin, member = setup
    async with factory() as session:
        agent = Agent(name="department-agent", created_by=admin.id)
        department = Department(name="Engineering")
        session.add_all([agent, department])
        await session.flush()
        membership = UserDepartment(
            user_id=member.id,
            department_id=department.id,
        )
        session.add_all(
            [
                membership,
                AgentGroupAssignment.for_department(
                    agent_id=agent.id,
                    department_id=department.id,
                    created_by_user_id=admin.id,
                ),
            ]
        )
        await session.commit()
        agent_id = agent.id
        membership_id = membership.id

    listed = await http.get(
        "/api/me/agent-connections",
        headers=member_headers,
    )
    assert listed.status_code == 200
    assert listed.json()[0]["agent_id"] == agent_id
    assert listed.json()[0]["assignment_id"] is None

    issued = await http.post(
        f"/api/me/agent-connections/{agent_id}/token/issue",
        headers=member_headers,
    )
    assert issued.status_code == 200
    assert issued.json()["token"] is None

    wrong_password = await http.post(
        f"/api/me/agent-connections/{agent_id}/token/reveal",
        json={"password": "wrong-password"},
        headers=member_headers,
    )
    assert wrong_password.status_code == 401
    revealed = await http.post(
        f"/api/me/agent-connections/{agent_id}/token/reveal",
        json={"password": "password"},
        headers=member_headers,
    )
    assert revealed.status_code == 200
    plaintext = revealed.json()["token"]
    assert plaintext.startswith("pas_user_agent_")

    async with factory() as session:
        membership = await session.get(UserDepartment, membership_id)
        assert membership is not None
        await session.delete(membership)
        await session.commit()

    assert (
        await http.get(
            "/api/me/agent-connections",
            headers=member_headers,
        )
    ).json() == []
    async with factory() as session:
        assert (
            await agent_user_token_service.resolve_token(session, plaintext)
            is None
        )
