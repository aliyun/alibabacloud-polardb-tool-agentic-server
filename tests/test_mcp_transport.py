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
from jose import jwt as jose_jwt
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
    AllocationMode, AuthProvider, Base, CredentialCapability, CredentialPurpose,
    Instance, InstanceCredential, InstanceStatus, InstanceTopology, Permission,
    User, UserInstanceBinding, UserRole,
)
from server.core.crypto import encrypt
from server.mcp.transport import mcp_lifespan, reset_mcp


def _create_mcp_token(user_id: str) -> str:
    """Create a valid MCP access token accepted by PASAuthProvider.load_access_token()."""
    private_key, _ = _load_keys()
    config = get_config()
    now = int(time.time())
    return jose_jwt.encode(
        {
            "sub": user_id,
            "aud": f"{config.server.public_base_url}/mcp",
            "jti": str(uuid.uuid4()),
            "iat": now,
            "exp": now + 3600,
            "type": "access",
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
        await session.commit()

        return {"admin": admin, "instance": instance}


@pytest.fixture
async def client(setup_data):
    app = create_app()
    async with mcp_lifespan():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
def auth_headers(setup_data):
    token = _create_mcp_token(f"user:{setup_data['admin'].id}")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def rest_auth_headers(setup_data):
    """Auth headers for legacy REST endpoints (standard JWT without audience)."""
    token = create_access_token({"sub": setup_data["admin"].id, "role": "admin"})
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
            json=_jsonrpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            }),
            headers=MCP_HEADERS,
        )
        assert resp.status_code == 401

    async def test_invalid_token_returns_401(self, client):
        headers = {**MCP_HEADERS, "Authorization": "Bearer invalid-token"}
        resp = await client.post(
            "/mcp",
            json=_jsonrpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            }),
            headers=headers,
        )
        assert resp.status_code == 401


