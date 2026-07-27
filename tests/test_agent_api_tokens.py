from __future__ import annotations

import base64
import asyncio
import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.auth.oauth_provider import PASAuthProvider
from server.config import AppConfig, reset_config
from server.core.agent_token_service import (
    get_or_create_token,
    regenerate_token,
    reveal_token,
    revoke_token,
)
from server.models import Agent, AgentAPIToken, Base


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv(
        "PAS_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode("ascii")
    )
    reset_config()
    yield
    reset_config()


@pytest.fixture
async def setup():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        agent = Agent(name="production-agent")
        session.add(agent)
        await session.commit()
        await session.refresh(agent)
    yield factory, agent
    await engine.dispose()


async def test_regenerate_reuses_row_and_invalidates_old_token(setup):
    factory, agent = setup
    provider = PASAuthProvider(
        session_factory=factory, config=AppConfig(server={"dev_mode": True})
    )
    async with factory() as session:
        first_row, first = await regenerate_token(session, agent.id, None)
        await session.commit()
        first_id = first_row.id
    async with factory() as session:
        second_row, second = await regenerate_token(session, agent.id, None)
        await session.commit()

    assert second_row.id == first_id
    assert second != first
    assert await provider._load_agent_access_token(first) is None
    loaded = await provider._load_agent_access_token(second)
    assert loaded is not None
    assert loaded.subject == f"agent:{agent.id}"


async def test_get_or_create_returns_existing_active_token(setup):
    factory, agent = setup
    async with factory() as session:
        first_row, first = await get_or_create_token(session, agent.id, None)
        await session.commit()
        second_row, second = await get_or_create_token(session, agent.id, None)
        assert second_row.id == first_row.id
        assert second == first


async def test_token_is_encrypted_and_reveal_rejects_revoked_token(setup):
    factory, agent = setup
    async with factory() as session:
        row, plaintext = await regenerate_token(session, agent.id, None)
        await session.commit()
        assert row.token_hash == hashlib.sha256(plaintext.encode()).hexdigest()
        assert row.token_ciphertext is not None
        assert plaintext not in row.token_ciphertext
        assert await reveal_token(session, agent.id) == plaintext

        revoked = await revoke_token(session, agent.id)
        await session.commit()
        assert revoked.token_ciphertext is None
        with pytest.raises(ValueError, match="not active"):
            await reveal_token(session, agent.id)


async def test_expired_token_cannot_be_revealed_or_authenticated(setup):
    factory, agent = setup
    provider = PASAuthProvider(
        session_factory=factory, config=AppConfig(server={"dev_mode": True})
    )
    async with factory() as session:
        row, plaintext = await regenerate_token(
            session, agent.id, datetime.now(timezone.utc) - timedelta(seconds=1)
        )
        await session.commit()
        with pytest.raises(ValueError, match="not active"):
            await reveal_token(session, agent.id)
        await session.refresh(row)
        assert row.token_ciphertext is None
    assert await provider._load_agent_access_token(plaintext) is None


async def test_missing_encryption_key_fails_closed(setup, monkeypatch):
    factory, agent = setup
    monkeypatch.delenv("PAS_ENCRYPTION_KEY")
    reset_config()
    async with factory() as session:
        with pytest.raises(ValueError, match="PAS_ENCRYPTION_KEY is required"):
            await regenerate_token(session, agent.id, None)
        assert (
            await session.execute(
                select(AgentAPIToken).where(AgentAPIToken.agent_id == agent.id)
            )
        ).scalar_one_or_none() is None


async def test_last_used_at_writes_are_coalesced_for_five_minutes(setup):
    factory, agent = setup
    provider = PASAuthProvider(
        session_factory=factory, config=AppConfig(server={"dev_mode": True})
    )
    async with factory() as session:
        row, plaintext = await regenerate_token(session, agent.id, None)
        await session.commit()
        row_id = row.id

    assert await provider._load_agent_access_token(plaintext) is not None
    first_used_at = None
    for _ in range(20):
        async with factory() as session:
            row = await session.get(AgentAPIToken, row_id)
            assert row is not None
            first_used_at = row.last_used_at
        if first_used_at is not None:
            break
        await asyncio.sleep(0.01)
    assert first_used_at is not None

    assert await provider._load_agent_access_token(plaintext) is not None
    await asyncio.sleep(0.02)
    async with factory() as session:
        row = await session.get(AgentAPIToken, row_id)
        assert row is not None
        assert row.last_used_at == first_used_at


async def test_last_used_telemetry_failure_does_not_fail_authentication(
    setup, monkeypatch
):
    factory, agent = setup
    provider = PASAuthProvider(
        session_factory=factory, config=AppConfig(server={"dev_mode": True})
    )
    async with factory() as session:
        _, plaintext = await regenerate_token(session, agent.id, None)
        await session.commit()

    async def fail_telemetry(*args, **kwargs):
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(
        "server.auth.oauth_provider._record_agent_token_use", fail_telemetry
    )
    loaded = await provider._load_agent_access_token(plaintext)
    assert loaded is not None
    assert loaded.subject == f"agent:{agent.id}"
