import base64
import json
import logging
import os
import time
import uuid
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
import jwt as jose_jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.aliyun.polardb_client import MockPolarDBClient, set_polardb_client, reset_polardb_client
from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.jwt_manager import create_access_token, reset_keys, _load_keys
from server.config import reset_config, get_config
from tests._helpers import init_test_jwt_keys
from server.core.sql_executor import reset_rate_limiters
from server.db import engine as engine_mod
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentInstanceBindingCapability,
    AgentPolarRAGInstanceBinding,
    AgentUserAssignment,
    AllocationMode,
    AuditLog,
    AuditStatus,
    AuthProvider,
    Base,
    BindingCapability,
    CredentialCapability,
    CredentialPurpose,
    Instance,
    InstanceCredential,
    InstanceStatus,
    InstanceTopology,
    Permission,
    PolarRAGInstance,
    PolarRAGInstanceStatus,
    User,
    UserInstanceBinding,
    UserRole,
)
from server.core.agent_user_token_service import issue_token
from server.core.crypto import encrypt
from server.mcp.transport import mcp_lifespan, reset_mcp


def _create_mcp_token(user_id: str, credential_epoch: int) -> str:
    """Create a valid MCP access token accepted by PASAuthProvider.load_access_token()."""
    private_key, _ = _load_keys()
    config = get_config()
    now = int(time.time())
    return jose_jwt.encode(
        {
            "iss": config.server.public_base_url,
            "sub": user_id,
            "aud": f"{config.server.public_base_url}/mcp",
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 3600,
            "type": "access",
            "credential_epoch": credential_epoch,
            "client_id": "test-client",
            "scope": "",
        },
        private_key,
        algorithm="RS256",
    )


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    reset_mcp()
    set_polardb_client(MockPolarDBClient())
    reset_rate_limiters()
    yield
    reset_keys()
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()
    reset_polardb_client()
    reset_rate_limiters()


@pytest.fixture
def encryption_key():
    key = os.urandom(32)
    key_b64 = base64.b64encode(key).decode()
    os.environ["PAS_ENCRYPTION_KEY"] = key_b64
    yield key
    del os.environ["PAS_ENCRYPTION_KEY"]


@pytest.fixture
async def test_engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield e
    finally:
        await e.dispose()


@pytest.fixture
async def setup_data(test_engine, encryption_key):
    engine_mod._engine = test_engine
    engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async with engine_mod._session_factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.ADMIN,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)

        instance = Instance(
            cluster_id="pc-test-001",
            name="Test Instance",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            host="127.0.0.1",
            port=3306,
            status=InstanceStatus.ACTIVE,
        )
        session.add(instance)
        await session.commit()
        await session.refresh(instance)

        encrypted_pw = encrypt("test_password", key=encryption_key)
        credential = InstanceCredential(
            instance_id=instance.id,
            name="pas_admin", purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("pas_admin", key=encryption_key),
            password_ciphertext=encrypted_pw, created_by_user_id=admin.id,
        )
        session.add(credential)
        await session.commit()
        await session.refresh(credential)

        binding = UserInstanceBinding(
            user_id=admin.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
        )
        session.add(binding)
        agent = Agent(
            name="workspace-agent",
            created_by=admin.id,
        )
        session.add(agent)
        await session.flush()
        agent_binding = AgentInstanceBinding(
            agent_id=agent.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
            created_by_user_id=admin.id,
        )
        agent_binding.capabilities = [
            AgentInstanceBindingCapability(
                capability=BindingCapability.SQL_READ
            ),
            AgentInstanceBindingCapability(
                capability=BindingCapability.SQL_WRITE
            ),
        ]
        session.add_all(
            [
                agent_binding,
                AgentUserAssignment(
                    agent_id=agent.id,
                    user_id=admin.id,
                    created_by_user_id=admin.id,
                    is_direct=True,
                ),
            ]
        )
        await session.commit()

        return {"admin": admin, "agent": agent, "instance": instance}


