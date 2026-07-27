from __future__ import annotations

import base64
import json
import os

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth.jwt_manager import create_access_token, reset_keys
from server.config import reset_config
from server.core.agent_token_service import get_or_create_token
from server.core.crypto import encrypt
from server.db import engine as engine_mod
from server.mcp.transport import mcp_lifespan, reset_mcp
from server.models import (
    Agent,
    AgentProvisioningBinding,
    Base,
    CredentialCapability,
    CredentialPurpose,
    CredentialStatus,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceStatus,
    InstanceTopology,
    ProvisioningBackend,
    ProvisioningBackendHealth,
    User,
)
from server.models.base import utc_now
from tests._helpers import init_test_jwt_keys

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


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


async def _call(
    client: AsyncClient,
    token: str,
    name: str,
    arguments: dict,
    req_id: int = 1,
) -> dict:
    response = await client.post(
        "/mcp",
        json=_jsonrpc(
            "tools/call",
            {"name": name, "arguments": arguments},
            req_id,
        ),
        headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    event = _parse_sse(response.text)
    assert "error" not in event, event
    return event["result"]


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


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
async def tool_setup():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    engine_mod._engine = engine
    engine_mod._session_factory = factory
    async with factory() as session:
        admin = User(external_id="tool-admin", display_name="Tool Admin")
        owner = Agent(name="tool-owner", max_active_resources=10)
        other = Agent(name="tool-other", max_active_resources=10)
        instance = Instance(
            cluster_id="pc-tool-backend",
            name="Tool Backend",
            topology=InstanceTopology.MULTITENANT,
            status=InstanceStatus.ACTIVE,
            host="tool.internal",
            port=3306,
        )
        session.add_all([admin, owner, other, instance])
        await session.flush()
        credential = InstanceCredential(
            instance_id=instance.id,
            name="tool-admin",
            purpose=CredentialPurpose.PROVISIONING_ADMIN,
            capability=CredentialCapability.ADMIN,
            username_ciphertext=encrypt("admin"),
            password_ciphertext=encrypt("secret"),
            created_by_user_id=admin.id,
        )
        session.add(credential)
        await session.flush()
        backend = ProvisioningBackend(
            instance_id=instance.id,
            admin_credential_id=credential.id,
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
                AgentProvisioningBinding(
                    agent_id=owner.id,
                    backend_id=backend.id,
                    created_by_user_id=admin.id,
                ),
                AgentProvisioningBinding(
                    agent_id=other.id,
                    backend_id=backend.id,
                    created_by_user_id=admin.id,
                ),
            ]
        )
        _, owner_token = await get_or_create_token(session, owner.id, None)
        _, other_token = await get_or_create_token(session, other.id, None)
        await session.commit()
        result = {
            "factory": factory,
            "owner_id": owner.id,
            "owner_token": owner_token,
            "other_token": other_token,
            "backend_id": backend.id,
            "rest_token": create_access_token(
                {"sub": admin.id, "role": "admin"}
            ),
        }
    yield result
    await engine.dispose()


@pytest.fixture
async def client(tool_setup):
    app = create_app()
    async with mcp_lifespan():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as value:
            yield value


async def test_create_list_describe_and_delete_resource(
    client, tool_setup
):
    created = await _call(
        client,
        tool_setup["owner_token"],
        "create_db_instance",
        {
            "client_token": "deploy-1",
            "db_type": "polardb_mysql",
            "name": "Orders",
        },
    )
    assert created["isError"] is False
    created_payload = _payload(created)
    assert created_payload["status"] == "CREATING"
    resource_id = created_payload["db_instance_id"]
    assert "password" not in created_payload

    listed = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "list_db_instances",
            {},
            req_id=2,
        )
    )
    assert [item["db_instance_id"] for item in listed["instances"]] == [
        resource_id
    ]
    assert listed["instances"][0]["usage"] is None
    assert listed["has_more"] is False

    async with tool_setup["factory"]() as session:
        resource = await session.get(DBInstanceResource, resource_id)
        resource.status = DBInstanceStatus.READY
        await session.commit()

    described = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "describe_db_instance",
            {"db_instance_id": resource_id},
            req_id=3,
        )
    )
    assert described["status"] == "READY"
    assert described["usage"] is None
    assert described["host"] == "tool.internal"
    assert described["password"]

    deleted = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "delete_db_instance",
            {"db_instance_id": resource_id},
            req_id=4,
        )
    )
    assert deleted == {
        "db_instance_id": resource_id,
        "name": "Orders",
        "db_type": "polardb_mysql",
        "source": "provisioned",
        "status": "DELETING",
    }


