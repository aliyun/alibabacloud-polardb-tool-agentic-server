from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth.builtin import hash_password
from server.auth.jwt_manager import create_access_token, reset_keys
from server.auth.oauth_provider import PASAuthProvider
from server.config import AppConfig, get_config, reset_config
from server.db import engine as engine_mod
from server.models import (
    Agent,
    AgentAPIToken,
    AgentStatus,
    AgentTokenRevealLimit,
    AuditLog,
    AuthProvider,
    Base,
    User,
    UserRole,
)
from tests._helpers import init_test_jwt_keys


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii")
    )
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    engine_mod.reset_engine()
    yield
    reset_keys()
    reset_config()
    engine_mod.reset_engine()


@pytest.fixture
async def setup():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    engine_mod._engine = engine
    engine_mod._session_factory = factory
    async with factory() as session:
        admin = User(
            external_id="admin",
            display_name="Admin",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.ADMIN,
        )
        member = User(
            external_id="member",
            display_name="Member",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password("password"),
            role=UserRole.MEMBER,
        )
        session.add_all([admin, member])
        await session.commit()
        await session.refresh(admin)
        await session.refresh(member)
    yield factory, admin, member
    await engine.dispose()


@pytest.fixture
async def client(setup):
    _, admin, member = setup
    app = create_app()
    admin_headers = {
        "Authorization": f"Bearer {create_access_token({'sub': admin.id, 'role': 'admin'})}"
    }
    member_headers = {
        "Authorization": f"Bearer {create_access_token({'sub': member.id, 'role': 'member'})}"
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http, admin_headers, member_headers


async def test_admin_crud_and_regenerate_reveal_revoke(client, setup, caplog):
    http, headers, _ = client
    factory, _, _ = setup
    created = await http.post(
        "/api/agents",
        json={
            "name": "prod-agent",
            "description": "production reporting",
            "max_active_resources": 3,
        },
        headers=headers,
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]
    assert created.json()["token"].startswith("pas_agent_")
    assert created.headers["cache-control"] == "no-store"
    initial_plaintext = created.json()["token"]
    initial_hash = hashlib.sha256(initial_plaintext.encode()).hexdigest()

    first = await http.post(
        f"/api/agents/{agent_id}/token/regenerate", headers=headers
    )
    second = await http.post(
        f"/api/agents/{agent_id}/token/regenerate", headers=headers
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["token"] != second.json()["token"]
    assert first.headers["cache-control"] == "no-store"
    first_hash = hashlib.sha256(first.json()["token"].encode()).hexdigest()
    second_hash = hashlib.sha256(second.json()["token"].encode()).hexdigest()
    async with factory() as session:
        active_row = (
            await session.execute(
                select(AgentAPIToken).where(AgentAPIToken.agent_id == agent_id)
            )
        ).scalar_one()
        assert active_row.token_ciphertext is not None
        second_ciphertext = active_row.token_ciphertext

    reveal = await http.post(
        f"/api/agents/{agent_id}/token/reveal",
        json={"confirmed": True},
        headers=headers,
    )
    assert reveal.status_code == 200
    assert reveal.headers["cache-control"] == "no-store"
    assert reveal.json()["token"] == second.json()["token"]

    revoked = await http.post(
        f"/api/agents/{agent_id}/token/revoke", headers=headers
    )
    assert revoked.status_code == 200
    denied = await http.post(
        f"/api/agents/{agent_id}/token/reveal",
        json={"confirmed": True},
        headers=headers,
    )
    assert denied.status_code == 409

    async with factory() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action.in_(
                        {
                            "agent_token.reveal",
                            "agent_token.regenerate",
                            "agent_token.revoke",
                        }
                    )
                )
            )
        ).scalars().all()
        assert len(rows) == 5
        serialized = json.dumps(
            [row.metadata_json for row in rows], ensure_ascii=False
        )
        assert first.json()["token"] not in serialized
        assert second.json()["token"] not in serialized
        assert initial_plaintext not in serialized
        assert initial_hash not in serialized
        assert first_hash not in serialized
        assert second_hash not in serialized
        assert second_ciphertext not in serialized
        log_text = caplog.text
        assert first.json()["token"] not in log_text
        assert second.json()["token"] not in log_text
        assert initial_plaintext not in log_text
        assert initial_hash not in log_text
        assert first_hash not in log_text
        assert second_hash not in log_text
        assert second_ciphertext not in log_text