@pytest.fixture
async def client(setup_data):
    app = create_app()
    async with mcp_lifespan():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def auth_headers(setup_data):
    token = _create_mcp_token(
        f"user:{setup_data['admin'].id}",
        setup_data["admin"].credential_epoch,
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def rest_auth_headers(setup_data):
    """Auth headers for legacy REST endpoints (standard JWT without audience)."""
    token = create_access_token({"sub": setup_data["admin"].id, "role": "admin", "credential_epoch": setup_data["admin"].credential_epoch})
    return {"Authorization": f"Bearer {token}"}


MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _parse_sse_response(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def test_personal_mcp_uses_user_grants_without_agent(client, setup_data, auth_headers):
    from server.core import admin_binding_service, personal_token_service
    get_config().server.public_base_url = "http://localhost:18760"

    factory = engine_mod.get_session_factory()
    async with factory() as session:
        user = User(external_id="personal-no-agent", display_name="Personal User")
        session.add(user)
        await session.flush()
        credential = await session.scalar(select(InstanceCredential).where(
            InstanceCredential.instance_id == setup_data["instance"].id))
        binding, _ = await admin_binding_service.update_user_instance_access(
            session, user_id=user.id, instance_id=credential.instance_id,
            credential_id=credential.id, permission=Permission.READONLY,
            capabilities={BindingCapability.SQL_READ}, enabled=True)
        row, token = await personal_token_service.issue(session, user.id,
            resource=get_config().server.public_base_url.rstrip("/") + "/mcp/personal")
        await session.commit()
        user_id, binding_id = user.id, binding.id

    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    metadata = await client.get("/.well-known/oauth-protected-resource/mcp/personal")
    assert metadata.status_code == 200
    assert metadata.json()["resource"].endswith("/mcp/personal")
    assert (await client.post("/mcp", headers=headers, json=_jsonrpc("tools/list"))).status_code == 401
    assert (await client.post("/mcp/personal", headers={**MCP_HEADERS, **auth_headers},
                              json=_jsonrpc("tools/list"))).status_code == 401
    unauthenticated = await client.post("/mcp/personal", headers=MCP_HEADERS,
                                        json=_jsonrpc("tools/list"))
    assert unauthenticated.status_code == 401
    assert "/oauth-protected-resource/mcp/personal" in unauthenticated.headers["www-authenticate"]
    response = await client.post("/mcp/personal", headers=headers, json=_jsonrpc("tools/list"))
    assert response.status_code == 200
    names = {item["name"] for item in _parse_sse_response(response.text)[0]["result"]["tools"]}
    assert {"run_sql", "list_db_instances", "describe_schema"} <= names
    assert "create_db_instance" not in names
    assert not names.intersection({"create_branch", "delete_branch", "set_default_instance", "doc_delete", "doc_rechunk"})
    tools = _parse_sse_response(response.text)[0]["result"]["tools"]
    sql_schema = next(item["inputSchema"] for item in tools if item["name"] == "run_sql")
    assert "instance_id" in sql_schema["required"]
    assert "branch" not in sql_schema["properties"]
    for tool, arguments in [("create_branch", {}), ("doc_delete", {}), ("run_sql", {"sql": "SELECT 1"}), ("run_sql", {"sql": "SELECT 1", "instance_id": setup_data["instance"].id, "branch": "main"})]:
        denied = await client.post("/mcp/personal", headers=headers, json=_jsonrpc("tools/call", {"name": tool, "arguments": arguments}))
        assert _parse_sse_response(denied.text)[0]["result"]["isError"]

    response = await client.post("/mcp/personal", headers=headers,
        json=_jsonrpc("tools/call", {"name": "list_db_instances", "arguments": {}}))
    result = _parse_sse_response(response.text)[0]["result"]
    payload = json.loads(result["content"][0]["text"])
    assert setup_data["instance"].id in json.dumps(payload)
    assert "password" not in json.dumps(payload)

    # Dispatch SQL as the actual user, never the fixture's assigned Agent.
    with patch("server.mcp.transport.handle_run_sql", new_callable=AsyncMock,
               return_value={"content": [{"type": "text", "text": "ok"}]}) as run:
        response = await client.post("/mcp/personal", headers=headers,
            json=_jsonrpc("tools/call", {"name": "run_sql", "arguments": {
                "instance_id": setup_data["instance"].id, "sql": "SELECT 1"}}))
        assert response.status_code == 200
        assert isinstance(run.call_args.args[0], User)
        assert run.call_args.args[0].id == user_id

    async with factory() as session:
        binding = await session.get(UserInstanceBinding, binding_id)
        binding.enabled = False
        await session.commit()
    response = await client.post("/mcp/personal", headers=headers, json=_jsonrpc("tools/list"))
    names = {item["name"] for item in _parse_sse_response(response.text)[0]["result"]["tools"]}
    assert "run_sql" not in names
    assert "list_db_instances" not in names


async def test_mcp_app_starts_with_http_vpc_external_base_url(
    setup_data,
) -> None:
    from server.mcp.transport import _build_mcp_server

    get_config().server.public_base_url = "http://10.0.0.8:18760"

    mcp = _build_mcp_server()
    assert mcp.streamable_http_app() is not None


def _schema_type_signature(schema: dict) -> str:
    if "anyOf" in schema:
        return "anyOf(" + ",".join(sorted(_schema_type_signature(item) for item in schema["anyOf"])) + ")"
    if schema.get("type") == "array":
        return f"array[{_schema_type_signature(schema.get('items', {}))}]"
    return str(schema.get("type"))


def _mcp_session_id(resp) -> str | None:
    value = resp.headers.get("mcp-session-id")
    return value if isinstance(value, str) else None


class TestMCPTransportAuth:
    async def test_no_token_returns_401(self, client):
        resp = await client.post(
            "/mcp",
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            ),
            headers=MCP_HEADERS,
        )
        assert resp.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        headers = {**MCP_HEADERS, "Authorization": "Bearer invalid-token"}
        resp = await client.post(
            "/mcp",
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            ),
            headers=headers,
        )
        assert resp.status_code == 401

    async def test_web_session_token_returns_401(self, client, setup_data):
        token = create_access_token(
            {
                "sub": setup_data["admin"].id,
            "role": "admin",
        })
        headers = {
            **MCP_HEADERS,
            "Authorization": f"Bearer {token}",
        }
        resp = await client.post(
            "/mcp",
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            ),
            headers=headers,
        )
        assert resp.status_code == 401


