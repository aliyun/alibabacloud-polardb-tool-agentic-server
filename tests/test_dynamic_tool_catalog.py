from __future__ import annotations

import base64
import json
import os
import time
import uuid
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt as jose_jwt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth.jwt_manager import _load_keys, reset_keys
from server.auth.principal import user_subject
from server.config import get_config, reset_config
from server.core.agent_instance_access_service import (
    AgentInstanceAccessCapability,
    upsert_agent_instance_access,
)
from server.core.agent_token_service import get_or_create_token
from server.core.crypto import encrypt
from server.db import engine as engine_mod
from server.mcp.transport import mcp_lifespan, reset_mcp
from server.mcp.tools import set_gateway
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentInstanceBindingCapability,
    AllocationMode,
    AuditLog,
    AuditStatus,
    AuthProvider,
    Base,
    BindingCapability,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceStatus,
    InstanceTopology,
    Permission,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    ProvisioningCapacity,
    User,
    UserInstanceBinding,
    UserInstanceBindingCapability,
)
from server.models.base import utc_now
from tests._helpers import init_test_jwt_keys

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
DB_TOOLS = {
    "list_db_instances",
    "create_db_instance",
    "describe_db_instance",
    "delete_db_instance",
}
AGENT_SQL_TOOLS = {
    "run_sql",
    "run_sql_transaction",
    "describe_schema",
}
USER_ONLY_TOOLS = {
    "set_default_instance",
    "list_branches",
    "create_branch",
    "delete_branch",
}