async def test_reveal_is_rate_limited_per_admin_and_agent(client):
    http, headers, _ = client
    created = await http.post(
        "/api/agents", json={"name": "rate-agent"}, headers=headers
    )
    agent_id = created.json()["id"]
    await http.post(f"/api/agents/{agent_id}/token/regenerate", headers=headers)
    for _ in range(5):
        assert (
            await http.post(
                f"/api/agents/{agent_id}/token/reveal",
                json={"confirmed": True},
                headers=headers,
            )
        ).status_code == 200
    limited = await http.post(
        f"/api/agents/{agent_id}/token/reveal",
        json={"confirmed": True},
        headers=headers,
    )
    assert limited.status_code == 429


async def test_reveal_limit_is_shared_in_database_across_app_instances(setup):
    factory, admin, _ = setup
    headers = {
        "Authorization": f"Bearer {create_access_token({'sub': admin.id, 'role': 'admin'})}"
    }
    first_app = create_app()
    second_app = create_app()
    async with (
        AsyncClient(
            transport=ASGITransport(app=first_app), base_url="http://first"
        ) as first,
        AsyncClient(
            transport=ASGITransport(app=second_app), base_url="http://second"
        ) as second,
    ):
        created = await first.post(
            "/api/agents", json={"name": "shared-limit"}, headers=headers
        )
        agent_id = created.json()["id"]
        for index in range(5):
            client = first if index % 2 == 0 else second
            assert (
                await client.post(
                    f"/api/agents/{agent_id}/token/reveal",
                    json={"confirmed": True},
                    headers=headers,
                )
            ).status_code == 200
        assert (
            await second.post(
                f"/api/agents/{agent_id}/token/reveal",
                json={"confirmed": True},
                headers=headers,
            )
        ).status_code == 429

    async with factory() as session:
        limiter = (
            await session.execute(
                select(AgentTokenRevealLimit).where(
                    AgentTokenRevealLimit.admin_id == admin.id,
                    AgentTokenRevealLimit.agent_id == agent_id,
                )
            )
        ).scalar_one()
        assert limiter.request_count == 5


async def test_required_token_audit_ignores_optional_sql_audit_disable(client, setup):
    http, headers, _ = client
    factory, _, _ = setup
    get_config().sql_security.audit.enabled = False
    created = await http.post(
        "/api/agents", json={"name": "required-audit"}, headers=headers
    )
    assert created.status_code == 201
    assert created.json()["token"].startswith("pas_agent_")
    revealed = await http.post(
        f"/api/agents/{created.json()['id']}/token/reveal",
        json={"confirmed": True},
        headers=headers,
    )
    assert revealed.status_code == 200
    async with factory() as session:
        actions = set((await session.execute(select(AuditLog.action))).scalars())
    assert {
        "agent.create",
        "agent_token.regenerate",
        "agent_token.reveal",
    } <= actions


async def test_audit_failure_rolls_back_regeneration_and_keeps_old_token_valid(
    client, setup, monkeypatch
):
    http, headers, _ = client
    factory, _, _ = setup
    created = await http.post(
        "/api/agents", json={"name": "audit-rollback"}, headers=headers
    )
    agent_id = created.json()["id"]
    old_plaintext = created.json()["token"]
    provider = PASAuthProvider(
        session_factory=factory, config=AppConfig(server={"dev_mode": True})
    )

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("server.api.agents.log_audit", fail_audit)
    failed = await http.post(
        f"/api/agents/{agent_id}/token/regenerate", headers=headers
    )
    assert failed.status_code == 503
    assert "token" not in failed.text
    assert await provider._load_agent_access_token(old_plaintext) is not None
    reveal_failed = await http.post(
        f"/api/agents/{agent_id}/token/reveal",
        json={"confirmed": True},
        headers=headers,
    )
    assert reveal_failed.status_code == 503
    assert old_plaintext not in reveal_failed.text


async def test_encryption_or_audit_failure_rolls_back_agent_creation(
    client, setup, monkeypatch
):
    http, headers, _ = client
    factory, _, _ = setup

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("server.api.agents.log_audit", fail_audit)
    failed = await http.post(
        "/api/agents", json={"name": "must-rollback"}, headers=headers
    )
    assert failed.status_code == 503
    assert "token" not in failed.text
    async with factory() as session:
        assert (
            await session.execute(
                select(Agent).where(Agent.name == "must-rollback")
            )
        ).scalar_one_or_none() is None


