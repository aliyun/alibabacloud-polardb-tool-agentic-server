from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.aliyun.polardb_client import (
    MockPolarDBClient,
    get_polardb_client_async,
    reset_polardb_client,
)
from server import config as config_module
from server.config import AppConfig, reset_config
from server.models import Base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean():
    reset_config()
    reset_polardb_client()
    yield
    reset_config()
    reset_polardb_client()


@pytest.fixture
def encryption_key():
    """Set a random base64-encoded 32-byte key for AES encryption tests."""
    key = base64.b64encode(os.urandom(32)).decode()
    os.environ["PAS_ENCRYPTION_KEY"] = key
    yield key
    os.environ.pop("PAS_ENCRYPTION_KEY", None)


@pytest.fixture
async def engine():
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def session(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_default_settings():
    config_module._config = AppConfig()


def _seed_db_credentials(overrides=None):
    defaults = {
        "credential_mode": "direct_ak",
        "access_key_id": "TEST_DB_ACCESS_KEY_ID",
        "access_key_secret": "DBTEST_CREDENTIAL_VALUE_123",
        "role_arn": "",
        "role_session_name": "polardb-agentic",
        "sts_duration_seconds": 3600,
        "region_id": "cn-hangzhou",
        "openapi_network": "public",
    }
    if overrides:
        defaults.update(overrides)
    config_module._config = AppConfig(aliyun=defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetPolarDBClientAsync:
    async def test_no_credentials_returns_mock(
        self, session: AsyncSession, encryption_key
    ):
        """When neither DB nor env credentials are set, returns MockPolarDBClient."""
        _seed_default_settings()
        client = await get_polardb_client_async(session)
        assert isinstance(client, MockPolarDBClient)
        os.environ.pop("PAS_ALIYUN_ACCESS_KEY_ID", None)
        os.environ.pop("PAS_ALIYUN_ACCESS_KEY_SECRET", None)

    async def test_returns_same_client_on_same_creds(
        self, session: AsyncSession, encryption_key
    ):
        """Active module credentials reuse the same cached client."""
        _seed_db_credentials()
        c1 = await get_polardb_client_async(session)
        c2 = await get_polardb_client_async(session)
        assert c1 is c2
        assert not isinstance(c1, MockPolarDBClient)

    async def test_db_creds_take_precedence_over_env(
        self, session: AsyncSession, encryption_key
    ):
        """Legacy environment credentials are ignored."""
        os.environ["PAS_ALIYUN_ACCESS_KEY_ID"] = "TEST_ENV_ACCESS_KEY_ID"
        os.environ["PAS_ALIYUN_ACCESS_KEY_SECRET"] = "TEST_ENV_CREDENTIAL_VALUE"
        _seed_default_settings()
        client = await get_polardb_client_async(session)
        assert isinstance(client, MockPolarDBClient)

    async def test_cache_invalidated_on_mode_change(
        self, session: AsyncSession, encryption_key
    ):
        """Changing credential mode invalidates the cached client."""
        _seed_db_credentials()
        c1 = await get_polardb_client_async(session)

        _seed_db_credentials({
            "credential_mode": "assume_role",
            "role_arn": "acs:ram::123456:role/test-role",
        })
        c2 = await get_polardb_client_async(session)

        assert c1 is not c2

    async def test_cache_invalidated_on_network_change(
        self, session: AsyncSession, encryption_key
    ):
        _seed_db_credentials({"openapi_network": "public"})
        public_client = await get_polardb_client_async(session)

        _seed_db_credentials({"openapi_network": "vpc"})
        vpc_client = await get_polardb_client_async(session)

        assert public_client is not vpc_client
