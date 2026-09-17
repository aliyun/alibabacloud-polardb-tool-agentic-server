from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.auth.principal import PrincipalAuthenticationError, PrincipalKind, agent_subject, user_subject
from server.mcp.workspace_context import resolve_mcp_workspace_context
from server.models import Agent, Base, OAuthRefreshToken, User, UserStatus, UserWorkspace
from server.models.base import utc_now


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def test_personal_context_needs_no_agent_or_workspace(session):
    user = User(external_id="personal", display_name="Personal")
    session.add(user)
    await session.commit()
    context = await resolve_mcp_workspace_context(session, user_subject(user.id), personal=True)
    assert context.resource_principal.kind == PrincipalKind.USER
    assert context.resource_principal.id == user.id
    assert context.sql_actor is user
    assert context.agent is None
    assert await session.scalar(select(UserWorkspace)) is None


async def test_personal_context_cannot_select_service_account(session):
    user = User(external_id="owner", display_name="Owner")
    agent = Agent(name="owned-service", creator=user)
    session.add_all([user, agent])
    await session.commit()
    with pytest.raises(PrincipalAuthenticationError):
        await resolve_mcp_workspace_context(session, user_subject(user.id), agent.id, personal=True)
    with pytest.raises(PrincipalAuthenticationError):
        await resolve_mcp_workspace_context(session, agent_subject(agent.id), personal=True)


async def test_personal_credential_is_hash_only_owned_and_revocable(session):
    from server.core import personal_token_service as tokens

    user = User(external_id="alice", display_name="Alice")
    other = User(external_id="bob", display_name="Bob")
    session.add_all([user, other])
    await session.commit()
    row, plaintext = await tokens.issue(session, user.id, resource="https://pas.test/mcp/personal")
    await session.commit()
    assert plaintext.startswith(tokens.TOKEN_PREFIX)
    assert row.token_hash != plaintext
    assert row.client_id == tokens.CLIENT_ID
    assert row.expires_at is not None
    assert (await tokens.resolve(session, plaintext))[0].id == user.id
    with pytest.raises(LookupError):
        await tokens.revoke(session, other.id, row.token_family)
    await tokens.revoke(session, user.id, row.token_family)
    await session.commit()
    assert await tokens.resolve(session, plaintext) is None
    assert await session.scalar(select(OAuthRefreshToken)) is not None


@pytest.mark.parametrize("invalidation", ["disabled", "epoch", "expired"])
async def test_personal_credential_rechecks_identity_and_expiry(session, invalidation):
    from server.core import personal_token_service as tokens

    user = User(external_id="alice", display_name="Alice")
    session.add(user)
    await session.commit()
    row, plaintext = await tokens.issue(session, user.id, resource="https://pas.test/mcp/personal")
    await session.commit()
    if invalidation == "disabled":
        user.status = UserStatus.DISABLED
    elif invalidation == "epoch":
        user.credential_epoch += 1
    else:
        row.expires_at = utc_now() - timedelta(seconds=1)
    await session.commit()
    assert await tokens.resolve(session, plaintext) is None


async def test_personal_credentials_do_not_displace_oauth_refresh_grants(session):
    from server.core import personal_token_service as tokens

    user = User(external_id="alice", display_name="Alice")
    session.add(user)
    await session.flush()
    ordinary = OAuthRefreshToken(
        token_hash="a" * 64, client_id="real-client", user_id=user.id, token_family="ordinary-family", scopes="[]"
    )
    session.add(ordinary)
    await session.commit()
    await tokens.issue(session, user.id, resource="https://pas.test/mcp/personal")
    with pytest.raises(tokens.ActiveTokenExists):
        await tokens.issue(session, user.id, resource="https://pas.test/mcp/personal")
    assert ordinary.revoked_at is None
    rows = await tokens.list_tokens(session, user.id)
    assert len(rows) == 1
    assert rows[0].client_id == tokens.CLIENT_ID


async def test_epoch_rotation_allows_replacing_invalid_personal_token(session):
    from server.core import personal_token_service as tokens

    user = User(external_id="epoch", display_name="Epoch")
    session.add(user)
    await session.commit()
    old, plaintext = await tokens.issue(session, user.id, resource="https://pas.test/mcp/personal")
    user.credential_epoch += 1
    await session.commit()
    assert tokens.status(old, user) == "revoked"
    _, new = await tokens.issue(session, user.id, resource="https://pas.test/mcp/personal")
    assert await tokens.resolve(session, plaintext) is None
    assert (await tokens.resolve(session, new))[0].id == user.id
