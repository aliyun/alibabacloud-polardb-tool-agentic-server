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
from server.config import reset_config
from server.core.settings_manager import set_setting
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

async def _seed_default_settings(session: AsyncSession, encryption_key):
    """Seed credential-related settings with empty AK/SK (defaults)."""
    defaults = {
        "aliyun_credential_mode": "direct_ak",
        "aliyun_access_key_id": "",
        "aliyun_access_key_secret": "",
        "aliyun_role_arn": "",
        "aliyun_role_session_name": "polardb-agentic",
        "aliyun_sts_duration_seconds": "3600",
        "pool_region_id": "cn-hangzhou",
    }
    for key, value in defaults.items():
        await set_setting(session, key, value)


async def _seed_db_credentials(session: AsyncSession, encryption_key, overrides=None):
    """Seed credential-related settings with real AK/SK."""
    defaults = {
        "aliyun_credential_mode": "direct_ak",
        "aliyun_access_key_id": "TEST_DB_ACCESS_KEY_ID",
        "aliyun_access_key_secret": "DBTEST_CREDENTIAL_VALUE_123",
        "aliyun_role_arn": "",
        "aliyun_role_session_name": "polardb-agentic",
        "aliyun_sts_duration_seconds": "3600",
        "pool_region_id": "cn-hangzhou",
    }
    if overrides:
        defaults.update(overrides)
    for key, value in defaults.items():
        await set_setting(session, key, value)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGetPolarDBClientAsync:
    async def test_no_credentials_returns_mock(
        self, session: AsyncSession, encryption_key
    ):
        """When neither DB nor env credentials are set, returns MockPolarDBClient."""
        await _seed_default_settings(session, encryption_key)
        client = await get_polardb_client_async(session)
        assert isinstance(client, MockPolarDBClient)

    async def test_returns_same_client_on_same_creds(
        self, session: AsyncSession, encryption_key
    ):
        """Env-based creds: calling twice returns the same cached instance."""
        os.environ["PAS_ALIYUN_ACCESS_KEY_ID"] = "TEST_ENV_ACCESS_KEY_ID"
        os.environ["PAS_ALIYUN_ACCESS_KEY_SECRET"] = "TEST_ENV_CREDENTIAL_VALUE"
        try:
            reset_config()
            await _seed_default_settings(session, encryption_key)
            c1 = await get_polardb_client_async(session)
            c2 = await get_polardb_client_async(session)
            assert c1 is c2
            assert not isinstance(c1, MockPolarDBClient)
        finally:
            os.environ.pop("PAS_ALIYUN_ACCESS_KEY_ID", None)
            os.environ.pop("PAS_ALIYUN_ACCESS_KEY_SECRET", None)
            reset_config()

    async def test_db_creds_take_precedence_over_env(
        self, session: AsyncSession, encryption_key
    ):
        """DB credentials take precedence over environment variables."""
        os.environ["PAS_ALIYUN_ACCESS_KEY_ID"] = "TEST_ENV_ACCESS_KEY_ID"
        os.environ["PAS_ALIYUN_ACCESS_KEY_SECRET"] = "TEST_ENV_CREDENTIAL_VALUE"
        try:
            reset_config()
            await _seed_db_credentials(session, encryption_key)
            client = await get_polardb_client_async(session)
            assert not isinstance(client, MockPolarDBClient)
        finally:
            os.environ.pop("PAS_ALIYUN_ACCESS_KEY_ID", None)
            os.environ.pop("PAS_ALIYUN_ACCESS_KEY_SECRET", None)
            reset_config()

    async def test_cache_invalidated_on_mode_change(
        self, session: AsyncSession, encryption_key
    ):
        """Changing credential mode invalidates the cached client."""
        await _seed_db_credentials(session, encryption_key)
        c1 = await get_polardb_client_async(session)

        # Change the credential mode
        await set_setting(session, "aliyun_credential_mode", "assume_role")
        await set_setting(session, "aliyun_role_arn", "acs:ram::123456:role/test-role")
        c2 = await get_polardb_client_async(session)

        assert c1 is not c2
