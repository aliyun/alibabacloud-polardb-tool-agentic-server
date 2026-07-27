import base64
import json
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from server.aliyun.polardb_client import MockPolarDBClient, set_polardb_client, reset_polardb_client
from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.jwt_manager import create_access_token, reset_keys
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.core.connection_cache import ConnectionCache
from server.core.sql_executor import reset_rate_limiters
from server.core.sql_gateway import SQLGateway
from server.db import engine as engine_mod
from server.models import (
    AllocationMode, AuditLog, AuditStatus, AuthProvider, Base, BindingCapability,
    CredentialCapability, CredentialPurpose, Instance, InstanceCredential,
    InstanceStatus, InstanceTopology, Permission, User, UserInstanceBinding,
    UserInstanceBindingCapability, UserRole,
)
from server.core.crypto import encrypt
from server.mcp.tools import set_gateway, reset_gateway
from server.mcp.transport import reset_mcp
from sqlalchemy import update


_MOCK_SQL_RESULT = {
    "columns": ["result"],
    "rows": [["mock"]],
    "row_count": 1,
    "truncated": False,
}


def _assert_nullable_string_schema(schema: dict) -> None:
    assert schema["default"] is None
    assert {"type": "string"} in schema["anyOf"]
    assert {"type": "null"} in schema["anyOf"]


def _assert_nullable_array_schema(schema: dict) -> None:
    assert schema["default"] is None
    assert {"type": "null"} in schema["anyOf"]
    array_schema = next(item for item in schema["anyOf"] if item.get("type") == "array")
    assert array_schema["items"]["type"] == "string"
    assert array_schema["items"]["minLength"] == 1


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    reset_mcp()
    set_polardb_client(MockPolarDBClient())
    reset_rate_limiters()

    mock_cache = AsyncMock(spec=ConnectionCache)
    gateway = SQLGateway(mock_cache)

    async def mock_execute(**kwargs):
        if kwargs.get("sql") == "SHOW BRANCHES":
            return {
                "columns": ["Branch"],
                "rows": [["mock"]],
                "row_count": 1,
                "truncated": False,
            }
        return _MOCK_SQL_RESULT.copy()

    async def mock_execute_transaction(**kwargs):
        return [_MOCK_SQL_RESULT.copy() for _ in kwargs.get("sql_statements", [])]

    with patch.object(gateway, "execute", side_effect=mock_execute), \
         patch.object(gateway, "execute_transaction", side_effect=mock_execute_transaction):
        set_gateway(gateway)
        yield
    reset_gateway()
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
        # Create admin user
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

        # Create shared instance
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

        # Create DB account
        encrypted_pw = encrypt("test_password", key=encryption_key)
        credential = InstanceCredential(
            instance_id=instance.id,
            name="pas_admin",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("pas_admin", key=encryption_key),
            password_ciphertext=encrypted_pw,
            created_by_user_id=admin.id,
        )
        session.add(credential)
        await session.commit()
        await session.refresh(credential)

        # Bind user to instance
        binding = UserInstanceBinding(
            user_id=admin.id,
            instance_id=instance.id,
            credential_id=credential.id,
            permission=Permission.READWRITE,
        )
        binding.capabilities = [
            UserInstanceBindingCapability(
                capability=BindingCapability.SQL_READ
            ),
            UserInstanceBindingCapability(
                capability=BindingCapability.SQL_WRITE
            ),
        ]
        session.add(binding)
        await session.commit()

        return {"admin": admin, "instance": instance}


@pytest.fixture
async def client(setup_data):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers(setup_data):
    token = create_access_token({"sub": setup_data["admin"].id, "role": "admin"})
    return {"Authorization": f"Bearer {token}"}