async def test_encryption_failure_rolls_back_agent_creation(
    client, setup, monkeypatch
):
    http, headers, _ = client
    factory, _, _ = setup
    monkeypatch.delenv("PAS_ENCRYPTION_KEY")
    reset_config()
    failed = await http.post(
        "/api/agents", json={"name": "no-encryption"}, headers=headers
    )
    assert failed.status_code == 503
    assert "token" not in failed.text
    async with factory() as session:
        assert (
            await session.execute(
                select(Agent).where(Agent.name == "no-encryption")
            )
        ).scalar_one_or_none() is None


async def test_update_can_clear_description_and_emits_stable_status_audits(
    client, setup
):
    http, headers, _ = client
    factory, _, _ = setup
    created = await http.post(
        "/api/agents",
        json={"name": "lifecycle", "description": "temporary"},
        headers=headers,
    )
    agent_id = created.json()["id"]
    cleared = await http.patch(
        f"/api/agents/{agent_id}",
        json={"description": None},
        headers=headers,
    )
    assert cleared.status_code == 200
    assert cleared.json()["description"] is None
    assert (
        await http.patch(
            f"/api/agents/{agent_id}",
            json={"status": AgentStatus.DISABLED.value},
            headers=headers,
        )
    ).status_code == 200
    assert (
        await http.patch(
            f"/api/agents/{agent_id}",
            json={"status": AgentStatus.ACTIVE.value},
            headers=headers,
        )
    ).status_code == 200
    async with factory() as session:
        actions = list((await session.execute(select(AuditLog.action))).scalars())
    assert "agent.disable" in actions
    assert "agent.enable" in actions


async def test_two_sessions_first_generation_is_linearizable(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'concurrent.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        agent = Agent(name="imported-tokenless")
        session.add(agent)
        await session.commit()
        agent_id = agent.id

    async def generate():
        async with factory() as session:
            row, plaintext = await __import__(
                "server.core.agent_token_service", fromlist=["regenerate_token"]
            ).regenerate_token(session, agent_id, None)
            await session.commit()
            return row.id, plaintext

    results = await asyncio.gather(generate(), generate())
    assert results[0][0] == results[1][0]
    assert results[0][1] != results[1][1]
    async with factory() as session:
        rows = (
            await session.execute(
                select(AgentAPIToken).where(AgentAPIToken.agent_id == agent_id)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].token_hash in {
            hashlib.sha256(plaintext.encode()).hexdigest()
            for _, plaintext in results
        }
    provider = PASAuthProvider(
        session_factory=factory, config=AppConfig(server={"dev_mode": True})
    )
    loaded = [
        await provider._load_agent_access_token(plaintext)
        for _, plaintext in results
    ]
    assert sum(value is not None for value in loaded) == 1
    await engine.dispose()


async def test_concurrent_shared_reveal_budget_allows_exactly_five(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'reveal-limit.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        admin = User(
            external_id="limit-admin",
            display_name="Limit Admin",
            auth_provider=AuthProvider.BUILTIN,
            role=UserRole.ADMIN,
        )
        agent = Agent(name="limit-agent")
        session.add_all([admin, agent])
        await session.commit()
        admin_id = admin.id
        agent_id = agent.id

    async def consume() -> bool:
        async with factory() as session:
            try:
                service = __import__(
                    "server.core.agent_token_service",
                    fromlist=["consume_reveal_budget"],
                )
                await service.consume_reveal_budget(
                    session, admin_id, agent_id
                )
                await session.commit()
                return True
            except service.TokenRevealRateLimitExceeded:
                await session.rollback()
                return False

    results = await asyncio.gather(*(consume() for _ in range(6)))
    assert results.count(True) == 5
    assert results.count(False) == 1
    await engine.dispose()


async def test_agent_routes_require_admin_and_old_user_token_routes_are_gone(
    client, setup
):
    http, headers, member_headers = client
    _, _, member = setup
    created = await http.post(
        "/api/agents", json={"name": "admin-only"}, headers=headers
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]

    forbidden = await http.post(
        "/api/agents", json={"name": "nope"}, headers=member_headers
    )
    assert forbidden.status_code == 403
    assert (
        await http.get("/api/agents", headers=member_headers)
    ).status_code == 403
    assert (
        await http.get(f"/api/agents/{agent_id}", headers=member_headers)
    ).status_code == 403
    assert (
        await http.post(
            f"/api/agents/{agent_id}/token/reveal",
            json={"confirmed": True},
            headers=member_headers,
        )
    ).status_code == 403

    old = await http.post(
        f"/api/users/{member.id}/api-tokens",
        json={"name": "legacy"},
        headers=headers,
    )
    assert old.status_code == 404


async def test_duplicate_agent_name_is_conflict(client):
    http, headers, _ = client
    assert (
        await http.post(
            "/api/agents", json={"name": "unique"}, headers=headers
        )
    ).status_code == 201
    duplicate = await http.post(
        "/api/agents", json={"name": "unique"}, headers=headers
    )
    assert duplicate.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        None,
        {"confirmed": False},
        {"confirmed": True, "unexpected": "field"},
    ],
)
async def test_reveal_requires_strict_confirmation_before_sensitive_work(
    client, setup, monkeypatch, body
):
    http, headers, _ = client
    factory, _, _ = setup
    created = await http.post(
        "/api/agents", json={"name": f"strict-reveal-{body}"}, headers=headers
    )
    agent_id = created.json()["id"]
    consume = AsyncMock()
    decrypt = AsyncMock()
    audit = AsyncMock()
    monkeypatch.setattr(
        "server.api.agents.agent_token_service.consume_reveal_budget", consume
    )
    monkeypatch.setattr(
        "server.api.agents.agent_token_service.reveal_token", decrypt
    )
    monkeypatch.setattr("server.api.agents._audit_token_action", audit)

    request_kwargs = {"headers": headers}
    if body is not None:
        request_kwargs["json"] = body
    response = await http.post(
        f"/api/agents/{agent_id}/token/reveal", **request_kwargs
    )

    assert response.status_code == 422
    consume.assert_not_awaited()
    decrypt.assert_not_awaited()
    audit.assert_not_awaited()
    async with factory() as session:
        assert (
            await session.execute(
                select(AgentTokenRevealLimit).where(
                    AgentTokenRevealLimit.agent_id == agent_id
                )
            )
        ).scalar_one_or_none() is None
        assert (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "agent_token.reveal",
                    AuditLog.target_id == created.json()["token_id"],
                )
            )
        ).scalar_one_or_none() is None


