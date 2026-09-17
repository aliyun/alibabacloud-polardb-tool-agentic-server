from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from server.api.configuration import router
from server.configuration.types import ConfigAction
from server.models import ConfigBootstrapClaim, User
from tests._configuration_helpers import create_config_context


@pytest.fixture
async def api_context():
    context = await create_config_context()
    app = FastAPI()
    app.state.config_service = context.service
    app.include_router(router, prefix="/api")
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield context, client
    await context.close()


async def test_bootstrap_authorized_describe_is_no_store(
    api_context,
) -> None:
    context, client = api_context
    claim = await context.repository.get_bootstrap_claim()
    assert claim is not None
    # initialize_configuration returns plaintext only once; issue a known
    # replacement solely for this API fixture.
    from server.configuration.bootstrap import rotate_bootstrap_token

    token = await rotate_bootstrap_token(context.repository)
    response = await client.post(
        "/api/config",
        headers={"Authorization": f"Bootstrap {token}"},
        json={
            "protocol_version": 1,
            "action": "describe",
            "module": "token_security",
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["system_state"] == "SETUP"
    assert body["module"]["effective"]["config"]["private_key"] == {"configured": True}


async def test_polarrag_limits_schema_is_independent_and_described(
    api_context,
) -> None:
    context, client = api_context
    from server.configuration.bootstrap import rotate_bootstrap_token

    token = await rotate_bootstrap_token(context.repository)
    response = await client.post(
        "/api/config",
        headers={"Authorization": f"Bootstrap {token}"},
        json={
            "protocol_version": 1,
            "action": "describe",
            "module": "polarrag_tool_limits",
        },
    )

    assert response.status_code == 200
    module = response.json()["module"]
    assert module["name"] == "polarrag_tool_limits"
    assert set(module["schema"]["properties"]) == {
        "enabled",
        "user_requests_per_minute",
        "user_burst",
        "agent_requests_per_minute",
        "agent_burst",
        "instance_max_inflight",
        "max_fanout",
        "max_exhaustive_knowledge_resources",
        "retry_after_seconds",
        "upstream_request_timeout_ms",
    }
    assert module["schema"]["properties"]["max_fanout"]["minimum"] == 1
    assert module["schema"]["properties"]["max_fanout"]["maximum"] == 1000
    assert (
        module["schema"]["properties"]["max_exhaustive_knowledge_resources"]
        ["maximum"]
        == 10000
    )
    assert module["schema"]["properties"]["upstream_request_timeout_ms"]["minimum"] == 100
    assert module["schema"]["properties"]["upstream_request_timeout_ms"]["maximum"] == 300000
    assert "replica" in module["ui_hints"]["local_limit_semantics"]


async def test_polarrag_limits_validation_blocks_unbounded_integer(
    api_context,
) -> None:
    context, client = api_context
    from server.configuration.bootstrap import rotate_bootstrap_token

    token = await rotate_bootstrap_token(context.repository)
    document = await context.repository.get_module("polarrag_tool_limits")
    assert document is not None
    saved = await client.post(
        "/api/config",
        headers={"Authorization": f"Bootstrap {token}"},
        json={
            "protocol_version": 1,
            "action": "save_draft",
            "module": "polarrag_tool_limits",
            "expected_revision": document.revision,
            "config": {"user_burst": 10**1000},
        },
    )

    assert saved.status_code == 200
    validated = await client.post(
        "/api/config",
        headers={"Authorization": f"Bootstrap {token}"},
        json={
            "protocol_version": 1,
            "action": "validate",
            "module": "polarrag_tool_limits",
            "expected_revision": saved.json()["module"]["revision"],
        },
    )

    assert validated.status_code == 409
    assert validated.json()["detail"]["code"] == "INVALID_MODULE_CONFIG"
    rejected = await context.repository.get_module("polarrag_tool_limits")
    assert rejected is not None
    assert rejected.last_error_code == "INVALID_MODULE_CONFIG"
    assert rejected.effective is not None
    assert rejected.effective.config["user_burst"] == 10


async def test_bootstrap_token_is_required_in_setup(api_context) -> None:
    _, client = api_context
    response = await client.post(
        "/api/config",
        json={"protocol_version": 1, "action": "describe"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "BOOTSTRAP_REQUIRED"


async def test_mutation_requires_expected_revision(api_context) -> None:
    context, client = api_context
    from server.configuration.bootstrap import rotate_bootstrap_token

    token = await rotate_bootstrap_token(context.repository)
    response = await client.post(
        "/api/config",
        headers={"Authorization": f"Bootstrap {token}"},
        json={
            "protocol_version": 1,
            "action": ConfigAction.SAVE_DRAFT,
            "module": "observability",
            "config": {"log_level": "debug"},
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "EXPECTED_REVISION_REQUIRED"
    )


async def test_side_effect_requires_idempotency_key(api_context) -> None:
    context, client = api_context
    from server.configuration.bootstrap import rotate_bootstrap_token

    token = await rotate_bootstrap_token(context.repository)
    response = await client.post(
        "/api/config",
        headers={"Authorization": f"Bootstrap {token}"},
        json={
            "protocol_version": 1,
            "action": "activate",
            "module": "aliyun_access",
            "expected_revision": 0,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == (
        "IDEMPOTENCY_KEY_REQUIRED"
    )


async def test_request_larger_than_one_mib_is_rejected(
    api_context,
) -> None:
    context, client = api_context
    from server.configuration.bootstrap import rotate_bootstrap_token

    token = await rotate_bootstrap_token(context.repository)
    response = await client.post(
        "/api/config",
        headers={"Authorization": f"Bootstrap {token}"},
        json={
            "protocol_version": 1,
            "action": "plan",
            "module": "runtime_policy",
            "config": {"padding": "x" * 1_048_576},
        },
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "CONFIG_TOO_LARGE"


async def test_core_admin_activation_is_atomic_and_consumes_claim(
    api_context,
) -> None:
    context, client = api_context
    from server.configuration.bootstrap import rotate_bootstrap_token

    token = await rotate_bootstrap_token(context.repository)
    headers = {"Authorization": f"Bootstrap {token}"}
    saved = await client.post(
        "/api/config",
        headers=headers,
        json={
            "protocol_version": 1,
            "action": "save_draft",
            "module": "core_admin",
            "expected_revision": 0,
            "config": {"username": "breakglass"},
        },
    )
    assert saved.status_code == 200
    validated = await client.post(
        "/api/config",
        headers=headers,
        json={
            "protocol_version": 1,
            "action": "validate",
            "module": "core_admin",
            "expected_revision": saved.json()["module"]["revision"],
        },
    )
    assert validated.status_code == 200
    activated = await client.post(
        "/api/config",
        headers=headers,
        json={
            "protocol_version": 1,
            "action": "activate",
            "module": "core_admin",
            "expected_revision": validated.json()["module"]["revision"],
            "validation_id": validated.json()["validation"][
                "validation_id"
            ],
            "idempotency_key": "create-breakglass",
            "config": {"password": "correct horse battery staple"},
        },
    )
    assert activated.status_code == 200
    assert activated.json()["system_state"] == "READY"

    async with context.repository.session_factory() as session:
        users = (
            await session.execute(
                select(User).where(User.external_id == "breakglass")
            )
        ).scalars().all()
        claim = await session.get(ConfigBootstrapClaim, "bootstrap")
    assert len(users) == 1
    assert users[0].password_hash != "correct horse battery staple"
    assert claim.consumed_at is not None
