from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts.dev.seed_mock_external_identity import seed_mock_identity
from server.models import (
    Base,
    EnterpriseDirectoryUser,
    EnterpriseIdentitySource,
    User,
    UserExternalIdentity,
    UserWorkspace,
)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _seed(session, *, pas_user: str = "admin"):
    return await seed_mock_identity(
        session,
        pas_user=pas_user,
        source_name="Mock external identity source",
        tenant_id="mock-external-tenant",
        external_user_id="mock-directory-user",
        display_name="Mock external user",
        email="mock-external@example.com",
    )


@pytest.mark.asyncio
async def test_seed_mock_identity_is_idempotent(session) -> None:
    admin = User(external_id="admin", display_name="Administrator")
    session.add(admin)
    await session.flush()

    first = await _seed(session)
    second = await _seed(session, pas_user=admin.id)
    await session.commit()

    assert first.source_id == second.source_id
    assert first.user_id == admin.id
    assert first.provider_key == "feishu:mock-external-tenant"
    source = await session.get(EnterpriseIdentitySource, first.source_id)
    assert source is not None
    assert source.config_ciphertext is None
    for model in (
        EnterpriseIdentitySource,
        EnterpriseDirectoryUser,
        UserExternalIdentity,
        UserWorkspace,
    ):
        count = await session.scalar(select(func.count()).select_from(model))
        assert count == 1


@pytest.mark.asyncio
async def test_seed_mock_identity_refuses_mapping_reassignment(session) -> None:
    first_user = User(external_id="first", display_name="First")
    second_user = User(external_id="second", display_name="Second")
    session.add_all([first_user, second_user])
    await session.flush()
    await _seed(session, pas_user="first")

    with pytest.raises(
        ValueError,
        match="already linked to another PAS user",
    ):
        await _seed(session, pas_user="second")