class TestMCPTransportInitialize:
    async def test_initialize(self, client, auth_headers):
        headers = {**MCP_HEADERS, **auth_headers}
        resp = await client.post(
            "/mcp",
            json=_jsonrpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            }),
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
            json=_jsonrpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            }),
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
        assert "set_default_instance" in names
        assert "run_sql_transaction" in names
        assert "list_branches" in names
        assert "create_branch" in names
        assert "delete_branch" in names

    async def test_branch_tool_schemas(self, client, auth_headers):
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
        by_name = {t["name"]: t for t in tools}

        list_branches = by_name["list_branches"]
        assert list_branches["annotations"]["readOnlyHint"] is True
        assert list_branches["inputSchema"]["additionalProperties"] is False
        assert "instance_id" in list_branches["inputSchema"]["properties"]

        create_branch = by_name["create_branch"]
        assert create_branch["annotations"]["destructiveHint"] is False
        assert create_branch["inputSchema"]["additionalProperties"] is False
        assert "branch_name" in create_branch["inputSchema"]["required"]
        assert create_branch["inputSchema"]["properties"]["branch_name"]["minLength"] == 1
        assert "include_databases" in create_branch["inputSchema"]["properties"]
        assert "instance_id" in create_branch["inputSchema"]["properties"]

        delete_branch = by_name["delete_branch"]
        assert delete_branch["annotations"]["destructiveHint"] is True
        assert delete_branch["inputSchema"]["additionalProperties"] is False
        assert "branch_name" in delete_branch["inputSchema"]["required"]
        assert delete_branch["inputSchema"]["properties"]["branch_name"]["minLength"] == 1
        assert "confirm" not in delete_branch["inputSchema"]["properties"]

        branch_tools = [t for t in tools if "branch" in t["name"]]
        assert {t["name"] for t in branch_tools} == {
            "list_branches",
            "create_branch",
            "delete_branch",
        }
        forbidden_params = {"project_id", "branch_id", "parent_branch_name", "confirm"}
        for tool in branch_tools:
            properties = tool["inputSchema"].get("properties", {})
            assert forbidden_params.isdisjoint(properties)

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
        assert set(mcp_branch_tools) == set(rest_branch_tools) == {
            "list_branches",
            "create_branch",
            "delete_branch",
        }

        for name, mcp_tool in mcp_branch_tools.items():
            rest_tool = rest_branch_tools[name]
            assert mcp_tool["annotations"] == rest_tool["annotations"]
            assert mcp_tool["inputSchema"].get("additionalProperties") is False
            assert rest_tool["inputSchema"].get("additionalProperties") is False
            assert set(mcp_tool["inputSchema"].get("required", [])) == set(
                rest_tool["inputSchema"].get("required", [])
            )
            assert set(mcp_tool["inputSchema"].get("properties", {})) == set(
                rest_tool["inputSchema"].get("properties", {})
            )
            for property_name, mcp_schema in mcp_tool["inputSchema"].get("properties", {}).items():
                rest_schema = rest_tool["inputSchema"]["properties"][property_name]
                assert _schema_type_signature(mcp_schema) == _schema_type_signature(rest_schema)

        include_schema = mcp_branch_tools["create_branch"]["inputSchema"]["properties"]["include_databases"]
        array_schema = next(item for item in include_schema["anyOf"] if item.get("type") == "array")
        assert array_schema["items"]["minLength"] == 1

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
        schema = run_sql["inputSchema"]["properties"]["branch"]
        assert any(item.get("type") == "string" for item in schema["anyOf"])
        assert any(item.get("type") == "null" for item in schema["anyOf"])
        assert "branch" not in run_sql["inputSchema"].get("required", [])
        rest_schema = rest_run_sql["inputSchema"]["properties"]["branch"]
        assert _schema_type_signature(schema) == _schema_type_signature(rest_schema)

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
                json=_jsonrpc("tools/call", {
                    "name": name,
                    "arguments": arguments,
                }, req_id=req_id),
                headers=headers,
            )
            assert resp.status_code == 200
            events = _parse_sse_response(resp.text)
            result = events[0].get("result", {})
            content = result.get("content", [])
            assert len(content) > 0
            return cast(dict, json.loads(content[0]["text"]))

        list_result = {"content": [{"type": "text", "text": json.dumps({"branches": []})}]}
        create_result = {"content": [{"type": "text", "text": json.dumps({
            "branch_name": "br_new",
            "status": "created",
        })}]}
        delete_result = {"content": [{"type": "text", "text": json.dumps({
            "branch_name": "br_old",
            "status": "deleted",
        })}]}

        with patch("server.mcp.transport.handle_list_branches",
                   new_callable=AsyncMock, return_value=list_result) as list_branches:
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
                json=_jsonrpc("tools/call", {
                    "name": name,
                    "arguments": arguments,
                }, req_id=req_id),
                headers=headers,
            )
            assert resp.status_code == 200
            events = _parse_sse_response(resp.text)
            result = events[0].get("result", {})
            content = result.get("content", [])
            assert len(content) > 0
            return cast(dict, json.loads(content[0]["text"]))

        list_result = {"content": [{"type": "text", "text": json.dumps({"branches": []})}]}
        create_result = {"content": [{"type": "text", "text": json.dumps({
            "branch_name": "br_new",
            "status": "created",
        })}]}
        delete_result = {"content": [{"type": "text", "text": json.dumps({
            "branch_name": "br_old",
            "status": "deleted",
        })}]}

        with patch("server.mcp.transport.handle_list_branches",
                   new_callable=AsyncMock, return_value=list_result) as list_branches:
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
                json=_jsonrpc("tools/call", {
                    "name": name,
                    "arguments": arguments,
                }, req_id=req_id),
                headers=headers,
            )
            assert resp.status_code == 200
            events = _parse_sse_response(resp.text)
            result = events[0].get("result", {})
            content = result.get("content", [])
            assert len(content) > 0
            return cast(dict, json.loads(content[0]["text"]))

        list_result = {"content": [{"type": "text", "text": json.dumps({"branches": []})}]}
        create_result = {"content": [{"type": "text", "text": json.dumps({
            "branch_name": "br_new",
            "status": "created",
        })}]}
        delete_result = {"content": [{"type": "text", "text": json.dumps({
            "branch_name": "br_old",
            "status": "deleted",
        })}]}

        with patch("server.mcp.transport.handle_list_branches",
                   new_callable=AsyncMock, return_value=list_result) as list_branches:
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
                json=_jsonrpc("tools/call", {
                    "name": name,
                    "arguments": arguments,
                }, req_id=req_id),
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
                json=_jsonrpc("tools/call", {
                    "name": name,
                    "arguments": arguments,
                }, req_id=req_id),
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
                json=_jsonrpc("tools/call", {
                    "name": name,
                    "arguments": arguments,
                }, req_id=req_id),
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
                {"content": [{"type": "text", "text": json.dumps({
                    "branches": [{"branch_name": "MAIN"}],
                })}]},
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
                {"content": [{"type": "text", "text": json.dumps({
                    "branch_name": "br_new",
                    "status": "created",
                })}]},
            ),
            (
                "delete_branch",
                "handle_delete_branch",
                "/mcp/rest/delete_branch",
                {
                    "instance_id": setup_data["instance"].id,
                    "branch_name": "br_old",
                },
                {"content": [{"type": "text", "text": json.dumps({
                    "branch_name": "br_old",
                    "status": "deleted",
                })}]},
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
                    "content": [{"type": "text", "text": json.dumps({
                        "error": "INVALID_IDENTIFIER",
                        "message": "branch_name contains forbidden characters.",
                    })}],
                    "isError": True,
                },
            ),
        ]

        for index, (
            name, handler_name, rest_path, arguments, handler_result,
        ) in enumerate(tool_cases, start=10):
            transport_patch = f"server.mcp.transport.{handler_name}"
            server_patch = f"server.mcp.server.{handler_name}"
            with patch(transport_patch, new_callable=AsyncMock, return_value=handler_result), \
                 patch(server_patch, new_callable=AsyncMock, return_value=handler_result):
                assert await call_mcp_tool(name, arguments, index) == await call_rest_tool(
                    rest_path, arguments,
                )

    async def test_branch_tool_call_error_sets_mcp_is_error(self, client, auth_headers):
        session_id = await self._initialize(client, auth_headers)
        headers = {**MCP_HEADERS, **auth_headers}
        if session_id:
            headers["mcp-session-id"] = session_id

        resp = await client.post(
            "/mcp",
            json=_jsonrpc("tools/call", {
                "name": "create_branch",
                "arguments": {
                    "branch_name": "bad;name",
                },
            }, req_id=6),
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
            }, req_id=7),
            headers=headers,
        )

        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        result = events[0].get("result", {})
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "BLOCKED_SQL"

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
            json=_jsonrpc("tools/call", {
                "name": "run_sql",
                "arguments": {
                    "sql": "DROP DATABASE app",
                    "instance_id": setup_data["instance"].id,
                    "confirm": True,
                },
            }, req_id=8),
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
            json=_jsonrpc("tools/call", {
                "name": "list_instances",
                "arguments": {},
            }, req_id=3),
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
            json=_jsonrpc("initialize", {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            }),
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
            "content": [{"type": "text", "text": json.dumps({
                "results": [{"columns": [], "rows": [], "row_count": 0, "truncated": False}],
                "statement_count": 1,
            })}],
        }
        with patch("server.mcp.transport.handle_run_sql_transaction",
                    new_callable=AsyncMock, return_value=mock_result):
            resp = await client.post(
                "/mcp",
                json=_jsonrpc("tools/call", {
                    "name": "run_sql_transaction",
                    "arguments": {
                        "sql_statements": ["INSERT INTO t VALUES (1)"],
                    },
                }, req_id=3),
                headers=headers,
            )
        assert resp.status_code == 200
        events = _parse_sse_response(resp.text)
        result = events[0].get("result", {})
        content = result.get("content", [])
        assert len(content) > 0