def _user_token(user_id: str) -> str:
    private_key, _ = _load_keys()
    now = int(time.time())
    return jose_jwt.encode(
        {
            "sub": user_subject(user_id),
            "aud": f"{get_config().server.public_base_url}/mcp",
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


def _jsonrpc(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    message = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _parse_sse(text: str) -> dict:
    for block in text.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
    raise AssertionError(f"No SSE event: {text}")


async def _tools(client: AsyncClient, token: str) -> list[dict]:
    response = await client.post(
        "/mcp",
        json=_jsonrpc("tools/list"),
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    event = _parse_sse(response.text)
    return event["result"]["tools"]


async def _call_tool(
    client: AsyncClient,
    token: str,
    name: str,
    arguments: dict,
) -> dict:
    response = await client.post(
        "/mcp",
        json=_jsonrpc(
            "tools/call", {"name": name, "arguments": arguments}
        ),
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    return _parse_sse(response.text)["result"]


class _AgentSQLGateway:
    def __init__(self):
        self.execute_kwargs = None
        self.transaction_kwargs = None
        self.schema_kwargs = None

    async def execute(self, **kwargs):
        self.execute_kwargs = kwargs
        return {
            "columns": ["value"],
            "rows": [[1]],
            "row_count": 1,
            "truncated": False,
        }

    async def execute_transaction(self, **kwargs):
        self.transaction_kwargs = kwargs
        return [
            {
                "columns": ["value"],
                "rows": [[1]],
                "row_count": 1,
                "truncated": False,
            }
        ]

    async def execute_parameterized(self, **kwargs):
        self.schema_kwargs = kwargs
        return {
            "columns": [
                "TABLE_NAME",
                "TABLE_COMMENT",
                "TABLE_ROWS",
                "CREATE_TIME",
            ],
            "rows": [["orders", "Orders", 1, None]],
        }


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY",
        base64.b64encode(os.urandom(32)).decode("ascii"),
    )
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    reset_mcp()
    yield
    reset_keys()
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()


@pytest.fixture
async def catalog_setup():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    engine_mod._engine = engine
    engine_mod._session_factory = factory

    async with factory() as session:
        admin = User(
            external_id="catalog-admin",
            display_name="Catalog Admin",
            auth_provider=AuthProvider.BUILTIN,
        )
        ungranted = User(
            external_id="catalog-ungranted",
            display_name="Ungraded User",
            auth_provider=AuthProvider.BUILTIN,
        )
        granted = User(
            external_id="catalog-granted",
            display_name="Granted User",
            auth_provider=AuthProvider.BUILTIN,
        )
        provisioning_agent = Agent(name="catalog-provisioning-agent")
        direct_agent = Agent(name="catalog-direct-agent")
        owner_agent = Agent(name="catalog-owner-agent")
        backend_instance = Instance(
            cluster_id="pc-catalog-backend",
            name="Catalog Backend",
            topology=InstanceTopology.MULTITENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="backend.internal",
            port=3306,
        )
        direct_instance = Instance(
            cluster_id="pc-catalog-direct",
            name="Catalog Direct",
            usage="Finance reporting",
            topology=InstanceTopology.SINGLE_TENANT,
            allocation_mode=AllocationMode.REGISTERED,
            status=InstanceStatus.ACTIVE,
            host="direct.internal",
            port=3306,
        )
        session.add_all(
            [
                admin,
                ungranted,
                granted,
                provisioning_agent,
                direct_agent,
                owner_agent,
                backend_instance,
                direct_instance,
            ]
        )
        await session.flush()

        backend_credential = InstanceCredential(
            instance_id=backend_instance.id,
            name="catalog-provisioning-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("provisioning-admin"),
            password_ciphertext=encrypt("provisioning-password"),
            created_by_user_id=admin.id,
        )
        direct_credential = InstanceCredential(
            instance_id=direct_instance.id,
            name="catalog-direct-access",
            purpose=CredentialPurpose.DIRECT_ACCESS,
            capability=CredentialCapability.READWRITE,
            username_ciphertext=encrypt("direct-user"),
            password_ciphertext=encrypt("direct-password"),
            database_name="app",
            created_by_user_id=admin.id,
        )
        session.add_all([backend_credential, direct_credential])
        await session.flush()

        backend = ProvisioningBackend(
            instance_id=backend_instance.id,
            admin_credential_id=backend_credential.id,
            max_active_resources=20,
        )
        session.add(backend)
        await session.flush()
        session.add_all(
            [
                ProvisioningBackendHealth(
                    backend_id=backend.id,
                    healthy=True,
                    checked_at=utc_now(),
                ),
            ]
        )
        await session.flush()
        await upsert_agent_instance_access(
            session,
            agent_id=provisioning_agent.id,
            instance_id=backend_instance.id,
            credential_id=None,
            permission=None,
            direct_enabled=None,
            capabilities={
                AgentInstanceAccessCapability.DB_INSTANCE_CREATE
            },
            admin_id=admin.id,
            require_existing=False,
        )

        user_binding = UserInstanceBinding(
            user_id=granted.id,
            instance_id=direct_instance.id,
            credential_id=direct_credential.id,
            permission=Permission.READWRITE,
            capabilities=[
                UserInstanceBindingCapability(
                    capability=BindingCapability.DB_INSTANCE_CREDENTIALS_READ
                )
            ],
        )
        direct_binding = AgentInstanceBinding(
            agent_id=direct_agent.id,
            instance_id=direct_instance.id,
            credential_id=direct_credential.id,
            permission=Permission.READWRITE,
            created_by_user_id=admin.id,
            capabilities=[
                AgentInstanceBindingCapability(
                    capability=BindingCapability.DB_INSTANCE_DESCRIBE
                ),
                AgentInstanceBindingCapability(
                    capability=BindingCapability.SQL_READ
                ),
                AgentInstanceBindingCapability(
                    capability=BindingCapability.SQL_WRITE
                ),
            ],
        )
        session.add_all([user_binding, direct_binding])
        await session.flush()

        resource = DBInstanceResource(
            owner_agent_id=owner_agent.id,
            backend_id=backend.id,
            client_token="owned-history",
            request_fingerprint="a" * 64,
            name="Owned resource",
            status=DBInstanceStatus.READY,
            database_name="owned_db",
        )
        session.add(resource)
        await session.flush()
        session.add(
            InstanceCredential(
                resource_id=resource.id,
                name="owned-access",
                purpose=CredentialPurpose.RESOURCE_ACCESS,
                capability=CredentialCapability.READWRITE,
                username_ciphertext=encrypt("owned-user"),
                password_ciphertext=encrypt("owned-password"),
                database_name="owned_db",
            )
        )

        _, provisioning_token = await get_or_create_token(
            session, provisioning_agent.id, None
        )
        _, direct_token = await get_or_create_token(
            session, direct_agent.id, None
        )
        _, owner_token = await get_or_create_token(
            session, owner_agent.id, None
        )
        await session.commit()
        result = {
            "factory": factory,
            "ungranted_token": _user_token(ungranted.id),
            "granted_token": _user_token(granted.id),
            "granted_binding_id": user_binding.id,
            "direct_instance_id": direct_instance.id,
            "provisioning_token": provisioning_token,
            "provisioning_agent_id": provisioning_agent.id,
            "backend_credential_id": backend_credential.id,
            "direct_token": direct_token,
            "direct_binding_id": direct_binding.id,
            "owner_token": owner_token,
            "owner_agent_id": owner_agent.id,
            "owner_resource_id": resource.id,
        }
    yield result
    await engine.dispose()


@pytest.fixture
async def client(catalog_setup):
    app = create_app()
    async with mcp_lifespan():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as value:
            yield value


async def test_ungranted_user_does_not_see_database_instance_tools(
    client, catalog_setup
):
    names = {
        tool["name"]
        for tool in await _tools(client, catalog_setup["ungranted_token"])
    }
    assert not DB_TOOLS & names
    assert "list_instances" not in names


async def test_granted_user_sees_only_list_and_describe(
    client, catalog_setup
):
    names = {
        tool["name"]
        for tool in await _tools(client, catalog_setup["granted_token"])
    }
    assert names & DB_TOOLS == {
        "list_db_instances",
        "describe_db_instance",
    }


async def test_provisioning_agent_sees_all_four_tools_and_strict_schemas(
    client, catalog_setup
):
    tools = await _tools(client, catalog_setup["provisioning_token"])
    by_name = {tool["name"]: tool for tool in tools}
    assert DB_TOOLS <= set(by_name)
    assert "run_sql" not in by_name
    assert "describe_schema" not in by_name
    create_schema = by_name["create_db_instance"]["inputSchema"]
    assert create_schema["required"] == ["client_token", "db_type"]
    assert set(create_schema["properties"]) == {
        "client_token",
        "name",
        "db_type",
    }
    assert create_schema["additionalProperties"] is False
    assert create_schema["properties"]["db_type"]["enum"] == [
        "polardb_mysql"
    ]
    for name in DB_TOOLS:
        assert by_name[name]["inputSchema"]["additionalProperties"] is False


async def test_direct_agent_and_resource_owner_get_distinct_catalogs(
    client, catalog_setup
):
    direct_tools = await _tools(client, catalog_setup["direct_token"])
    owner_tools = await _tools(client, catalog_setup["owner_token"])
    direct_names = {tool["name"] for tool in direct_tools}
    owner_names = {tool["name"] for tool in owner_tools}
    assert direct_names & DB_TOOLS == {
        "list_db_instances",
        "describe_db_instance",
    }
    assert AGENT_SQL_TOOLS <= direct_names
    assert USER_ONLY_TOOLS.isdisjoint(direct_names)
    assert owner_names & DB_TOOLS == {
        "list_db_instances",
        "describe_db_instance",
        "delete_db_instance",
    }
    assert AGENT_SQL_TOOLS <= owner_names
    assert USER_ONLY_TOOLS.isdisjoint(owner_names)
    by_name = {tool["name"]: tool for tool in owner_tools}
    assert set(by_name["run_sql"]["inputSchema"]["required"]) == {
        "sql",
        "instance_id",
    }
    assert "branch" not in by_name["run_sql"]["inputSchema"]["properties"]
    assert set(
        by_name["run_sql_transaction"]["inputSchema"]["required"]
    ) == {"sql_statements", "instance_id"}
    assert by_name["describe_schema"]["inputSchema"]["required"] == [
        "instance_id"
    ]


async def test_agent_sql_access_error_preserves_mcp_error_flag(
    client,
    catalog_setup,
):
    result = await _call_tool(
        client,
        catalog_setup["owner_token"],
        "run_sql",
        {
            "instance_id": "dbi-not-owned",
            "sql": "SELECT 1",
        },
    )

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INSTANCE_NOT_ACCESSIBLE"
    assert "Call list_db_instances" in payload["message"]


async def test_disabling_direct_binding_removes_agent_sql_tools(
    client, catalog_setup
):
    assert AGENT_SQL_TOOLS <= {
        tool["name"]
        for tool in await _tools(
            client,
            catalog_setup["direct_token"],
        )
    }

    async with catalog_setup["factory"]() as session:
        binding = await session.get(
            AgentInstanceBinding,
            catalog_setup["direct_binding_id"],
        )
        assert binding is not None
        binding.enabled = False
        await session.commit()

    names = {
        tool["name"]
        for tool in await _tools(
            client,
            catalog_setup["direct_token"],
        )
    }
    assert AGENT_SQL_TOOLS.isdisjoint(names)


async def test_agent_reuses_list_id_for_describe_and_sql_tools(
    client,
    catalog_setup,
):
    listed = await _call_tool(
        client,
        catalog_setup["direct_token"],
        "list_db_instances",
        {},
    )
    list_payload = json.loads(listed["content"][0]["text"])
    db_instance_id = list_payload["instances"][0]["db_instance_id"]
    assert db_instance_id == catalog_setup["direct_instance_id"]
    assert list_payload["instances"][0]["usage"] == "Finance reporting"

    described = await _call_tool(
        client,
        catalog_setup["direct_token"],
        "describe_db_instance",
        {"db_instance_id": db_instance_id},
    )
    assert described.get("isError") is not True
    describe_payload = json.loads(described["content"][0]["text"])
    assert describe_payload["usage"] == "Finance reporting"

    gateway = _AgentSQLGateway()
    set_gateway(gateway)
    run = await _call_tool(
        client,
        catalog_setup["direct_token"],
        "run_sql",
        {"sql": "SELECT 1", "instance_id": db_instance_id},
    )
    run_payload = json.loads(run["content"][0]["text"])
    assert run_payload["permission"] == "readwrite"
    assert gateway.execute_kwargs["instance_id"] == db_instance_id

    transaction = await _call_tool(
        client,
        catalog_setup["direct_token"],
        "run_sql_transaction",
        {
            "sql_statements": ["SELECT 1"],
            "instance_id": db_instance_id,
        },
    )
    assert transaction.get("isError") is not True
    assert gateway.transaction_kwargs["instance_id"] == db_instance_id

    schema = await _call_tool(
        client,
        catalog_setup["direct_token"],
        "describe_schema",
        {
            "instance_id": db_instance_id,
            "include_columns": False,
        },
    )
    schema_payload = json.loads(schema["content"][0]["text"])
    assert schema_payload["tables"][0]["table_name"] == "orders"
    assert gateway.schema_kwargs["instance_id"] == db_instance_id


async def test_agent_sql_tools_require_instance_and_reject_branch(
    client,
    catalog_setup,
):
    missing = await _call_tool(
        client,
        catalog_setup["direct_token"],
        "run_sql",
        {"sql": "SELECT 1"},
    )
    assert json.loads(missing["content"][0]["text"])["error"] == (
        "INVALID_ARGUMENT"
    )

    branch = await _call_tool(
        client,
        catalog_setup["direct_token"],
        "run_sql",
        {
            "sql": "SELECT 1",
            "instance_id": catalog_setup["direct_instance_id"],
            "branch": "feature",
        },
    )
    assert json.loads(branch["content"][0]["text"])["error"] == (
        "INVALID_ARGUMENT"
    )


async def test_concurrent_principals_do_not_leak_catalogs(
    client, catalog_setup
):
    import asyncio

    ungranted, provisioning = await asyncio.gather(
        _tools(client, catalog_setup["ungranted_token"]),
        _tools(client, catalog_setup["provisioning_token"]),
    )
    assert not DB_TOOLS & {tool["name"] for tool in ungranted}
    assert DB_TOOLS <= {tool["name"] for tool in provisioning}


async def test_describe_reauthorizes_direct_binding_after_catalog_list(
    client, catalog_setup
):
    first = await _call_tool(
        client,
        catalog_setup["granted_token"],
        "describe_db_instance",
        {"db_instance_id": catalog_setup["direct_instance_id"]},
    )
    first_payload = json.loads(first["content"][0]["text"])
    assert first_payload["username"] == "direct-user"
    assert first_payload["password"] == "direct-password"

    async with catalog_setup["factory"]() as session:
        binding = await session.get(
            UserInstanceBinding,
            catalog_setup["granted_binding_id"],
        )
        assert binding is not None
        binding.enabled = False
        await session.commit()

    revoked = await _call_tool(
        client,
        catalog_setup["granted_token"],
        "describe_db_instance",
        {"db_instance_id": catalog_setup["direct_instance_id"]},
    )
    assert revoked["isError"] is True
    assert json.loads(revoked["content"][0]["text"])["error"] == (
        "DB_INSTANCE_NOT_FOUND"
    )


@pytest.mark.parametrize(
    "mutation",
    ("admin_capability", "missing", "empty_ciphertext", "revoked"),
)
async def test_invalid_direct_credential_never_lists_or_decrypts(
    client, catalog_setup, monkeypatch, mutation
):
    assert {
        "list_db_instances",
        "describe_db_instance",
    } <= {
        tool["name"]
        for tool in await _tools(
            client, catalog_setup["granted_token"]
        )
    }
    async with catalog_setup["factory"]() as session:
        binding = await session.get(
            UserInstanceBinding,
            catalog_setup["granted_binding_id"],
        )
        assert binding is not None
        credential = await session.get(
            InstanceCredential, binding.credential_id
        )
        assert credential is not None
        if mutation == "admin_capability":
            credential.capability = CredentialCapability.ADMIN
        elif mutation == "missing":
            binding.credential_id = None
        elif mutation == "empty_ciphertext":
            credential.password_ciphertext = ""
        else:
            credential.status = CredentialStatus.REVOKED
        await session.commit()

    decrypt_called = False

    def reject_decrypt(_ciphertext):
        nonlocal decrypt_called
        decrypt_called = True
        raise AssertionError("invalid direct credential was decrypted")

    monkeypatch.setattr(
        "server.mcp.tools.db_instance_handler.decrypt",
        reject_decrypt,
    )
    names = {
        tool["name"]
        for tool in await _tools(client, catalog_setup["granted_token"])
    }
    assert not {
        "list_db_instances",
        "describe_db_instance",
    } & names
    described = await _call_tool(
        client,
        catalog_setup["granted_token"],
        "describe_db_instance",
        {"db_instance_id": catalog_setup["direct_instance_id"]},
    )
    assert described["isError"] is True
    assert json.loads(described["content"][0]["text"])["error"] == (
        "DB_INSTANCE_NOT_FOUND"
    )
    assert decrypt_called is False


@pytest.mark.parametrize(
    "ciphertext",
    (
        "not-base64",
        base64.b64encode(b"not-a-fernet-token").decode("ascii"),
    ),
)
async def test_corrupt_physical_ciphertext_returns_stable_not_found(
    client, catalog_setup, ciphertext
):
    async with catalog_setup["factory"]() as session:
        binding = await session.get(
            UserInstanceBinding,
            catalog_setup["granted_binding_id"],
        )
        assert binding is not None
        credential = await session.get(
            InstanceCredential, binding.credential_id
        )
        assert credential is not None
        credential.password_ciphertext = ciphertext
        await session.commit()

    described = await _call_tool(
        client,
        catalog_setup["granted_token"],
        "describe_db_instance",
        {"db_instance_id": catalog_setup["direct_instance_id"]},
    )
    assert described["isError"] is True
    payload = json.loads(described["content"][0]["text"])
    assert payload == {
        "error": "DB_INSTANCE_NOT_FOUND",
        "message": "Database instance not found",
    }


async def test_physical_decrypt_exception_is_sanitized(
    client, catalog_setup, monkeypatch
):
    def fail_decrypt(_ciphertext):
        raise RuntimeError("physical decrypt sentinel with secret material")

    monkeypatch.setattr(
        "server.mcp.tools.db_instance_handler.decrypt", fail_decrypt
    )
    described = await _call_tool(
        client,
        catalog_setup["granted_token"],
        "describe_db_instance",
        {"db_instance_id": catalog_setup["direct_instance_id"]},
    )
    assert described["isError"] is True
    text = described["content"][0]["text"]
    assert json.loads(text)["error"] == "DB_INSTANCE_NOT_FOUND"
    assert "secret material" not in text


async def test_revoked_provisioning_credential_removes_create_and_call_fails(
    client, catalog_setup
):
    assert DB_TOOLS <= {
        tool["name"]
        for tool in await _tools(
            client, catalog_setup["provisioning_token"]
        )
    }
    async with catalog_setup["factory"]() as session:
        credential = await session.get(
            InstanceCredential,
            catalog_setup["backend_credential_id"],
        )
        assert credential is not None
        credential.status = CredentialStatus.REVOKED
        credential.username_ciphertext = None
        credential.password_ciphertext = None
        await session.commit()

    names = {
        tool["name"]
        for tool in await _tools(
            client, catalog_setup["provisioning_token"]
        )
    }
    assert not DB_TOOLS & names
    stale_call = await _call_tool(
        client,
        catalog_setup["provisioning_token"],
        "create_db_instance",
        {
            "client_token": "stale-catalog",
            "db_type": "polardb_mysql",
        },
    )
    assert stale_call["isError"] is True
    assert json.loads(stale_call["content"][0]["text"])["error"] == (
        "NO_PROVISIONING_BACKEND"
    )


async def test_agent_without_sql_binding_cannot_call_stale_sql_tool(
    client, catalog_setup
):
    result = await _call_tool(
        client,
        catalog_setup["provisioning_token"],
        "run_sql",
        {
            "sql": "SELECT 1",
            "instance_id": catalog_setup["direct_instance_id"],
        },
    )
    assert json.loads(result["content"][0]["text"])["error"] == (
        "INSTANCE_NOT_ACCESSIBLE"
    )


async def test_db_instance_tools_write_safe_traceable_audit_events(
    client, catalog_setup
):
    request_id = "trace-db-instance-audit"
    headers = {
        **MCP_HEADERS,
        "Authorization": (
            f"Bearer {catalog_setup['provisioning_token']}"
        ),
        "X-Request-ID": request_id,
    }

    async def call(name: str, arguments: dict) -> dict:
        response = await client.post(
            "/mcp",
            json=_jsonrpc(
                "tools/call",
                {"name": name, "arguments": arguments},
                req_id=93,
            ),
            headers=headers,
        )
        assert response.status_code == 200
        return _parse_sse(response.text)["result"]

    listed = await call("list_db_instances", {})
    assert listed.get("isError") is not True
    created = await call(
        "create_db_instance",
        {
            "client_token": "AUDIT_CLIENT_TOKEN_SENTINEL",
            "db_type": "polardb_mysql",
            "name": "Audit resource",
        },
    )
    resource_id = json.loads(created["content"][0]["text"])[
        "db_instance_id"
    ]
    replayed = await call(
        "create_db_instance",
        {
            "client_token": "AUDIT_CLIENT_TOKEN_SENTINEL",
            "db_type": "polardb_mysql",
            "name": "Audit resource",
        },
    )
    assert json.loads(replayed["content"][0]["text"])[
        "db_instance_id"
    ] == resource_id
    conflict = await call(
        "create_db_instance",
        {
            "client_token": "AUDIT_CLIENT_TOKEN_SENTINEL",
            "db_type": "polardb_mysql",
            "name": "Different request",
        },
    )
    assert conflict["isError"] is True
    assert json.loads(conflict["content"][0]["text"])["error"] == (
        "IDEMPOTENCY_CONFLICT"
    )
    described = await call(
        "describe_db_instance",
        {"db_instance_id": resource_id},
    )
    assert described.get("isError") is not True
    deleted = await call(
        "delete_db_instance",
        {"db_instance_id": resource_id},
    )
    assert deleted.get("isError") is not True
    deleted_replay = await call(
        "delete_db_instance",
        {"db_instance_id": resource_id},
    )
    assert deleted_replay.get("isError") is not True
    missing = await call(
        "describe_db_instance",
        {"db_instance_id": "missing-resource"},
    )
    assert missing["isError"] is True

    async with catalog_setup["factory"]() as session:
        rows = (
            await session.execute(
                select(AuditLog)
                .where(
                    AuditLog.action.in_(
                        {
                            "db_instance.list",
                            "db_instance.create",
                            "db_instance.describe",
                            "db_instance.delete",
                        }
                    )
                )
                .order_by(AuditLog.created_at, AuditLog.id)
            )
        ).scalars().all()

    assert {row.action for row in rows} == {
        "db_instance.list",
        "db_instance.create",
        "db_instance.describe",
        "db_instance.delete",
    }
    assert any(
        row.action == "db_instance.describe"
        and row.status == AuditStatus.ERROR
        and row.error_code == "DB_INSTANCE_NOT_FOUND"
        for row in rows
    )
    for row in rows:
        assert row.actor_agent_id == catalog_setup["provisioning_agent_id"]
        assert row.actor_user_id is None
        assert row.target_type == "db_instance"
        assert row.request_id == request_id
        assert row.duration_ms is not None
        assert row.duration_ms >= 0
        serialized = row.metadata_json or ""
        assert "AUDIT_CLIENT_TOKEN_SENTINEL" not in serialized
        assert "password" not in serialized.lower()
        assert "connection" not in serialized.lower()
    list_rows = [row for row in rows if row.action == "db_instance.list"]
    assert len(list_rows) == 1
    assert list_rows[0].target_id is None
    create_rows = [
        row for row in rows if row.action == "db_instance.create"
    ]
    assert len(create_rows) == 3
    assert all(row.target_id == resource_id for row in create_rows)
    assert all(row.instance_id is None for row in create_rows)
    assert [row.status for row in create_rows].count(
        AuditStatus.SUCCESS
    ) == 2
    assert any(
        row.status == AuditStatus.ERROR
        and row.error_code == "IDEMPOTENCY_CONFLICT"
        for row in create_rows
    )
    delete_rows = [
        row for row in rows if row.action == "db_instance.delete"
    ]
    assert len(delete_rows) == 2
    assert all(row.status == AuditStatus.SUCCESS for row in delete_rows)


@pytest.mark.parametrize(
    "untrusted_request_id",
    ["x" * 129, "contains spaces"],
)
async def test_db_instance_mutations_replace_untrusted_request_ids(
    client, catalog_setup, untrusted_request_id
):
    headers = {
        **MCP_HEADERS,
        "Authorization": (
            f"Bearer {catalog_setup['provisioning_token']}"
        ),
        "X-Request-ID": untrusted_request_id,
    }

    async def call(name: str, arguments: dict) -> tuple[dict, str]:
        response = await client.post(
            "/mcp",
            json=_jsonrpc(
                "tools/call",
                {"name": name, "arguments": arguments},
                req_id=94,
            ),
            headers=headers,
        )
        assert response.status_code == 200
        return (
            _parse_sse(response.text)["result"],
            response.headers["X-Request-ID"],
        )

    created, create_request_id = await call(
        "create_db_instance",
        {
            "client_token": f"safe-request-id-{len(untrusted_request_id)}",
            "db_type": "polardb_mysql",
        },
    )
    assert created.get("isError") is not True
    resource_id = json.loads(created["content"][0]["text"])[
        "db_instance_id"
    ]
    deleted, delete_request_id = await call(
        "delete_db_instance",
        {"db_instance_id": resource_id},
    )
    assert deleted.get("isError") is not True

    for request_id in (create_request_id, delete_request_id):
        assert request_id != untrusted_request_id
        assert request_id.isascii()
        assert 1 <= len(request_id) <= 128

    async with catalog_setup["factory"]() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.target_id == resource_id,
                    AuditLog.action.in_(
                        {"db_instance.create", "db_instance.delete"}
                    ),
                )
            )
        ).scalars().all()
    assert {row.action for row in rows} == {
        "db_instance.create",
        "db_instance.delete",
    }
    assert {row.request_id for row in rows} == {
        create_request_id,
        delete_request_id,
    }


async def test_required_create_audit_failure_rolls_back_resource_and_capacity(
    client, catalog_setup, monkeypatch
):
    async def fail_audit(*_args, **_kwargs):
        raise IntegrityError(
            "audit insert",
            {},
            RuntimeError("audit unavailable"),
        )

    monkeypatch.setattr(
        "server.mcp.tools.db_instance_handler.log_audit",
        fail_audit,
    )
    result = await _call_tool(
        client,
        catalog_setup["provisioning_token"],
        "create_db_instance",
        {
            "client_token": "audit-failure-create",
            "db_type": "polardb_mysql",
        },
    )
    assert result["isError"] is True
    async with catalog_setup["factory"]() as session:
        resource = (
            await session.execute(
                select(DBInstanceResource).where(
                    DBInstanceResource.client_token
                    == "audit-failure-create"
                )
            )
        ).scalar_one_or_none()
        capacity_counts = (
            await session.execute(
                select(ProvisioningCapacity.active_count)
            )
        ).scalars().all()
    assert resource is None
    assert all(count == 0 for count in capacity_counts)


async def test_required_delete_audit_failure_rolls_back_state(
    client, catalog_setup, monkeypatch
):
    async with catalog_setup["factory"]() as session:
        resource = await session.get(
            DBInstanceResource, catalog_setup["owner_resource_id"]
        )
        assert resource is not None
        resource.retry_count = 4
        resource.next_retry_at = utc_now() + timedelta(minutes=5)
        resource.failure_reason = "previous failure"
        resource.worker_id = "active-worker"
        resource.worker_lease_until = utc_now() + timedelta(minutes=5)
        await session.commit()
        expected_next_retry_at = resource.next_retry_at
        expected_worker_lease_until = resource.worker_lease_until

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(
        "server.mcp.tools.db_instance_handler.log_audit",
        fail_audit,
    )
    result = await _call_tool(
        client,
        catalog_setup["owner_token"],
        "delete_db_instance",
        {"db_instance_id": catalog_setup["owner_resource_id"]},
    )
    assert result["isError"] is True
    async with catalog_setup["factory"]() as session:
        resource = await session.get(
            DBInstanceResource, catalog_setup["owner_resource_id"]
        )
        assert resource is not None
        assert resource.status == DBInstanceStatus.READY
        assert resource.cleanup_required is False
        assert resource.retry_count == 4
        assert resource.next_retry_at == expected_next_retry_at.replace(
            tzinfo=None
        )
        assert resource.failure_reason == "previous failure"
        assert resource.worker_id == "active-worker"
        assert (
            resource.worker_lease_until
            == expected_worker_lease_until.replace(tzinfo=None)
        )


async def test_read_audit_failure_does_not_mask_list_result(
    client, catalog_setup, monkeypatch
):
    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable with sensitive details")

    monkeypatch.setattr(
        "server.mcp.tools.db_instance_handler.log_audit",
        fail_audit,
    )
    result = await _call_tool(
        client,
        catalog_setup["provisioning_token"],
        "list_db_instances",
        {},
    )
    assert result.get("isError") is not True
    payload = json.loads(result["content"][0]["text"])
    assert "instances" in payload


async def test_disabled_optional_audit_does_not_suppress_required_create(
    client, catalog_setup
):
    audit_config = get_config().sql_security.audit
    original_enabled = audit_config.enabled
    audit_config.enabled = False
    try:
        result = await _call_tool(
            client,
            catalog_setup["provisioning_token"],
            "create_db_instance",
            {
                "client_token": "required-while-disabled",
                "db_type": "polardb_mysql",
            },
        )
    finally:
        audit_config.enabled = original_enabled
    assert result.get("isError") is not True
    resource_id = json.loads(result["content"][0]["text"])[
        "db_instance_id"
    ]
    async with catalog_setup["factory"]() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "db_instance.create",
                    AuditLog.target_id == resource_id,
                )
            )
        ).scalar_one()
    assert audit.status == AuditStatus.SUCCESS
