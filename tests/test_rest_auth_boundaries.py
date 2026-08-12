from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.auth.jwt_manager import create_access_token
from server.db.engine import get_session
from server.mcp.db_instance_rest import router as agent_db_instance_router
from server.mcp.server import router as user_rest_router
from server.models import Agent, AgentStatus, Base, User
from server.models.base import utc_now
from tests._helpers import create_test_agent_token, init_test_jwt_keys


async def test_agent_and_user_rest_principals_are_not_interchangeable():
    init_test_jwt_keys()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session:
        user = User(external_id="rest-user", display_name="REST User")
        agent = Agent(name="boundary-agent")
        session.add_all([user, agent])
        await session.flush()
        token_row, agent_token = await create_test_agent_token(session, agent.id)
        user_token = create_access_token(
            {"sub": user.id, "role": "member"}
        )
        await session.commit()

    app = FastAPI()
    app.include_router(user_rest_router)
    app.include_router(agent_db_instance_router)

    async def session_override():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        user_route = await client.post(
            "/mcp/rest/run_sql",
            headers={"Authorization": f"Bearer {agent_token}"},
            json={"sql": "SELECT 1"},
        )
        assert user_route.status_code == 401

        body = {
            "client_token": "auth-boundary",
            "db_type": "polardb_mysql",
            "provisioning_mode": "multitenant",
        }
        user_bearer = await client.post(
            "/mcp/rest/db-instances",
            json=body,
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert user_bearer.status_code == 401
        assert user_bearer.json()["code"] == "UNAUTHORIZED"
        assert user_bearer.json()["request_id"]

        client.cookies.set("session_token", user_token)
        user_cookie = await client.post("/mcp/rest/db-instances", json=body)
        client.cookies.clear()
        assert user_cookie.status_code == 401
        assert user_cookie.json()["code"] == "UNAUTHORIZED"
        assert user_cookie.json()["request_id"]

        token_row.revoked_at = utc_now()
        await session.merge(token_row)
        await session.commit()
        revoked = await client.post(
            "/mcp/rest/db-instances",
            headers={"Authorization": f"Bearer {agent_token}"},
            json=body,
        )
        assert revoked.status_code == 401

        token_row.revoked_at = None
        token_row.expires_at = utc_now() - timedelta(seconds=1)
        await session.merge(token_row)
        await session.commit()
        expired = await client.post(
            "/mcp/rest/db-instances",
            headers={"Authorization": f"Bearer {agent_token}"},
            json=body,
        )
        assert expired.status_code == 401

        token_row.expires_at = None
        await session.merge(token_row)
        agent.status = AgentStatus.DISABLED
        await session.merge(agent)
        await session.commit()
        disabled = await client.post(
            "/mcp/rest/db-instances",
            headers={"Authorization": f"Bearer {agent_token}"},
            json=body,
        )
        assert disabled.status_code == 401

    await engine.dispose()
