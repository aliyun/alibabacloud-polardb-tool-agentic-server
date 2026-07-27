from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.models import (
    Base,
    ConfigBootstrapClaim,
    ConfigOperationReceipt,
    SystemConfig,
)


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as value:
        yield value
    await engine.dispose()


def test_mysql_config_value_is_longtext() -> None:
    column_type = SystemConfig.__table__.c.config_value.type

    assert isinstance(
        column_type.dialect_impl(mysql.dialect()), mysql.LONGTEXT
    )


async def test_large_document_round_trip(session: AsyncSession) -> None:
    value = json.dumps({"payload": "x" * 70_000})
    session.add(
        SystemConfig(
            config_key="module.test",
            config_value=value,
            config_version=1,
        )
    )
    await session.commit()

    row = await session.get(SystemConfig, "module.test")

    assert row is not None
    assert row.config_value == value


async def test_bootstrap_claim_is_singleton(session: AsyncSession) -> None:
    future = datetime.now(timezone.utc) + timedelta(minutes=15)
    session.add(
        ConfigBootstrapClaim(
            singleton_key="bootstrap",
            token_hash="a" * 64,
            expires_at=future,
        )
    )
    await session.flush()
    session.add(
        ConfigBootstrapClaim(
            singleton_key="bootstrap",
            token_hash="b" * 64,
            expires_at=future,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()


async def test_operation_receipt_key_is_actor_scoped(
    session: AsyncSession,
) -> None:
    future = datetime.now(timezone.utc) + timedelta(hours=24)
    common = {
        "idempotency_key_hash": "c" * 64,
        "action": "activate",
        "module": "user_sso",
        "request_digest": "d" * 64,
        "status": "completed",
        "response_json": "{}",
        "expires_at": future,
    }
    session.add(
        ConfigOperationReceipt(
            actor_scope="admin:first",
            **common,
        )
    )
    session.add(
        ConfigOperationReceipt(
            actor_scope="admin:second",
            **common,
        )
    )

    await session.commit()