async def test_idempotency_and_stable_validation_errors(client, tool_setup):
    arguments = {
        "client_token": "same-token",
        "db_type": "polardb_mysql",
    }
    first = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "create_db_instance",
            arguments,
        )
    )
    second = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "create_db_instance",
            arguments,
            req_id=2,
        )
    )
    assert first["db_instance_id"] == second["db_instance_id"]

    invalid = await _call(
        client,
        tool_setup["owner_token"],
        "create_db_instance",
        {
            "client_token": "contains space",
            "db_type": "polardb_mysql",
        },
        req_id=3,
    )
    assert invalid["isError"] is True
    assert _payload(invalid)["error"] == "INVALID_CLIENT_TOKEN"


async def test_other_agent_cannot_describe_or_delete_owned_resource(
    client, tool_setup
):
    created = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "create_db_instance",
            {
                "client_token": "private",
                "db_type": "polardb_mysql",
            },
        )
    )
    for index, tool_name in enumerate(
        ("describe_db_instance", "delete_db_instance"), start=2
    ):
        result = await _call(
            client,
            tool_setup["other_token"],
            tool_name,
            {"db_instance_id": created["db_instance_id"]},
            req_id=index,
        )
        assert result["isError"] is True
        assert _payload(result)["error"] == "DB_INSTANCE_NOT_FOUND"


@pytest.mark.parametrize("mutation", ("revoked", "missing"))
async def test_invalid_resource_credential_returns_base_metadata(
    client, tool_setup, mutation
):
    created = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "create_db_instance",
            {
                "client_token": f"invalid-resource-{mutation}",
                "db_type": "polardb_mysql",
            },
        )
    )
    async with tool_setup["factory"]() as session:
        from sqlalchemy import select

        resource = await session.get(
            DBInstanceResource, created["db_instance_id"]
        )
        assert resource is not None
        resource.status = DBInstanceStatus.READY
        credential = (
            await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.resource_id == resource.id
                )
            )
        ).scalar_one()
        if mutation == "revoked":
            credential.status = CredentialStatus.REVOKED
        elif mutation == "missing":
            await session.delete(credential)
        await session.commit()

    described = await _call(
        client,
        tool_setup["owner_token"],
        "describe_db_instance",
        {"db_instance_id": created["db_instance_id"]},
        req_id=2,
    )
    assert described["isError"] is False
    payload = _payload(described)
    assert payload["db_instance_id"] == created["db_instance_id"]
    assert payload["capabilities"] == ["list", "describe", "delete"]
    assert not {
        "host",
        "port",
        "database",
        "username",
        "password",
    } & set(payload)
    listed = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "list_db_instances",
            {},
            req_id=3,
        )
    )
    listed_resource = next(
        item
        for item in listed["instances"]
        if item["db_instance_id"] == created["db_instance_id"]
    )
    assert listed_resource["capabilities"] == payload["capabilities"]


async def test_ambiguous_resource_credential_returns_base_metadata(
    client, tool_setup, monkeypatch
):
    created = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "create_db_instance",
            {
                "client_token": "ambiguous-resource-contract",
                "db_type": "polardb_mysql",
            },
        )
    )
    async with tool_setup["factory"]() as session:
        resource = await session.get(
            DBInstanceResource, created["db_instance_id"]
        )
        assert resource is not None
        resource.status = DBInstanceStatus.READY
        await session.commit()

    monkeypatch.setattr(
        "server.core.db_instance_contract."
        "usable_resource_access_credential",
        lambda _resource: None,
    )
    described = await _call(
        client,
        tool_setup["owner_token"],
        "describe_db_instance",
        {"db_instance_id": created["db_instance_id"]},
        req_id=2,
    )
    assert described["isError"] is False
    payload = _payload(described)
    assert payload["db_instance_id"] == created["db_instance_id"]
    assert payload["capabilities"] == ["list", "describe", "delete"]
    assert not {
        "host",
        "port",
        "database",
        "username",
        "password",
    } & set(payload)
    listed = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "list_db_instances",
            {},
            req_id=3,
        )
    )
    listed_resource = next(
        item
        for item in listed["instances"]
        if item["db_instance_id"] == created["db_instance_id"]
    )
    assert listed_resource["capabilities"] == payload["capabilities"]