class TestMCPTransportInitialize:
    async def test_initialize(self, client, auth_headers):
        headers = {**MCP_HEADERS, **auth_headers}
        resp = await client.post(
            "/mcp",
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            ),
            headers=headers,
        )
        assert resp.status_code == 200

        events = _parse_sse_response(resp.text)
        assert len(events) >= 1
        result = events[0].get("result", {})
        assert result.get("protocolVersion") == "2025-03-26"
        assert "tools" in result.get("capabilities", {})
        assert result.get("serverInfo", {}).get("name") == "alibabacloud polardb tool agentic server"


class TestMCPTransportTools:
    async def _initialize(self, client, auth_headers) -> str | None:
        headers = {**MCP_HEADERS, **auth_headers}
        resp = await client.post(
            "/mcp",
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            ),
            headers=headers,
        )
        return _mcp_session_id(resp)

    async def test_tools_list(self, client, auth_headers):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        resp = await client.post(
            "/mcp",
            json=_jsonrpc("tools/list", req_id=2),
            headers=headers,
        )
        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        assert len(events) >= 1
        tools = events[0].get("result", {}).get("tools", [])
        names = [t["name"] for t in tools]
        assert "run_sql" in names
        assert "list_instances" not in names
        assert not {
            "list_db_instances",
            "create_db_instance",
            "describe_db_instance",
            "delete_db_instance",
        } & set(names)
        assert "set_default_instance" not in names
        assert "run_sql_transaction" in names
        assert "list_branches" not in names
        assert "create_branch" not in names
        assert "delete_branch" not in names
        assert not {
            "prepare_document_upload",
            "complete_document_upload",
        } & set(names)

    async def test_polarrag_rate_limit_returns_429_and_retry_after(
        self,
        client,
        auth_headers,
    ):
        limits = get_config().polarrag_tool_limits
        limits.user_requests_per_minute = 1
        limits.user_burst = 1
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id
        request = _jsonrpc(
            "tools/call",
            {
                "name": "list_knowledge_resources",
                "arguments": {},
            },
            req_id=20,
        )

        first = await client.post("/mcp", json=request, headers=headers)
        request["id"] = 21
        rejected = await client.post("/mcp", json=request, headers=headers)

        assert first.status_code == 200
        assert rejected.status_code == 429
        assert rejected.headers["retry-after"] == "60"
        events = _parse_sse_response(rejected.text)
        payload = json.loads(events[0]["result"]["content"][0]["text"])
        assert payload == {
            "error": "POLARRAG_TOOL_LIMITED",
            "message": ("PolarRAG Tool capacity is temporarily unavailable."),
            "reason": "RATE_LIMIT",
            "retry_after_seconds": 60,
        }
        async with engine_mod._session_factory() as session:
            audit = (
                (
                    await session.execute(
                        select(AuditLog)
                        .where(
                            AuditLog.action == "polarrag.list_knowledge_resources",
                            AuditLog.status == AuditStatus.ERROR,
                        )
                        .order_by(AuditLog.created_at.desc())
                    )
                )
                .scalars()
                .first()
            )
        assert audit is not None
        assert audit.error_code == "POLARRAG_TOOL_LIMITED"
        audit_metadata = json.loads(audit.metadata_json or "{}")
        assert json.loads(audit_metadata["client_info"]) == {
            "polarrag_status": "POLARRAG_TOOL_LIMITED",
            "governance_reason": "RATE_LIMIT",
            "retry_after_seconds": 60,
        }

    async def test_user_agent_token_lists_and_calls_upload_tools(
        self,
        client,
        setup_data,
    ):
        async with engine_mod._session_factory() as session:
            member = User(
                external_id="upload-member",
                display_name="Upload Member",
                auth_provider=AuthProvider.BUILTIN,
            )
            agent = Agent(name="upload-agent")
            session.add_all([member, agent])
            await session.flush()
            instance = PolarRAGInstance(
                name="upload-rag",
                scheme="http",
                host="rag.example.test",
                port=9200,
                username_ciphertext=encrypt("service-user"),
                password_ciphertext=encrypt("service-password"),
                status=PolarRAGInstanceStatus.ACTIVE,
                created_by=setup_data["admin"].id,
            )
            session.add(instance)
            await session.flush()
            assignment = AgentUserAssignment(
                agent_id=agent.id,
                user_id=member.id,
                created_by_user_id=setup_data["admin"].id,
            )
            session.add_all(
                [
                    AgentPolarRAGInstanceBinding(
                        agent_id=agent.id,
                        polarrag_instance_id=instance.id,
                        created_by_user_id=setup_data["admin"].id,
                    ),
                    assignment,
                ]
            )
            await session.flush()
            _row, plaintext = await issue_token(session, assignment.id)
            await session.commit()

        auth_headers = {"Authorization": f"Bearer {plaintext}"}
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id
        listed = await client.post(
            "/mcp",
            json=_jsonrpc("tools/list", req_id=92),
            headers=headers,
        )
        names = {
            tool["name"]
            for tool in _parse_sse_response(listed.text)[0]["result"]["tools"]
        }
        from server.mcp.tools.polarrag import POLARRAG_TOOL_NAMES

        assert names == POLARRAG_TOOL_NAMES

        called = await client.post(
            "/mcp",
            json=_jsonrpc(
                "tools/call",
                {
                    "name": "complete_document_upload",
                    "arguments": {"upload_session_id": str(uuid.uuid4())},
                },
                req_id=93,
            ),
            headers=headers,
        )
        result = _parse_sse_response(called.text)[0]["result"]
        assert result["isError"] is True
        assert json.loads(result["content"][0]["text"])["error"] == (
            "UPLOAD_SESSION_NOT_ACCESSIBLE"
        )

    async def test_workspace_sql_schema_requires_agent_instance(
        self,
        client,
        auth_headers,
    ):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        resp = await client.post(
            "/mcp",
            json=_jsonrpc("tools/list", req_id=2),
            headers=headers,
        )
        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        tools = events[0].get("result", {}).get("tools", [])
        by_name = {tool["name"]: tool for tool in tools}
        run_sql = by_name["run_sql"]
        assert "instance_id" in run_sql["inputSchema"]["required"]
        assert "branch" not in run_sql["inputSchema"]["properties"]
        assert not {
            "set_default_instance",
            "list_branches",
            "create_branch",
            "delete_branch",
        } & set(by_name)

    async def test_branch_tool_schema_matches_rest_contract(
        self, client, auth_headers, rest_auth_headers
    ):
        session_id = await self._initialize(client, auth_headers)
        mcp_headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            mcp_headers["mcp-session-id"] = session_id

        mcp_resp = await client.post(
            "/mcp",
            json=_jsonrpc("tools/list", req_id=2),
            headers=mcp_headers,
        )
        rest_resp = await client.get("/mcp/rest/tools", headers=rest_auth_headers)

        assert mcp_resp.status_code == 200
        assert rest_resp.status_code == 200
        mcp_tools = _parse_sse_response(mcp_resp.text)[0].get("result", {}).get("tools", [])
        rest_tools = rest_resp.json()["tools"]

        mcp_branch_tools = {t["name"]: t for t in mcp_tools if "branch" in t["name"]}
        rest_branch_tools = {t["name"]: t for t in rest_tools if "branch" in t["name"]}
        assert mcp_branch_tools == {}
        assert set(rest_branch_tools) == {
            "list_branches",
            "create_branch",
            "delete_branch",
        }

    async def test_run_sql_branch_schema_matches_rest_contract(
        self, client, auth_headers, rest_auth_headers
    ):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        resp = await client.post(
            "/mcp",
            json=_jsonrpc("tools/list", req_id=2),
            headers=headers,
        )
        rest_resp = await client.get("/mcp/rest/tools", headers=rest_auth_headers)
        assert resp.status_code == 200
        assert rest_resp.status_code == 200
        events = _parse_sse_response(resp.text)
        tools = events[0].get("result", {}).get("tools", [])
        run_sql = next(t for t in tools if t["name"] == "run_sql")
        rest_tools = rest_resp.json()["tools"]
        rest_run_sql = next(t for t in rest_tools if t["name"] == "run_sql")
        assert "branch" not in run_sql["inputSchema"]["properties"]
        assert "instance_id" in run_sql["inputSchema"]["required"]
        assert "branch" in rest_run_sql["inputSchema"]["properties"]

        run_sql_transaction = next(t for t in tools if t["name"] == "run_sql_transaction")
        rest_transaction = next(t for t in rest_tools if t["name"] == "run_sql_transaction")
        assert "branch" not in run_sql_transaction["inputSchema"]["properties"]
        assert "branch" not in rest_transaction["inputSchema"]["properties"]

    async def test_branch_tool_calls_dispatch(self, client, auth_headers, setup_data):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        async def call_tool(name: str, arguments: dict, req_id: int) -> dict:
            resp = await client.post(
                "/mcp",
                json=_jsonrpc(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                    },
                    req_id=req_id,
                ),
                headers=headers,
            )
            assert resp.status_code == 200
            events = _parse_sse_response(resp.text)
            result = events[0].get("result", {})
            content = result.get("content", [])
            assert len(content) > 0
            return cast(dict, json.loads(content[0]["text"]))

        list_result = {"content": [{"type": "text", "text": json.dumps({"branches": []})}]}
        create_result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "branch_name": "br_new",
                            "status": "created",
                        }
                    ),
                }
            ]
        }
        delete_result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "branch_name": "br_old",
                            "status": "deleted",
                        }
                    ),
                }
            ]
        }

        with patch(
            "server.mcp.transport.handle_list_branches", new_callable=AsyncMock, return_value=list_result
        ) as list_branches:
            payload = await call_tool(
                "list_branches",
                {"instance_id": setup_data["instance"].id},
                3,
            )
        assert payload == {"branches": []}
        assert list_branches.await_args.kwargs["instance_id"] == setup_data["instance"].id

        with patch("server.mcp.transport.handle_create_branch",
                   new_callable=AsyncMock, return_value=create_result) as create_branch:
            payload = await call_tool(
                "create_branch",
                {
                    "instance_id": setup_data["instance"].id,
                    "branch_name": "br_new",
                    "include_databases": ["db1"],
                },
                4,
            )
        assert payload == {"branch_name": "br_new", "status": "created"}
        assert create_branch.await_args.kwargs["instance_id"] == setup_data["instance"].id
        assert create_branch.await_args.kwargs["branch_name"] == "br_new"
        assert create_branch.await_args.kwargs["include_databases"] == ["db1"]

        with patch("server.mcp.transport.handle_delete_branch",
                   new_callable=AsyncMock, return_value=delete_result) as delete_branch:
            payload = await call_tool(
                "delete_branch",
                {
                    "instance_id": setup_data["instance"].id,
                    "branch_name": "br_old",
                },
                5,
            )
        assert payload == {"branch_name": "br_old", "status": "deleted"}
        assert delete_branch.await_args.kwargs["instance_id"] == setup_data["instance"].id
        assert delete_branch.await_args.kwargs["branch_name"] == "br_old"

    async def test_branch_tool_calls_allow_omitted_instance_id(self, client, auth_headers):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        async def call_tool(name: str, arguments: dict, req_id: int) -> dict:
            resp = await client.post(
                "/mcp",
                json=_jsonrpc(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                    },
                    req_id=req_id,
                ),
                headers=headers,
            )
            assert resp.status_code == 200
            events = _parse_sse_response(resp.text)
            result = events[0].get("result", {})
            content = result.get("content", [])
            assert len(content) > 0
            return cast(dict, json.loads(content[0]["text"]))

        list_result = {"content": [{"type": "text", "text": json.dumps({"branches": []})}]}
        create_result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "branch_name": "br_new",
                            "status": "created",
                        }
                    ),
                }
            ]
        }
        delete_result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "branch_name": "br_old",
                            "status": "deleted",
                        }
                    ),
                }
            ]
        }

        with patch(
            "server.mcp.transport.handle_list_branches", new_callable=AsyncMock, return_value=list_result
        ) as list_branches:
            payload = await call_tool("list_branches", {}, 6)
        assert payload == {"branches": []}
        assert list_branches.await_args.kwargs["instance_id"] is None

        with patch("server.mcp.transport.handle_create_branch",
                   new_callable=AsyncMock, return_value=create_result) as create_branch:
            payload = await call_tool("create_branch", {"branch_name": "br_new"}, 7)
        assert payload == {"branch_name": "br_new", "status": "created"}
        assert create_branch.await_args.kwargs["instance_id"] is None
        assert create_branch.await_args.kwargs["include_databases"] is None

        with patch("server.mcp.transport.handle_delete_branch",
                   new_callable=AsyncMock, return_value=delete_result) as delete_branch:
            payload = await call_tool("delete_branch", {"branch_name": "br_old"}, 8)
        assert payload == {"branch_name": "br_old", "status": "deleted"}
        assert delete_branch.await_args.kwargs["instance_id"] is None

    async def test_branch_tool_calls_allow_explicit_null_optional_args(self, client, auth_headers):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        async def call_tool(name: str, arguments: dict, req_id: int) -> dict:
            resp = await client.post(
                "/mcp",
                json=_jsonrpc(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                    },
                    req_id=req_id,
                ),
                headers=headers,
            )
            assert resp.status_code == 200
            events = _parse_sse_response(resp.text)
            result = events[0].get("result", {})
            content = result.get("content", [])
            assert len(content) > 0
            return cast(dict, json.loads(content[0]["text"]))

        list_result = {"content": [{"type": "text", "text": json.dumps({"branches": []})}]}
        create_result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "branch_name": "br_new",
                            "status": "created",
                        }
                    ),
                }
            ]
        }
        delete_result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "branch_name": "br_old",
                            "status": "deleted",
                        }
                    ),
                }
            ]
        }

        with patch(
            "server.mcp.transport.handle_list_branches", new_callable=AsyncMock, return_value=list_result
        ) as list_branches:
            payload = await call_tool("list_branches", {"instance_id": None}, 6)
        assert payload == {"branches": []}
        assert list_branches.await_args.kwargs["instance_id"] is None

        with patch("server.mcp.transport.handle_create_branch",
                   new_callable=AsyncMock, return_value=create_result) as create_branch:
            payload = await call_tool(
                "create_branch",
                {
                    "branch_name": "br_new",
                    "instance_id": None,
                    "include_databases": None,
                },
                7,
            )
        assert payload == {"branch_name": "br_new", "status": "created"}
        assert create_branch.await_args.kwargs["instance_id"] is None
        assert create_branch.await_args.kwargs["include_databases"] is None

        with patch("server.mcp.transport.handle_delete_branch",
                   new_callable=AsyncMock, return_value=delete_result) as delete_branch:
            payload = await call_tool(
                "delete_branch",
                {"branch_name": "br_old", "instance_id": None},
                8,
            )
        assert payload == {"branch_name": "br_old", "status": "deleted"}
        assert delete_branch.await_args.kwargs["instance_id"] is None

    async def test_branch_tool_calls_reject_unsupported_arguments(self, client, auth_headers):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        async def call_tool(name: str, arguments: dict, req_id: int) -> dict:
            resp = await client.post(
                "/mcp",
                json=_jsonrpc(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                    },
                    req_id=req_id,
                ),
                headers=headers,
            )
            assert resp.status_code == 200
            events = _parse_sse_response(resp.text)
            assert len(events) >= 1
            return cast(dict, events[0])

        cases = [
            (
                "list_branches",
                "handle_list_branches",
                {"project_id": "unsupported"},
            ),
            (
                "list_branches",
                "handle_list_branches",
                {"branch_id": "unsupported"},
            ),
            (
                "create_branch",
                "handle_create_branch",
                {"branch_name": "br_new", "parent_branch_name": "unsupported"},
            ),
            (
                "create_branch",
                "handle_create_branch",
                {"branch_name": "br_new", "branch_id": "unsupported"},
            ),
            (
                "delete_branch",
                "handle_delete_branch",
                {"branch_name": "br_old", "confirm": True},
            ),
            (
                "delete_branch",
                "handle_delete_branch",
                {"branch_name": "br_old", "branch_id": "unsupported"},
            ),
        ]

        for index, (name, handler_name, arguments) in enumerate(cases, start=20):
            with patch(f"server.mcp.transport.{handler_name}", new_callable=AsyncMock) as handler:
                event = await call_tool(name, arguments, index)
            handler.assert_not_awaited()
            assert "error" in event or event.get("result", {}).get("isError") is True

    async def test_branch_tool_calls_reject_invalid_argument_types(self, client, auth_headers):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        async def call_tool(name: str, arguments: dict, req_id: int) -> dict:
            resp = await client.post(
                "/mcp",
                json=_jsonrpc(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                    },
                    req_id=req_id,
                ),
                headers=headers,
            )
            assert resp.status_code == 200
            events = _parse_sse_response(resp.text)
            assert len(events) >= 1
            return cast(dict, events[0])

        cases = [
            (
                "list_branches",
                "handle_list_branches",
                {"instance_id": 123},
            ),
            (
                "create_branch",
                "handle_create_branch",
                {"branch_name": 123},
            ),
            (
                "create_branch",
                "handle_create_branch",
                {"branch_name": ""},
            ),
            (
                "create_branch",
                "handle_create_branch",
                {"branch_name": "br_new", "include_databases": "db1"},
            ),
            (
                "create_branch",
                "handle_create_branch",
                {"branch_name": "br_new", "include_databases": [123]},
            ),
            (
                "create_branch",
                "handle_create_branch",
                {"branch_name": "br_new", "include_databases": [""]},
            ),
            (
                "delete_branch",
                "handle_delete_branch",
                {"branch_name": 123},
            ),
            (
                "delete_branch",
                "handle_delete_branch",
                {"branch_name": ""},
            ),
        ]

        for index, (name, handler_name, arguments) in enumerate(cases, start=40):
            with patch(f"server.mcp.transport.{handler_name}", new_callable=AsyncMock) as handler:
                event = await call_tool(name, arguments, index)
            handler.assert_not_awaited()
            assert "error" in event or event.get("result", {}).get("isError") is True

    async def test_branch_tool_call_output_matches_rest_output(
        self, client, auth_headers, rest_auth_headers, setup_data
    ):
        session_id = await self._initialize(client, auth_headers)
        mcp_headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            mcp_headers["mcp-session-id"] = session_id

        async def call_mcp_tool(name: str, arguments: dict, req_id: int) -> dict:
            resp = await client.post(
                "/mcp",
                json=_jsonrpc(
                    "tools/call",
                    {
                        "name": name,
                        "arguments": arguments,
                    },
                    req_id=req_id,
                ),
                headers=mcp_headers,
            )
            assert resp.status_code == 200
            result = _parse_sse_response(resp.text)[0].get("result", {})
            return {
                "isError": bool(result.get("isError", False)),
                "payload": json.loads(result["content"][0]["text"]),
            }

        async def call_rest_tool(path: str, arguments: dict) -> dict:
            resp = await client.post(path, json=arguments, headers=rest_auth_headers)
            assert resp.status_code == 200
            result = resp.json()
            return {
                "isError": bool(result.get("isError", False)),
                "payload": json.loads(result["content"][0]["text"]),
            }

        tool_cases = [
            (
                "list_branches",
                "handle_list_branches",
                "/mcp/rest/list_branches",
                {"instance_id": setup_data["instance"].id},
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "branches": [{"branch_name": "MAIN"}],
                                }
                            ),
                        }
                    ]
                },
            ),
            (
                "create_branch",
                "handle_create_branch",
                "/mcp/rest/create_branch",
                {
                    "instance_id": setup_data["instance"].id,
                    "branch_name": "br_new",
                    "include_databases": ["db1"],
                },
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "branch_name": "br_new",
                                    "status": "created",
                                }
                            ),
                        }
                    ]
                },
            ),
            (
                "delete_branch",
                "handle_delete_branch",
                "/mcp/rest/delete_branch",
                {
                    "instance_id": setup_data["instance"].id,
                    "branch_name": "br_old",
                },
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "branch_name": "br_old",
                                    "status": "deleted",
                                }
                            ),
                        }
                    ]
                },
            ),
            (
                "create_branch",
                "handle_create_branch",
                "/mcp/rest/create_branch",
                {
                    "instance_id": setup_data["instance"].id,
                    "branch_name": "bad;name",
                },
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": "INVALID_IDENTIFIER",
                                    "message": "branch_name contains forbidden characters.",
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
            ),
        ]

        for index, (
            name,
            handler_name,
            rest_path,
            arguments,
            handler_result,
        ) in enumerate(tool_cases, start=10):
            transport_patch = f"server.mcp.transport.{handler_name}"
            server_patch = f"server.mcp.server.{handler_name}"
            with (
                patch(transport_patch, new_callable=AsyncMock, return_value=handler_result),
                patch(server_patch, new_callable=AsyncMock, return_value=handler_result),
            ):
                assert await call_mcp_tool(name, arguments, index) == await call_rest_tool(
                    rest_path,
                    arguments,
                )

    async def test_branch_tool_call_error_sets_mcp_is_error(self, client, auth_headers):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        resp = await client.post(
            "/mcp",
            json=_jsonrpc(
                "tools/call",
                {
                    "name": "create_branch",
                    "arguments": {
                        "branch_name": "bad;name",
                    },
                },
                req_id=6,
            ),
            headers=headers,
        )

        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        result = events[0].get("result", {})
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "INVALID_IDENTIFIER"

    async def test_run_sql_branch_tool_call_error_sets_mcp_is_error(self, client, auth_headers, setup_data):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        resp = await client.post(
            "/mcp",
            json=_jsonrpc("tools/call", {
                "name": "run_sql",
                    "arguments": {
                        "sql": "DROP DATABASE app",
                        "instance_id": setup_data["instance"].id,
                        "branch": "br1",
                        "confirm": True,
                    },
                },
                req_id=7,
            ),
            headers=headers,
        )

        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        result = events[0].get("result", {})
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "INVALID_ARGUMENT"

    async def test_run_sql_without_branch_preserves_handler_error(
        self,
        client,
        auth_headers,
        setup_data,
    ):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        resp = await client.post(
            "/mcp",
            json=_jsonrpc("tools/call",
                {
                    "name": "run_sql",
                    "arguments": {
                        "sql": "DROP DATABASE app",
                        "instance_id": setup_data["instance"].id,
                        "confirm": True,
                    },
                },
                req_id=8,
            ),
            headers=headers,
        )

        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        result = events[0].get("result", {})
        assert result.get("isError") is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "BLOCKED_SQL"

    @pytest.mark.parametrize(
        ("handler_result", "expected_status"),
        [
            (
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "rows": [["SENSITIVE_RESULT_SENTINEL"]],
                                    "row_count": 1,
                                    "truncated": False,
                                }
                            ),
                        }
                    ]
                },
                "success",
            ),
            (
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": "SQL_ERROR",
                                    "message": "SENSITIVE_ERROR_SENTINEL",
                                }
                            ),
                        }
                    ],
                    "isError": True,
                },
                "error",
            ),
        ],
    )
    async def test_run_sql_logs_only_safe_structured_metadata(
        self,
        client,
        auth_headers,
        setup_data,
        caplog,
        monkeypatch,
        handler_result,
        expected_status,
    ):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id
        capture_logger = logging.getLogger(
            "test.capture.server.mcp.transport"
        )
        capture_logger.handlers.clear()
        capture_logger.propagate = True
        monkeypatch.setattr(
            "server.mcp.transport.logger", capture_logger
        )
        caplog.set_level(logging.INFO, logger=capture_logger.name)

        with patch(
            "server.mcp.transport.handle_run_sql",
            new_callable=AsyncMock,
            return_value=handler_result,
        ):
            response = await client.post(
                "/mcp",
                json=_jsonrpc(
                    "tools/call",
                    {
                        "name": "run_sql",
                        "arguments": {
                            "sql": "SELECT 'SENSITIVE_SQL_SENTINEL'",
                            "instance_id": setup_data["instance"].id,
                            "database": "SENSITIVE_DATABASE_SENTINEL",
                            "max_rows": 7,
                            "confirm": True,
                        },
                    },
                    req_id=91,
                ),
                headers=headers,
            )

        assert response.status_code == 200
        assert "SENSITIVE_SQL_SENTINEL" not in caplog.text
        assert "SENSITIVE_RESULT_SENTINEL" not in caplog.text
        assert "SENSITIVE_ERROR_SENTINEL" not in caplog.text
        assert "SENSITIVE_DATABASE_SENTINEL" not in caplog.text
        records = [
            record
            for record in caplog.records
            if record.name == capture_logger.name
            and record.getMessage().startswith("tool.run_sql")
        ]
        assert records
        completed = records[-1]
        assert completed.actor_kind == "user"
        assert completed.actor_id == setup_data["admin"].id
        assert completed.instance_id == setup_data["instance"].id
        assert completed.statement_count == 1
        assert completed.confirm is True
        assert completed.tool_status == expected_status

    @pytest.mark.parametrize(
        ("tool_name", "handler_name", "arguments", "handler_result"),
        [
            (
                "describe_schema",
                "handle_describe_schema",
                {
                    "database": "SENSITIVE_DATABASE_SENTINEL",
                    "table_pattern": "SENSITIVE_PATTERN_SENTINEL",
                },
                {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "tables": [{
                                "table_name": "SENSITIVE_TABLE_SENTINEL",
                            }],
                            "has_more": False,
                        }),
                    }],
                },
            ),
            (
                "list_branches",
                "handle_list_branches",
                {},
                {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "branches": [{
                                "branch_name": "SENSITIVE_BRANCH_SENTINEL",
                            }],
                        }),
                    }],
                },
            ),
            (
                "create_branch",
                "handle_create_branch",
                {
                    "branch_name": "SENSITIVE_CREATE_BRANCH",
                    "include_databases": ["SENSITIVE_INCLUDED_DATABASE"],
                },
                {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "branch_name": "SENSITIVE_CREATE_BRANCH",
                            "status": "created",
                        }),
                    }],
                },
            ),
            (
                "delete_branch",
                "handle_delete_branch",
                {"branch_name": "SENSITIVE_DELETE_BRANCH"},
                {
                    "content": [{
                        "type": "text",
                        "text": json.dumps({
                            "branch_name": "SENSITIVE_DELETE_BRANCH",
                            "status": "deleted",
                        }),
                    }],
                },
            ),
        ],
    )
    async def test_schema_and_branch_transport_logs_only_allowlisted_metadata(
        self,
        client,
        auth_headers,
        setup_data,
        caplog,
        monkeypatch,
        tool_name,
        handler_name,
        arguments,
        handler_result,
    ):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id
        capture_logger = logging.getLogger(
            "test.capture.server.mcp.transport.database-tools"
        )
        capture_logger.handlers.clear()
        capture_logger.propagate = True
        monkeypatch.setattr(
            "server.mcp.transport.logger", capture_logger
        )
        caplog.set_level(logging.INFO, logger=capture_logger.name)

        with patch(
            f"server.mcp.transport.{handler_name}",
            new_callable=AsyncMock,
            return_value=handler_result,
        ):
            response = await client.post(
                "/mcp",
                json=_jsonrpc(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments},
                    req_id=92,
                ),
                headers=headers,
            )

        assert response.status_code == 200
        # Tool output remains unchanged; only ordinary logs are sanitized.
        assert any(
            sentinel in response.text
            for sentinel in (
                "SENSITIVE_TABLE_SENTINEL",
                "SENSITIVE_BRANCH_SENTINEL",
                "SENSITIVE_CREATE_BRANCH",
                "SENSITIVE_DELETE_BRANCH",
            )
        )
        for sentinel in (
            "SENSITIVE_DATABASE_SENTINEL",
            "SENSITIVE_PATTERN_SENTINEL",
            "SENSITIVE_TABLE_SENTINEL",
            "SENSITIVE_BRANCH_SENTINEL",
            "SENSITIVE_CREATE_BRANCH",
            "SENSITIVE_INCLUDED_DATABASE",
            "SENSITIVE_DELETE_BRANCH",
        ):
            assert sentinel not in caplog.text
        records = [
            record
            for record in caplog.records
            if record.name == capture_logger.name
            and record.getMessage().startswith(f"tool.{tool_name}")
        ]
        assert len(records) == 1
        assert records[0].actor_kind == "user"
        assert records[0].actor_id == setup_data["admin"].id
        assert records[0].tool_status == "success"
        assert isinstance(records[0].duration_ms, int)

    async def test_removed_list_instances_tool_is_unknown(
        self, client, auth_headers
    ):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        resp = await client.post(
            "/mcp",
            json=_jsonrpc(
                "tools/call",
                {
                    "name": "list_instances",
                    "arguments": {},
                },
                req_id=3,
            ),
            headers=headers,
        )
        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        assert len(events) >= 1
        result = events[0].get("result", {})
        assert result.get("isError") is True
        content = result.get("content", [])
        assert len(content) > 0
        assert content[0]["text"] == "Unknown tool: list_instances"