class TestListTools:
    async def test_list_tools(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        assert resp.status_code == 200
        tools = resp.json()["tools"]
        names = [t["name"] for t in tools]
        assert "run_sql" in names
        assert "list_instances" not in names
        assert "set_default_instance" in names

    async def test_run_sql_has_confirm_param(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        tools = resp.json()["tools"]
        run_sql = next(t for t in tools if t["name"] == "run_sql")
        assert "confirm" in run_sql["inputSchema"]["properties"]
        assert run_sql["inputSchema"]["properties"]["confirm"]["type"] == "boolean"

    async def test_positive_integer_limits_have_minimum_schema(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        tools = resp.json()["tools"]
        by_name = {tool["name"]: tool for tool in tools}

        assert by_name["run_sql"]["inputSchema"]["properties"]["max_rows"]["minimum"] == 1
        assert by_name["describe_schema"]["inputSchema"]["properties"]["max_tables"]["minimum"] == 1

    async def test_run_sql_has_branch_param(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        tools = resp.json()["tools"]
        run_sql = next(t for t in tools if t["name"] == "run_sql")
        assert "branch" in run_sql["inputSchema"]["properties"]
        _assert_nullable_string_schema(run_sql["inputSchema"]["properties"]["branch"])

    async def test_run_sql_transaction_has_no_branch_param(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        tools = resp.json()["tools"]
        transaction = next(t for t in tools if t["name"] == "run_sql_transaction")
        assert "branch" not in transaction["inputSchema"]["properties"]

    async def test_branch_tool_optional_params_are_nullable(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        tools = resp.json()["tools"]
        by_name = {tool["name"]: tool for tool in tools}

        expected_nullable = {
            "run_sql": {"branch"},
            "list_branches": {"instance_id"},
            "create_branch": {"instance_id"},
            "delete_branch": {"instance_id"},
        }
        for tool_name, property_names in expected_nullable.items():
            properties = by_name[tool_name]["inputSchema"]["properties"]
            for property_name in property_names:
                _assert_nullable_string_schema(properties[property_name])

    async def test_branch_tools_have_expected_schema(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        tools = resp.json()["tools"]
        names = [t["name"] for t in tools]
        assert "list_branches" in names
        assert "create_branch" in names
        assert "delete_branch" in names

        create_branch = next(t for t in tools if t["name"] == "create_branch")
        assert create_branch["inputSchema"]["additionalProperties"] is False
        assert create_branch["inputSchema"]["required"] == ["branch_name"]
        assert create_branch["inputSchema"]["properties"]["branch_name"]["minLength"] == 1
        assert "include_databases" in create_branch["inputSchema"]["properties"]
        _assert_nullable_array_schema(create_branch["inputSchema"]["properties"]["include_databases"])

        list_branches = next(t for t in tools if t["name"] == "list_branches")
        assert list_branches["inputSchema"]["additionalProperties"] is False

        delete_branch = next(t for t in tools if t["name"] == "delete_branch")
        assert delete_branch["inputSchema"]["additionalProperties"] is False
        assert delete_branch["inputSchema"]["properties"]["branch_name"]["minLength"] == 1
        assert delete_branch["annotations"]["destructiveHint"] is True
        assert "confirm" not in delete_branch["inputSchema"]["properties"]

    async def test_branch_tools_contract_stays_minimal(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        tools = resp.json()["tools"]
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

    async def test_run_sql_has_annotations(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        tools = resp.json()["tools"]
        run_sql = next(t for t in tools if t["name"] == "run_sql")
        assert run_sql["annotations"]["destructiveHint"] is True
        assert run_sql["annotations"]["readOnlyHint"] is False

    async def test_set_default_instance_tool_schema(self, client, auth_headers):
        resp = await client.get("/mcp/rest/tools")
        tools = resp.json()["tools"]
        sdi = next(t for t in tools if t["name"] == "set_default_instance")
        assert "instance_id" in sdi["inputSchema"]["properties"]
        assert sdi["annotations"]["idempotentHint"] is True


class TestRemovedListInstances:
    async def test_authenticated_route_is_not_found(
        self, client, auth_headers
    ):
        resp = await client.get("/mcp/rest/list_instances", headers=auth_headers)
        assert resp.status_code == 404

    async def test_unauthenticated_route_is_not_found(self, client):
        resp = await client.get("/mcp/rest/list_instances")
        assert resp.status_code == 404


class TestDescribeSchemaRest:
    async def test_describe_schema_accepts_omitted_body(self, client, auth_headers):
        result = {"content": [{"type": "text", "text": json.dumps({"tables": [], "has_more": False})}]}

        with patch("server.mcp.server.handle_describe_schema", new_callable=AsyncMock, return_value=result) as handler:
            resp = await client.post("/mcp/rest/describe_schema", headers=auth_headers)

        assert resp.status_code == 200
        payload = json.loads(resp.json()["content"][0]["text"])
        assert payload == {"tables": [], "has_more": False}
        assert handler.await_args.kwargs["instance_id"] is None
        assert handler.await_args.kwargs["database"] is None
        assert handler.await_args.kwargs["table_pattern"] is None
        assert handler.await_args.kwargs["include_columns"] is True
        assert handler.await_args.kwargs["cursor"] is None
        assert handler.await_args.kwargs["max_tables"] == 20

    @pytest.mark.parametrize("max_tables", [True, "2", 0, -1])
    async def test_describe_schema_rejects_invalid_max_tables(
        self, client, auth_headers, max_tables,
    ):
        resp = await client.post(
            "/mcp/rest/describe_schema",
            json={"max_tables": max_tables},
            headers=auth_headers,
        )

        assert resp.status_code == 422


class TestBranchTools:
    async def test_list_branches_success(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/list_branches", json={
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)

        assert resp.status_code == 200
        payload = json.loads(resp.json()["content"][0]["text"])
        assert payload == {"branches": [{"branch_name": "mock"}]}

    async def test_list_branches_accepts_omitted_body(self, client, auth_headers, setup_data):
        gateway = SimpleNamespace(execute=AsyncMock(return_value={
            "columns": ["Branch"],
            "rows": [["MAIN"]],
            "row_count": 1,
            "truncated": False,
        }))

        with patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway):
            resp = await client.post("/mcp/rest/list_branches", headers=auth_headers)

        assert resp.status_code == 200
        payload = json.loads(resp.json()["content"][0]["text"])
        assert payload == {"branches": [{"branch_name": "MAIN"}]}
        assert gateway.execute.await_args.kwargs["instance_id"] == setup_data["instance"].id

    async def test_create_branch_success(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/create_branch", json={
            "instance_id": setup_data["instance"].id,
            "branch_name": "br_new",
            "include_databases": ["db1", "db2"],
        }, headers=auth_headers)

        assert resp.status_code == 200
        payload = json.loads(resp.json()["content"][0]["text"])
        assert payload == {"branch_name": "br_new", "status": "created"}

    async def test_delete_branch_success(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/delete_branch", json={
            "instance_id": setup_data["instance"].id,
            "branch_name": "br_old",
        }, headers=auth_headers)

        assert resp.status_code == 200
        payload = json.loads(resp.json()["content"][0]["text"])
        assert payload == {"branch_name": "br_old", "status": "deleted"}

    async def test_branch_tools_resolve_instance_when_instance_id_omitted(
        self, client, auth_headers, setup_data
    ):
        gateway = SimpleNamespace(execute=AsyncMock(side_effect=[
            {
                "columns": ["Branch"],
                "rows": [["MAIN"]],
                "row_count": 1,
                "truncated": False,
            },
            {"columns": [], "rows": [], "row_count": 0, "truncated": False},
            {"columns": [], "rows": [], "row_count": 0, "truncated": False},
        ]))

        with patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway):
            list_resp = await client.post("/mcp/rest/list_branches", json={}, headers=auth_headers)
            create_resp = await client.post(
                "/mcp/rest/create_branch",
                json={"branch_name": "br_new"},
                headers=auth_headers,
            )
            delete_resp = await client.post(
                "/mcp/rest/delete_branch",
                json={"branch_name": "br_old"},
                headers=auth_headers,
            )

        assert list_resp.status_code == 200
        assert create_resp.status_code == 200
        assert delete_resp.status_code == 200
        assert [call.kwargs["instance_id"] for call in gateway.execute.await_args_list] == [
            setup_data["instance"].id,
            setup_data["instance"].id,
            setup_data["instance"].id,
        ]
        assert [call.kwargs["sql"] for call in gateway.execute.await_args_list] == [
            "SHOW BRANCHES",
            "CREATE BRANCH br_new",
            "DROP BRANCH br_old",
        ]

    async def test_branch_tools_accept_explicit_null_optional_args(
        self, client, auth_headers, setup_data
    ):
        gateway = SimpleNamespace(execute=AsyncMock(side_effect=[
            {
                "columns": ["Branch"],
                "rows": [["MAIN"]],
                "row_count": 1,
                "truncated": False,
            },
            {"columns": [], "rows": [], "row_count": 0, "truncated": False},
            {"columns": [], "rows": [], "row_count": 0, "truncated": False},
        ]))

        with patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway):
            list_resp = await client.post(
                "/mcp/rest/list_branches",
                json={"instance_id": None},
                headers=auth_headers,
            )
            create_resp = await client.post(
                "/mcp/rest/create_branch",
                json={
                    "branch_name": "br_new",
                    "instance_id": None,
                    "include_databases": None,
                },
                headers=auth_headers,
            )
            delete_resp = await client.post(
                "/mcp/rest/delete_branch",
                json={"branch_name": "br_old", "instance_id": None},
                headers=auth_headers,
            )

        assert list_resp.status_code == 200
        assert create_resp.status_code == 200
        assert delete_resp.status_code == 200
        assert [call.kwargs["instance_id"] for call in gateway.execute.await_args_list] == [
            setup_data["instance"].id,
            setup_data["instance"].id,
            setup_data["instance"].id,
        ]
        assert [call.kwargs["sql"] for call in gateway.execute.await_args_list] == [
            "SHOW BRANCHES",
            "CREATE BRANCH br_new",
            "DROP BRANCH br_old",
        ]

    async def test_branch_tools_reject_inaccessible_instance_before_gateway(
        self, client, auth_headers, test_engine
    ):
        engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with engine_mod._session_factory() as session:
            other = Instance(
                cluster_id="pc-other-branch-001",
                name="Other Branch DB",
                topology=InstanceTopology.SINGLE_TENANT,
                allocation_mode=AllocationMode.REGISTERED,
                host="127.0.0.1",
                port=3308,
                status=InstanceStatus.ACTIVE,
            )
            session.add(other)
            await session.commit()
            await session.refresh(other)
            other_id = other.id

        gateway = SimpleNamespace(execute=AsyncMock())

        with patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway):
            requests = [
                ("/mcp/rest/list_branches", {"instance_id": other_id}),
                ("/mcp/rest/create_branch", {"instance_id": other_id, "branch_name": "br_new"}),
                ("/mcp/rest/delete_branch", {"instance_id": other_id, "branch_name": "br_old"}),
            ]
            for path, payload in requests:
                resp = await client.post(path, json=payload, headers=auth_headers)
                assert resp.status_code == 200
                data = resp.json()
                assert data["isError"] is True
                content = json.loads(data["content"][0]["text"])
                assert content["error"] == "INSTANCE_NOT_ACCESSIBLE"

        gateway.execute.assert_not_awaited()

    async def test_create_branch_invalid_name_returns_mcp_error(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/create_branch", json={
            "instance_id": setup_data["instance"].id,
            "branch_name": "bad;name",
        }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["isError"] is True
        payload = json.loads(data["content"][0]["text"])
        assert payload["error"] == "INVALID_IDENTIFIER"

    async def test_branch_tools_reject_unsupported_rest_fields(self, client, auth_headers, setup_data):
        requests = [
            ("/mcp/rest/list_branches", {
                "instance_id": setup_data["instance"].id,
                "project_id": "unsupported",
            }),
            ("/mcp/rest/list_branches", {
                "instance_id": setup_data["instance"].id,
                "branch_id": "unsupported",
            }),
            ("/mcp/rest/create_branch", {
                "instance_id": setup_data["instance"].id,
                "branch_name": "br_new",
                "parent_branch_name": "unsupported",
            }),
            ("/mcp/rest/create_branch", {
                "instance_id": setup_data["instance"].id,
                "branch_name": "br_new",
                "branch_id": "unsupported",
            }),
            ("/mcp/rest/delete_branch", {
                "instance_id": setup_data["instance"].id,
                "branch_name": "br_old",
                "confirm": True,
            }),
            ("/mcp/rest/delete_branch", {
                "instance_id": setup_data["instance"].id,
                "branch_name": "br_old",
                "branch_id": "unsupported",
            }),
        ]

        for path, payload in requests:
            resp = await client.post(path, json=payload, headers=auth_headers)
            assert resp.status_code == 422

    async def test_branch_tools_reject_invalid_rest_field_types(self, client, auth_headers, setup_data):
        requests = [
            ("/mcp/rest/list_branches", {"instance_id": 123}),
            ("/mcp/rest/create_branch", {"branch_name": 123}),
            ("/mcp/rest/create_branch", {"branch_name": ""}),
            ("/mcp/rest/create_branch", {
                "branch_name": "br_new",
                "include_databases": "db1",
            }),
            ("/mcp/rest/create_branch", {
                "branch_name": "br_new",
                "include_databases": [123],
            }),
            ("/mcp/rest/create_branch", {
                "branch_name": "br_new",
                "include_databases": [""],
            }),
            ("/mcp/rest/delete_branch", {"branch_name": 123}),
            ("/mcp/rest/delete_branch", {"branch_name": ""}),
        ]

        for path, payload in requests:
            resp = await client.post(path, json=payload, headers=auth_headers)
            assert resp.status_code == 422


class TestRunSQL:
    @pytest.mark.parametrize(
        ("path", "payload"),
        [
            (
                "/mcp/rest/run_sql",
                {"sql": "SELECT 1"},
            ),
            (
                "/mcp/rest/run_sql_transaction",
                {"sql_statements": ["SELECT 1"]},
            ),
            (
                "/mcp/rest/describe_schema",
                {},
            ),
            (
                "/mcp/rest/create_branch",
                {"branch_name": "safe_branch"},
            ),
        ],
    )
    async def test_provisioning_prerequisite_errors_are_sanitized_for_all_callers(
        self,
        client,
        auth_headers,
        setup_data,
        caplog,
        path,
        payload,
    ):
        sentinel = (
            "password=SECRET host=private db=secret_db "
            "SQL=CREATE USER SENTINEL"
        )
        request_payload = {
            **payload,
            "instance_id": setup_data["instance"].id,
        }
        caplog.set_level(logging.INFO)

        with (
            patch(
                "server.mcp.tools.handlers.get_user_credential",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "server.mcp.tools.handlers.create_db_account",
                new_callable=AsyncMock,
                side_effect=RuntimeError(sentinel),
            ),
        ):
            response = await client.post(
                path,
                json=request_payload,
                headers=auth_headers,
            )

        assert response.status_code == 200
        body = response.json()
        assert body["isError"] is True
        assert json.loads(body["content"][0]["text"]) == {
            "error": "CONNECTION_ERROR",
            "message": "Database account provisioning failed.",
        }
        assert sentinel not in caplog.text
        async with engine_mod._session_factory() as session:
            audit = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "sql_credential.resolve")
                    .order_by(AuditLog.created_at.desc())
                )
            ).scalars().first()
        assert audit is not None
        assert audit.status == AuditStatus.ERROR
        assert audit.error_code == "CONNECTION_ERROR"
        assert sentinel not in (audit.metadata_json or "")

    async def test_run_sql_success(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "content" in data

    @pytest.mark.parametrize("max_rows", [True, "2", 0, -1])
    async def test_run_sql_rejects_invalid_max_rows_at_request_layer(
        self, client, auth_headers, setup_data, max_rows,
    ):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
            "instance_id": setup_data["instance"].id,
            "max_rows": max_rows,
        }, headers=auth_headers)

        assert resp.status_code == 422

    async def test_run_sql_rejects_empty_sql_at_request_layer(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)

        assert resp.status_code == 422

    async def test_run_sql_branch_echo(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
            "instance_id": setup_data["instance"].id,
            "branch": "br1",
        }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        payload = json.loads(data["content"][0]["text"])
        assert payload["branch"] == "br1"

    async def test_run_sql_preserves_branch_before_execution_and_echo(self, client, auth_headers, setup_data):
        gateway = SimpleNamespace(execute=AsyncMock(return_value={
            "columns": ["x"],
            "rows": [[1]],
            "row_count": 1,
            "truncated": False,
        }))

        with patch("server.mcp.tools.handlers.get_gateway", return_value=gateway):
            resp = await client.post("/mcp/rest/run_sql", json={
                "sql": "SELECT 1",
                "instance_id": setup_data["instance"].id,
                "branch": "Br1",
            }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        payload = json.loads(data["content"][0]["text"])
        assert payload["branch"] == "Br1"
        assert gateway.execute.await_args.kwargs["branch"] == "Br1"

    async def test_run_sql_cursor_request_preserves_branch(self, client, auth_headers, setup_data):
        from server.core.sql_executor import encode_cursor

        gateway = SimpleNamespace(execute=AsyncMock(return_value={
            "columns": ["x"],
            "rows": [[2]],
            "row_count": 1,
            "truncated": False,
        }))

        with patch("server.mcp.tools.handlers.get_gateway", return_value=gateway):
            resp = await client.post("/mcp/rest/run_sql", json={
                "sql": "SELECT 1",
                "instance_id": setup_data["instance"].id,
                "branch": "Br1",
                "cursor": encode_cursor(100),
            }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        payload = json.loads(data["content"][0]["text"])
        assert payload["branch"] == "Br1"
        assert gateway.execute.await_args.kwargs["branch"] == "Br1"
        assert gateway.execute.await_args.kwargs["offset"] == 100

    async def test_run_sql_rejects_non_string_branch_at_request_layer(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
            "instance_id": setup_data["instance"].id,
            "branch": 123,
        }, headers=auth_headers)

        assert resp.status_code == 422

    @pytest.mark.parametrize("max_rows", [True, "2", 0, -1])
    async def test_run_sql_handler_rejects_invalid_max_rows_before_resolution(
        self, setup_data, max_rows,
    ):
        from server.mcp.tools.handlers import handle_run_sql

        session = AsyncMock()
        with patch("server.mcp.tools.handlers.resolve_target_instance", new_callable=AsyncMock) as resolve:
            result = await handle_run_sql(
                setup_data["admin"], session, sql="SELECT 1", max_rows=max_rows,
            )

        resolve.assert_not_awaited()
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "error": "INVALID_ARGUMENT",
            "message": "max_rows must be a positive integer.",
        }

    @pytest.mark.parametrize(
        "sql",
        ["", "   ", ";", " ; ; ", "/* comment */", "-- comment", "# comment", "/* comment */;"],
    )
    async def test_run_sql_handler_rejects_empty_sql_before_resolution(self, setup_data, sql):
        from server.mcp.tools.handlers import handle_run_sql

        session = AsyncMock()
        with patch("server.mcp.tools.handlers.resolve_target_instance", new_callable=AsyncMock) as resolve:
            result = await handle_run_sql(setup_data["admin"], session, sql=sql)

        resolve.assert_not_awaited()
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "error": "INVALID_ARGUMENT",
            "message": "sql must contain exactly one SQL statement.",
        }

    async def test_run_sql_handler_rejects_multiple_statements_before_resolution(self, setup_data):
        from server.mcp.tools.handlers import handle_run_sql

        session = AsyncMock()
        with patch("server.mcp.tools.handlers.resolve_target_instance", new_callable=AsyncMock) as resolve:
            result = await handle_run_sql(
                setup_data["admin"], session, sql="SELECT 1; INSERT INTO t VALUES (1)",
            )

        resolve.assert_not_awaited()
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "error": "INVALID_ARGUMENT",
            "message": (
                "run_sql accepts exactly one SQL statement. "
                "Use run_sql_transaction for multiple statements."
            ),
        }

    async def test_run_sql_rejects_empty_branch_before_execution(self, client, auth_headers, setup_data):
        gateway = SimpleNamespace(execute=AsyncMock())

        with patch("server.mcp.tools.handlers.get_gateway", return_value=gateway):
            resp = await client.post("/mcp/rest/run_sql", json={
                "sql": "SELECT 1",
                "instance_id": setup_data["instance"].id,
                "branch": "",
            }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["isError"] is True
        payload = json.loads(data["content"][0]["text"])
        assert payload["error"] == "INVALID_IDENTIFIER"
        gateway.execute.assert_not_awaited()

    async def test_run_sql_rejects_invalid_branch_before_execution(self, client, auth_headers, setup_data):
        gateway = SimpleNamespace(execute=AsyncMock())

        with patch("server.mcp.tools.handlers.get_gateway", return_value=gateway):
            resp = await client.post("/mcp/rest/run_sql", json={
                "sql": "SELECT 1",
                "instance_id": setup_data["instance"].id,
                "branch": "bad;name",
            }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["isError"] is True
        payload = json.loads(data["content"][0]["text"])
        assert payload["error"] == "INVALID_IDENTIFIER"
        gateway.execute.assert_not_awaited()

    async def test_run_sql_rejects_invalid_branch_before_rate_limit_and_resolution(self, setup_data):
        from server.core.sql_executor import RateLimitError
        from server.mcp.tools.handlers import handle_run_sql

        session = AsyncMock()
        with (
            patch("server.mcp.tools.handlers._check_rate_limit", side_effect=RateLimitError()) as check_rate,
            patch("server.mcp.tools.handlers.resolve_target_instance", new_callable=AsyncMock) as resolve,
        ):
            result = await handle_run_sql(
                setup_data["admin"],
                session,
                sql="SELECT 1",
                branch="bad;name",
            )

        check_rate.assert_not_called()
        resolve.assert_not_awaited()
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "INVALID_IDENTIFIER"

    async def test_run_sql_rejects_session_branch_sql_when_branch_requested(
        self, client, auth_headers, setup_data
    ):
        gateway = SimpleNamespace(execute=AsyncMock())

        with (
            patch("server.mcp.tools.handlers.get_gateway", return_value=gateway),
            patch("server.mcp.tools.handlers.log_audit", new_callable=AsyncMock) as log,
        ):
            resp = await client.post("/mcp/rest/run_sql", json={
                "sql": "SET @@session.branch = 'br2'",
                "instance_id": setup_data["instance"].id,
                "branch": "br1",
            }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["isError"] is True
        payload = json.loads(data["content"][0]["text"])
        assert payload["error"] == "BLOCKED_SQL"
        gateway.execute.assert_not_awaited()
        log.assert_awaited_once()
        assert log.await_args.kwargs["status"].value == "blocked"
        assert log.await_args.kwargs["error_message"] == "Branch session override blocked"

    async def test_run_sql_allows_session_branch_text_literal_when_branch_requested(
        self, client, auth_headers, setup_data
    ):
        gateway = SimpleNamespace(execute=AsyncMock(return_value={
            "columns": ["x"],
            "rows": [["@@session.branch"]],
            "row_count": 1,
            "truncated": False,
        }))

        with patch("server.mcp.tools.handlers.get_gateway", return_value=gateway):
            resp = await client.post("/mcp/rest/run_sql", json={
                "sql": "SELECT '@@session.branch' AS x",
                "instance_id": setup_data["instance"].id,
                "branch": "br1",
            }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is not True
        payload = json.loads(data["content"][0]["text"])
        assert payload["branch"] == "br1"
        gateway.execute.assert_awaited_once()

    async def test_run_sql_omits_branch_when_not_requested(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        payload = json.loads(data["content"][0]["text"])
        assert "branch" not in payload

    @pytest.mark.parametrize("branch", [None, "br1"])
    async def test_run_sql_sanitizes_unexpected_gateway_exception(
        self, client, auth_headers, setup_data, caplog, branch
    ):
        sentinel = (
            "password=SECRET host=private db=secret_db "
            "SQL=SELECT SENTINEL"
        )
        gateway = SimpleNamespace(
            execute=AsyncMock(side_effect=RuntimeError(sentinel))
        )
        caplog.set_level(logging.INFO)

        with patch("server.mcp.tools.handlers.get_gateway", return_value=gateway):
            resp = await client.post("/mcp/rest/run_sql", json={
                "sql": "SELECT 'SENTINEL'",
                "instance_id": setup_data["instance"].id,
                "database": "secret_db",
                "branch": branch,
            }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["isError"] is True
        payload = json.loads(data["content"][0]["text"])
        assert payload == {
            "error": "INTERNAL_ERROR",
            "message": "Internal SQL execution error",
        }
        assert sentinel not in caplog.text
        async with engine_mod._session_factory() as session:
            audit = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "run_sql")
                    .order_by(AuditLog.created_at.desc())
                )
            ).scalars().first()
        assert audit is not None
        assert audit.status == AuditStatus.ERROR
        assert audit.error_code == "INTERNAL_ERROR"
        assert sentinel not in (audit.metadata_json or "")
        assert "SENTINEL" not in (audit.metadata_json or "")
        assert "secret_db" not in (audit.metadata_json or "")

    async def test_blocked_sql(self, client, auth_headers, setup_data):
        """DROP DATABASE is always blocked, even with confirm=true."""
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "DROP DATABASE mydb",
            "instance_id": setup_data["instance"].id,
            "confirm": True,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is True
        content = json.loads(data["content"][0]["text"])
        assert content["error"] == "BLOCKED_SQL"

    async def test_inaccessible_instance(self, client, auth_headers):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
            "instance_id": "nonexistent-id",
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is True


@pytest.fixture
async def setup_multi_instance(test_engine, encryption_key):
    """Create a user with two instances (personal + shared) for routing tests."""
    engine_mod._engine = test_engine
    engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async with engine_mod._session_factory() as session:
        user = User(
            external_id="multi-user",
            display_name="Multi User",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.MEMBER,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        personal = Instance(
            cluster_id="pc-personal-001",
            name="Personal DB",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.AUTO_PROVISIONED,
            host="127.0.0.1", port=3306,
            status=InstanceStatus.ACTIVE,
            owner_user_id=user.id,
        )
        shared = Instance(
            cluster_id="pc-shared-001",
            name="Shared DB",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            host="127.0.0.1", port=3307,
            status=InstanceStatus.ACTIVE,
        )
        session.add_all([personal, shared])
        await session.commit()
        await session.refresh(personal)
        await session.refresh(shared)

        enc_pw = encrypt("test_password", key=encryption_key)
        for inst in [personal, shared]:
            credential = InstanceCredential(
                instance_id=inst.id, name=f"pas_{inst.cluster_id[:8]}",
                purpose=CredentialPurpose.DIRECT_ACCESS,
                capability=CredentialCapability.READWRITE,
                username_ciphertext=encrypt(f"pas_{inst.cluster_id[:8]}", key=encryption_key),
                password_ciphertext=enc_pw, created_by_user_id=user.id,
            )
            session.add(credential)
            await session.flush()
            binding = UserInstanceBinding(
                user_id=user.id, instance_id=inst.id,
                credential_id=credential.id, permission=Permission.READWRITE,
            )
            binding.capabilities = [
                UserInstanceBindingCapability(
                    capability=BindingCapability.SQL_READ
                ),
                UserInstanceBindingCapability(
                    capability=BindingCapability.SQL_WRITE
                ),
            ]
            session.add(binding)

        await session.commit()
        return {"user": user, "personal": personal, "shared": shared}


@pytest.fixture
async def multi_client(setup_multi_instance):
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def multi_auth_headers(setup_multi_instance):
    token = create_access_token({"sub": setup_multi_instance["user"].id, "role": "member"})
    return {"Authorization": f"Bearer {token}"}


class TestDefaultInstanceRouting:
    async def test_single_instance_auto_routes(self, client, auth_headers, setup_data):
        """When user has exactly one instance, instance_id can be omitted."""
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert result["instance_id"] == setup_data["instance"].id

    async def test_explicit_instance_id_still_works(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False

    async def test_multiple_instances_no_default_uses_personal(
        self, multi_client, multi_auth_headers, setup_multi_instance
    ):
        """With multiple instances and no default, falls back to personal instance."""
        resp = await multi_client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
        }, headers=multi_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert result["instance_id"] == setup_multi_instance["personal"].id

    async def test_multiple_instances_with_default(
        self, multi_client, multi_auth_headers, setup_multi_instance, test_engine
    ):
        """With multiple instances and a default set, uses the default."""
        engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with engine_mod._session_factory() as session:
            await session.execute(
                update(User)
                .where(User.id == setup_multi_instance["user"].id)
                .values(default_instance_id=setup_multi_instance["shared"].id)
            )
            await session.commit()

        resp = await multi_client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
        }, headers=multi_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        result = json.loads(data["content"][0]["text"])
        assert result["instance_id"] == setup_multi_instance["shared"].id


    async def test_zero_instances_returns_no_instance_available(
        self, test_engine, encryption_key
    ):
        """User with no instance bindings gets NO_INSTANCE_AVAILABLE error."""
        engine_mod._engine = test_engine
        engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        async with engine_mod._session_factory() as session:
            user = User(
                external_id="lonely-user",
                display_name="Lonely User",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=hash_password("password"),
                role=UserRole.MEMBER,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            user_id = user.id

        token = create_access_token({"sub": user_id, "role": "member"})
        headers = {"Authorization": f"Bearer {token}"}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/mcp/rest/run_sql", json={"sql": "SELECT 1"}, headers=headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is True
        content = json.loads(data["content"][0]["text"])
        assert content["error"] == "NO_INSTANCE_AVAILABLE"

    async def test_stale_default_cleared_and_falls_back_to_personal(
        self, multi_client, multi_auth_headers, setup_multi_instance, test_engine
    ):
        """When default_instance_id points to inaccessible instance, clear it and fall back to personal."""
        engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        stale_id = "00000000-0000-0000-0000-000000000000"
        async with engine_mod._session_factory() as session:
            await session.execute(
                update(User)
                .where(User.id == setup_multi_instance["user"].id)
                .values(default_instance_id=stale_id)
            )
            await session.commit()

        resp = await multi_client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
        }, headers=multi_auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert result["instance_id"] == setup_multi_instance["personal"].id

        # Verify default was cleared
        async with engine_mod._session_factory() as session:
            refreshed_user = (await session.get(User, setup_multi_instance["user"].id))
            assert refreshed_user.default_instance_id is None

    async def test_multiple_shared_no_personal_no_default_returns_multiple_instances(
        self, test_engine, encryption_key
    ):
        """Multiple SHARED instances, no PERSONAL, no default -> MULTIPLE_INSTANCES error."""
        engine_mod._engine = test_engine
        engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        async with engine_mod._session_factory() as session:
            user = User(
                external_id="shared-only-user",
                display_name="Shared Only User",
                auth_provider=AuthProvider.BUILTIN,
                password_hash=hash_password("password"),
                role=UserRole.MEMBER,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)

            shared1 = Instance(
                cluster_id="pc-shared-a01",
                name="Shared A",
                topology=InstanceTopology.SINGLE_TENANT,
                allocation_mode=AllocationMode.REGISTERED,
                host="127.0.0.1", port=3306,
                status=InstanceStatus.ACTIVE,
            )
            shared2 = Instance(
                cluster_id="pc-shared-b01",
                name="Shared B",
                topology=InstanceTopology.SINGLE_TENANT,
                allocation_mode=AllocationMode.REGISTERED,
                host="127.0.0.1", port=3307,
                status=InstanceStatus.ACTIVE,
            )
            session.add_all([shared1, shared2])
            await session.commit()
            await session.refresh(shared1)
            await session.refresh(shared2)

            enc_pw = encrypt("test_password", key=encryption_key)
            for inst in [shared1, shared2]:
                credential = InstanceCredential(
                    instance_id=inst.id, name=f"pas_{inst.cluster_id[:8]}",
                    purpose=CredentialPurpose.DIRECT_ACCESS,
                    capability=CredentialCapability.READWRITE,
                    username_ciphertext=encrypt(f"pas_{inst.cluster_id[:8]}", key=encryption_key),
                    password_ciphertext=enc_pw, created_by_user_id=user.id,
                )
                session.add(credential)
                await session.flush()
                binding = UserInstanceBinding(
                    user_id=user.id, instance_id=inst.id,
                    credential_id=credential.id, permission=Permission.READWRITE,
                )
                binding.capabilities = [
                    UserInstanceBindingCapability(
                        capability=BindingCapability.SQL_READ
                    ),
                    UserInstanceBindingCapability(
                        capability=BindingCapability.SQL_WRITE
                    ),
                ]
                session.add(binding)
            await session.commit()

            user_id = user.id
            shared1_id = shared1.id
            shared2_id = shared2.id

        token = create_access_token({"sub": user_id, "role": "member"})
        headers = {"Authorization": f"Bearer {token}"}

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.post("/mcp/rest/run_sql", json={"sql": "SELECT 1"}, headers=headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is True
        content = json.loads(data["content"][0]["text"])
        assert content["error"] == "MULTIPLE_INSTANCES"
        returned_ids = {inst["instance_id"] for inst in content["instances"]}
        assert shared1_id in returned_ids
        assert shared2_id in returned_ids


class TestSetDefaultInstance:
    async def test_set_default_instance(self, multi_client, multi_auth_headers, setup_multi_instance):
        resp = await multi_client.post("/mcp/rest/set_default_instance", json={
            "instance_id": setup_multi_instance["shared"].id,
        }, headers=multi_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert result["instance_id"] == setup_multi_instance["shared"].id

    async def test_set_default_then_route(self, multi_client, multi_auth_headers, setup_multi_instance):
        """After setting default, run_sql without instance_id uses it."""
        await multi_client.post("/mcp/rest/set_default_instance", json={
            "instance_id": setup_multi_instance["shared"].id,
        }, headers=multi_auth_headers)

        resp = await multi_client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
        }, headers=multi_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        result = json.loads(data["content"][0]["text"])
        assert result["instance_id"] == setup_multi_instance["shared"].id

    async def test_set_nonexistent_instance(self, multi_client, multi_auth_headers):
        resp = await multi_client.post("/mcp/rest/set_default_instance", json={
            "instance_id": "nonexistent-id",
        }, headers=multi_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["isError"] is True
        content = json.loads(data["content"][0]["text"])
        assert content["error"] == "INSTANCE_NOT_FOUND"

    async def test_set_inaccessible_instance(
        self, multi_client, multi_auth_headers, setup_multi_instance, test_engine
    ):
        """Cannot set default to an instance the user has no access to."""
        engine_mod._session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
        async with engine_mod._session_factory() as session:
            other = Instance(
                cluster_id="pc-other-001", name="Other DB",
                topology=InstanceTopology.SINGLE_TENANT,
                allocation_mode=AllocationMode.REGISTERED, host="127.0.0.1", port=3308,
                status=InstanceStatus.ACTIVE,
            )
            session.add(other)
            await session.commit()
            await session.refresh(other)
            other_id = other.id

        resp = await multi_client.post("/mcp/rest/set_default_instance", json={
            "instance_id": other_id,
        }, headers=multi_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["isError"] is True
        content = json.loads(data["content"][0]["text"])
        assert content["error"] == "INSTANCE_NOT_ACCESSIBLE"


class TestTwoPhaseConfirmation:
    async def test_select_executes_directly(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "SELECT 1",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False

    async def test_drop_table_without_confirm_returns_warning(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "DROP TABLE users",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is not True
        text = data["content"][0]["text"]
        assert "DESTRUCTIVE" in text
        assert "confirm" in text.lower()

    async def test_destructive_warning_includes_branch_when_requested(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "DROP TABLE users",
            "instance_id": setup_data["instance"].id,
            "branch": "br1",
        }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is not True
        text = data["content"][0]["text"]
        assert "Branch: br1" in text
        assert "same branch" in text

    async def test_destructive_empty_branch_returns_error_not_warning(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "DROP TABLE users",
            "instance_id": setup_data["instance"].id,
            "branch": "",
        }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data["isError"] is True
        payload = json.loads(data["content"][0]["text"])
        assert payload["error"] == "INVALID_IDENTIFIER"

    async def test_drop_table_with_confirm_executes(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "DROP TABLE users",
            "instance_id": setup_data["instance"].id,
            "confirm": True,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False

    async def test_destructive_confirm_with_branch_executes_on_same_branch(
        self, client, auth_headers, setup_data
    ):
        gateway = SimpleNamespace(execute=AsyncMock(return_value={
            "columns": [],
            "rows": [],
            "row_count": 0,
            "truncated": False,
        }))

        with patch("server.mcp.tools.handlers.get_gateway", return_value=gateway):
            resp = await client.post("/mcp/rest/run_sql", json={
                "sql": "DROP TABLE users",
                "instance_id": setup_data["instance"].id,
                "branch": "br1",
                "confirm": True,
            }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        payload = json.loads(data["content"][0]["text"])
        assert payload["branch"] == "br1"
        assert gateway.execute.await_args.kwargs["branch"] == "br1"

    async def test_drop_database_always_blocked(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "DROP DATABASE mydb",
            "instance_id": setup_data["instance"].id,
            "confirm": True,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["isError"] is True
        content = json.loads(data["content"][0]["text"])
        assert content["error"] == "BLOCKED_SQL"

    async def test_bounded_delete_no_confirm_needed(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "DELETE FROM users WHERE id = 'x'",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False

    async def test_unbounded_delete_requires_confirm(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "DELETE FROM users",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is not True
        text = data["content"][0]["text"]
        assert "DESTRUCTIVE" in text

    async def test_truncate_requires_confirm(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "TRUNCATE TABLE users",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is not True
        text = data["content"][0]["text"]
        assert "DESTRUCTIVE" in text

    async def test_alter_requires_confirm(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "ALTER TABLE users ADD COLUMN x INT",
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is not True
        text = data["content"][0]["text"]
        assert "DESTRUCTIVE" in text

    async def test_confirm_true_on_first_call_bypasses_warning(self, client, auth_headers, setup_data):
        """confirm=true on first call skips the warning phase (advisory, not security)."""
        resp = await client.post("/mcp/rest/run_sql", json={
            "sql": "TRUNCATE TABLE users",
            "instance_id": setup_data["instance"].id,
            "confirm": True,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False


class TestRunSQLTransaction:
    async def test_transaction_success(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql_transaction", json={
            "sql_statements": ["INSERT INTO t VALUES (1)", "INSERT INTO t VALUES (2)"],
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False
        result = json.loads(data["content"][0]["text"])
        assert result["statement_count"] == 2

    async def test_transaction_sanitizes_unexpected_gateway_exception(
        self, client, auth_headers, setup_data, caplog
    ):
        sentinel = (
            "password=SECRET host=private db=secret_db "
            "SQL=INSERT SENTINEL"
        )
        gateway = SimpleNamespace(
            execute_transaction=AsyncMock(
                side_effect=RuntimeError(sentinel)
            )
        )
        caplog.set_level(logging.INFO)
        with patch(
            "server.mcp.tools.handlers.get_gateway",
            return_value=gateway,
        ):
            response = await client.post(
                "/mcp/rest/run_sql_transaction",
                json={
                    "sql_statements": ["INSERT INTO t VALUES ('SENTINEL')"],
                    "instance_id": setup_data["instance"].id,
                    "database": "secret_db",
                },
                headers=auth_headers,
            )
        assert response.status_code == 200
        body = response.json()
        assert body["isError"] is True
        assert json.loads(body["content"][0]["text"]) == {
            "error": "INTERNAL_ERROR",
            "message": "Internal SQL execution error",
        }
        assert sentinel not in caplog.text
        async with engine_mod._session_factory() as session:
            audit = (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.action == "run_sql_transaction")
                    .order_by(AuditLog.created_at.desc())
                )
            ).scalars().first()
        assert audit is not None
        assert audit.status == AuditStatus.ERROR
        assert audit.error_code == "INTERNAL_ERROR"
        assert sentinel not in (audit.metadata_json or "")
        assert "SENTINEL" not in (audit.metadata_json or "")
        assert "secret_db" not in (audit.metadata_json or "")

    async def test_transaction_rejects_empty_list_at_request_layer(
        self, client, auth_headers, setup_data,
    ):
        resp = await client.post("/mcp/rest/run_sql_transaction", json={
            "sql_statements": [],
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)

        assert resp.status_code == 422

    async def test_transaction_handler_rejects_empty_list_before_resolution(self, setup_data):
        from server.mcp.tools.handlers import handle_run_sql_transaction

        session = AsyncMock()
        with patch("server.mcp.tools.handlers.resolve_target_instance", new_callable=AsyncMock) as resolve:
            result = await handle_run_sql_transaction(
                setup_data["admin"], session, sql_statements=[],
            )

        resolve.assert_not_awaited()
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "error": "INVALID_ARGUMENT",
            "message": "sql_statements must contain at least one SQL statement.",
        }

    @pytest.mark.parametrize(
        "sql",
        ["", "   ", ";", " ; ; ", "/* comment */", "-- comment", "# comment", "/* comment */;"],
    )
    async def test_transaction_handler_rejects_empty_statement_before_resolution(
        self, setup_data, sql,
    ):
        from server.mcp.tools.handlers import handle_run_sql_transaction

        session = AsyncMock()
        with patch("server.mcp.tools.handlers.resolve_target_instance", new_callable=AsyncMock) as resolve:
            result = await handle_run_sql_transaction(
                setup_data["admin"], session, sql_statements=[sql],
            )

        resolve.assert_not_awaited()
        assert result["isError"] is True
        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "error": "INVALID_ARGUMENT",
            "message": "Each sql_statements item must contain exactly one SQL statement.",
        }

    async def test_transaction_rejects_multiple_statements_inside_item(
        self, client, auth_headers, setup_data,
    ):
        gateway = AsyncMock()

        with patch("server.mcp.tools.handlers.get_gateway", return_value=gateway):
            resp = await client.post("/mcp/rest/run_sql_transaction", json={
                "sql_statements": ["SELECT 1; INSERT INTO t VALUES (1)"],
                "instance_id": setup_data["instance"].id,
            }, headers=auth_headers)

        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is True
        payload = json.loads(data["content"][0]["text"])
        assert payload == {
            "error": "INVALID_ARGUMENT",
            "message": "Each sql_statements item must contain exactly one SQL statement.",
        }
        gateway.execute_transaction.assert_not_awaited()

    async def test_blocked_statement_rejects_transaction(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql_transaction", json={
            "sql_statements": ["SELECT 1", "DROP DATABASE mydb"],
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("isError") is True
        content = json.loads(data["content"][0]["text"])
        assert content["error"] == "BLOCKED_SQL"
        assert "Statement 2" in content["message"]

    async def test_destructive_without_confirm_warns(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql_transaction", json={
            "sql_statements": ["INSERT INTO t VALUES (1)", "DROP TABLE t"],
            "instance_id": setup_data["instance"].id,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        text = data["content"][0]["text"]
        assert "DESTRUCTIVE" in text
        assert "confirm" in text.lower()

    async def test_destructive_with_confirm_executes(self, client, auth_headers, setup_data):
        resp = await client.post("/mcp/rest/run_sql_transaction", json={
            "sql_statements": ["INSERT INTO t VALUES (1)", "DROP TABLE t"],
            "instance_id": setup_data["instance"].id,
            "confirm": True,
        }, headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "isError" not in data or data["isError"] is False


class TestMCPErrorCodes:
    async def test_auth_required(self, client):
        resp = await client.post("/mcp/rest/run_sql", json={"sql": "SELECT 1"})
        assert resp.status_code in (401, 403)