async def test_agent_detail_reports_token_lifecycle_independent_of_agent_status(
    client, setup
):
    http, headers, _ = client
    factory, admin, _ = setup
    created = await http.post(
        "/api/agents", json={"name": "token-summary"}, headers=headers
    )
    agent_id = created.json()["id"]
    assert created.json()["token_summary"]["status"] == "active"
    assert "token" not in created.json()["token_summary"]

    disabled = await http.patch(
        f"/api/agents/{agent_id}",
        json={"status": AgentStatus.DISABLED.value},
        headers=headers,
    )
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["token_summary"]["status"] == "active"
    revealed = await http.post(
        f"/api/agents/{agent_id}/token/reveal",
        json={"confirmed": True},
        headers=headers,
    )
    assert revealed.status_code == 200

    expired_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    expired = await http.post(
        f"/api/agents/{agent_id}/token/regenerate",
        json={"expires_at": expired_at.isoformat()},
        headers=headers,
    )
    assert expired.status_code == 200
    detail = await http.get(f"/api/agents/{agent_id}", headers=headers)
    assert detail.json()["token_summary"]["status"] == "expired"

    denied_expired = await http.post(
        f"/api/agents/{agent_id}/token/reveal",
        json={"confirmed": True},
        headers=headers,
    )
    assert denied_expired.status_code == 409
    async with factory() as session:
        expired_row = (
            await session.execute(
                select(AgentAPIToken).where(
                    AgentAPIToken.agent_id == agent_id
                )
            )
        ).scalar_one()
        assert expired_row.token_ciphertext is None
        # SQLite returns this column as a naive datetime, exercising the UTC
        # normalization path used by the API summary.
        assert expired_row.expires_at is not None
        assert expired_row.expires_at.tzinfo is None
    detail_after_cleanup = await http.get(
        f"/api/agents/{agent_id}", headers=headers
    )
    assert detail_after_cleanup.json()["token_summary"]["status"] == "expired"
    listed_after_cleanup = await http.get("/api/agents", headers=headers)
    listed_agent = next(
        item for item in listed_after_cleanup.json() if item["id"] == agent_id
    )
    assert listed_agent["token_summary"]["status"] == "expired"

    await http.post(f"/api/agents/{agent_id}/token/revoke", headers=headers)
    revoked = await http.get(f"/api/agents/{agent_id}", headers=headers)
    assert revoked.json()["token_summary"]["status"] == "revoked"

    async with factory() as session:
        tokenless = Agent(name="tokenless", created_by=admin.id)
        session.add(tokenless)
        await session.commit()
        tokenless_id = tokenless.id
    missing = await http.get(f"/api/agents/{tokenless_id}", headers=headers)
    assert missing.json()["token_summary"] is None