class TestLegacyRESTEndpoints:
    async def test_rest_list_instances_is_removed(
        self, client, rest_auth_headers
    ):
        resp = await client.get("/mcp/rest/list_instances", headers=rest_auth_headers)
        assert resp.status_code == 404

    async def test_rest_tools_endpoint(self, client):
        resp = await client.get("/mcp/rest/tools")
        assert resp.status_code == 200
        tools = resp.json()["tools"]
        assert len(tools) == 7
        assert "list_instances" not in {
            tool["name"] for tool in tools
        }


class TestMCPTransportRunSQLTransaction:
    async def _initialize(self, client, auth_headers) -> str | None:
        headers = {**MCP_HEADERS, **auth_headers}
        resp = await client.post(
            "/mcp",
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1.0"},
                },
            ),
            headers=headers,
        )
        return _mcp_session_id(resp)

    async def test_run_sql_transaction_in_tools_list(self, client, auth_headers):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        resp = await client.post(
            "/mcp",
            json=_jsonrpc("tools/list", req_id=2),
            headers=headers,
        )
        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        tools = events[0].get("result", {}).get("tools", [])
        names = [t["name"] for t in tools]
        assert "run_sql_transaction" in names

    async def test_run_sql_transaction_call(self, client, auth_headers):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        mock_result = {
            "content": [{"type": "text", "text": json.dumps(
                        {
                            "results": [{"columns": [], "rows": [], "row_count": 0, "truncated": False}],
                            "statement_count": 1,
                        }
                    ),
                }
            ],
        }
        with patch("server.mcp.transport.handle_run_sql_transaction", new_callable=AsyncMock, return_value=mock_result):
            resp = await client.post(
                "/mcp",
                json=_jsonrpc(
                    "tools/call",
                    {
                        "name": "run_sql_transaction",
                        "arguments": {
                            "sql_statements": ["INSERT INTO t VALUES (1)"],
                        },
                    },
                    req_id=3,
                ),
                headers=headers,
            )
        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        result = events[0].get("result", {})
        content = result.get("content", [])
        assert len(content) > 0


async def test_personal_oauth_metadata_requires_configured_external_url(client):
    get_config().server.public_base_url = ""
    response = await client.get("/.well-known/oauth-protected-resource/mcp/personal")
    assert response.status_code == 503
    assert response.json()["error"] == "oauth_not_configured"
    get_config().server.public_base_url = "https://pas.example.test"
    response = await client.get("/.well-known/oauth-protected-resource/mcp/personal")
    assert response.status_code == 200
    assert response.json()["resource"] == "https://pas.example.test/mcp/personal"