@pytest.mark.parametrize(
    "ciphertext",
    (
        "",
        "not-base64",
        base64.b64encode(b"not-a-fernet-token").decode("ascii"),
    ),
)
async def test_corrupt_resource_ciphertext_returns_base_metadata(
    client, tool_setup, ciphertext
):
    created = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "create_db_instance",
            {
                "client_token": f"corrupt-{len(ciphertext)}",
                "db_type": "polardb_mysql",
            },
        )
    )
    async with tool_setup["factory"]() as session:
        from sqlalchemy import select

        resource = await session.get(
            DBInstanceResource, created["db_instance_id"]
        )
        assert resource is not None
        resource.status = DBInstanceStatus.READY
        credential = (
            await session.execute(
                select(InstanceCredential).where(
                    InstanceCredential.resource_id == resource.id
                )
            )
        ).scalar_one()
        credential.password_ciphertext = ciphertext
        await session.commit()

    result = await _call(
        client,
        tool_setup["owner_token"],
        "describe_db_instance",
        {"db_instance_id": created["db_instance_id"]},
        req_id=2,
    )
    assert result["isError"] is False
    payload = _payload(result)
    assert payload["db_instance_id"] == created["db_instance_id"]
    assert payload["status"] == "READY"
    assert payload["capabilities"] == ["list", "describe", "delete"]
    assert not {"host", "port", "database", "username", "password"} & set(
        payload
    )
    listed = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "list_db_instances",
            {},
            req_id=3,
        )
    )
    listed_resource = next(
        item
        for item in listed["instances"]
        if item["db_instance_id"] == created["db_instance_id"]
    )
    assert listed_resource["capabilities"] == payload["capabilities"]


async def test_resource_decrypt_exception_returns_base_metadata(
    client, tool_setup, monkeypatch
):
    created = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "create_db_instance",
            {
                "client_token": "decrypt-sentinel",
                "db_type": "polardb_mysql",
            },
        )
    )
    async with tool_setup["factory"]() as session:
        resource = await session.get(
            DBInstanceResource, created["db_instance_id"]
        )
        assert resource is not None
        resource.status = DBInstanceStatus.READY
        await session.commit()

    def fail_decrypt(_ciphertext):
        raise RuntimeError("decrypt sentinel with secret material")

    monkeypatch.setattr(
        "server.core.db_instance_contract.decrypt", fail_decrypt
    )
    result = await _call(
        client,
        tool_setup["owner_token"],
        "describe_db_instance",
        {"db_instance_id": created["db_instance_id"]},
        req_id=2,
    )
    assert result["isError"] is False
    payload = _payload(result)
    assert payload["db_instance_id"] == created["db_instance_id"]
    assert payload["status"] == "READY"
    assert payload["capabilities"] == ["list", "describe", "delete"]
    assert not {"host", "port", "database", "username", "password"} & set(
        payload
    )
    assert "secret material" not in result["content"][0]["text"]


async def test_call_reauthorizes_after_provisioning_binding_revocation(
    client, tool_setup
):
    existing_arguments = {
        "client_token": "before-revocation",
        "db_type": "polardb_mysql",
    }
    existing = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "create_db_instance",
            existing_arguments,
        )
    )
    async with tool_setup["factory"]() as session:
        from sqlalchemy import select

        binding = (
            await session.execute(
                select(AgentProvisioningBinding).where(
                    AgentProvisioningBinding.agent_id
                    == tool_setup["owner_id"]
                )
            )
        ).scalar_one()
        binding.enabled = False
        await session.commit()

    replay = _payload(
        await _call(
            client,
            tool_setup["owner_token"],
            "create_db_instance",
            existing_arguments,
            req_id=2,
        )
    )
    assert replay["db_instance_id"] == existing["db_instance_id"]

    result = await _call(
        client,
        tool_setup["owner_token"],
        "create_db_instance",
        {
            "client_token": "revoked",
            "db_type": "polardb_mysql",
        },
        req_id=3,
    )
    assert result["isError"] is True
    assert _payload(result)["error"] == "NO_PROVISIONING_BACKEND"


async def test_rest_list_instances_is_removed(client, tool_setup):
    response = await client.get(
        "/mcp/rest/list_instances",
        headers={
            "Authorization": f"Bearer {tool_setup['rest_token']}"
        },
    )
    assert response.status_code == 404
