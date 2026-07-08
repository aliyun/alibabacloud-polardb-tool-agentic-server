from __future__ import annotations

import base64
import os
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.config import reset_config
from server.core.settings_manager import set_setting
from server.models import Base
from server.aliyun.credential_provider import (
    AliyunCredentials,
    AssumeRoleProvider,
    DirectAKProvider,
    build_credential_provider,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean():
    reset_config()
    yield
    reset_config()


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


@pytest.fixture
def encryption_key():
    """Set a random base64-encoded 32-byte key for AES encryption tests."""
    key = base64.b64encode(os.urandom(32)).decode()
    os.environ["PAS_ENCRYPTION_KEY"] = key
    yield key
    os.environ.pop("PAS_ENCRYPTION_KEY", None)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _seed_settings(session: AsyncSession, encryption_key, overrides: dict[str, str] | None = None):
    """Seed credential-related settings into the DB."""
    defaults = {
        "aliyun_credential_mode": "direct_ak",
        "aliyun_access_key_id": "TEST_ACCESS_KEY_ID",
        "aliyun_access_key_secret": "TEST_CREDENTIAL_VALUE_123",
        "aliyun_role_arn": "acs:ram::123456:role/test-role",
        "aliyun_role_session_name": "polardb-agentic",
        "aliyun_sts_duration_seconds": "3600",
        "pool_region_id": "cn-hangzhou",
    }
    if overrides:
        defaults.update(overrides)
    for key, value in defaults.items():
        await set_setting(session, key, value)


# ---------------------------------------------------------------------------
# Tests: DirectAKProvider
# ---------------------------------------------------------------------------

class TestDirectAKProvider:
    async def test_returns_credentials(self):
        provider = DirectAKProvider(ak="my_ak", sk="my_sk", region_id="cn-shanghai")
        creds = await provider.get_credentials()
        assert creds.access_key_id == "my_ak"
        assert creds.access_key_secret == "my_sk"
        assert creds.security_token is None
        assert creds.region_id == "cn-shanghai"

    async def test_default_region(self):
        provider = DirectAKProvider(ak="ak", sk="sk")
        creds = await provider.get_credentials()
        assert creds.region_id == "cn-hangzhou"

    async def test_frozen_credentials(self):
        provider = DirectAKProvider(ak="ak", sk="sk")
        creds = await provider.get_credentials()
        assert isinstance(creds, AliyunCredentials)
        with pytest.raises(AttributeError):
            creds.access_key_id = "changed"


# ---------------------------------------------------------------------------
# Tests: AssumeRoleProvider
# ---------------------------------------------------------------------------

def _make_sts_mock():
    """Create a mock STS response."""
    mock_cred = MagicMock()
    mock_cred.access_key_id = "STS.temp_ak"
    mock_cred.access_key_secret = "temp_sk"
    mock_cred.security_token = "token123"
    mock_resp = MagicMock()
    mock_resp.body.credentials = mock_cred
    return mock_resp


class TestAssumeRoleProvider:
    async def test_calls_sts_and_returns_credentials(self):
        provider = AssumeRoleProvider(
            ak="base_ak", sk="base_sk",
            role_arn="acs:ram::123:role/test",
            session_name="test-session",
            duration=3600,
            region_id="cn-beijing",
        )
        mock_resp = _make_sts_mock()
        with patch(
            "server.aliyun.credential_provider._call_sts_assume_role",
            new_callable=AsyncMock,
        ) as mock_sts:
            mock_sts.return_value = mock_resp
            creds = await provider.get_credentials()

        assert creds.access_key_id == "STS.temp_ak"
        assert creds.access_key_secret == "temp_sk"
        assert creds.security_token == "token123"
        assert creds.region_id == "cn-beijing"
        mock_sts.assert_called_once_with(
            "base_ak", "base_sk", "acs:ram::123:role/test",
            "test-session", 3600,
        )

    async def test_caches_credentials(self):
        provider = AssumeRoleProvider(
            ak="ak", sk="sk", role_arn="arn", duration=3600,
        )
        mock_resp = _make_sts_mock()
        with patch(
            "server.aliyun.credential_provider._call_sts_assume_role",
            new_callable=AsyncMock,
        ) as mock_sts:
            mock_sts.return_value = mock_resp
            creds1 = await provider.get_credentials()
            creds2 = await provider.get_credentials()

        # STS should only be called once due to caching
        assert mock_sts.call_count == 1
        assert creds1 is creds2

    async def test_refreshes_when_near_expiry(self):
        provider = AssumeRoleProvider(
            ak="ak", sk="sk", role_arn="arn", duration=3600,
        )
        mock_resp = _make_sts_mock()
        with patch(
            "server.aliyun.credential_provider._call_sts_assume_role",
            new_callable=AsyncMock,
        ) as mock_sts:
            mock_sts.return_value = mock_resp
            # First call populates cache
            await provider.get_credentials()
            assert mock_sts.call_count == 1

            # Simulate near-expiry: set _expires_at to soon
            provider._expires_at = time.monotonic() + 100  # within 300s buffer

            # Second call should refresh
            await provider.get_credentials()
            assert mock_sts.call_count == 2

    async def test_default_session_name_and_duration(self):
        provider = AssumeRoleProvider(ak="ak", sk="sk", role_arn="arn")
        assert provider._session_name == "polardb-agentic"
        assert provider._duration == 3600
        assert provider._region_id == "cn-hangzhou"


# ---------------------------------------------------------------------------
# Tests: build_credential_provider
# ---------------------------------------------------------------------------

class TestBuildCredentialProvider:
    async def test_direct_ak_mode(self, session: AsyncSession, encryption_key):
        await _seed_settings(session, encryption_key, {
            "aliyun_credential_mode": "direct_ak",
        })
        provider = await build_credential_provider(session)
        assert isinstance(provider, DirectAKProvider)
        creds = await provider.get_credentials()
        assert creds.access_key_id == "TEST_ACCESS_KEY_ID"
        assert creds.access_key_secret == "TEST_CREDENTIAL_VALUE_123"
        assert creds.region_id == "cn-hangzhou"

    async def test_assume_role_mode(self, session: AsyncSession, encryption_key):
        await _seed_settings(session, encryption_key, {
            "aliyun_credential_mode": "assume_role",
            "aliyun_role_arn": "acs:ram::999:role/my-role",
            "aliyun_role_session_name": "custom-session",
            "aliyun_sts_duration_seconds": "1800",
            "pool_region_id": "cn-shanghai",
        })
        provider = await build_credential_provider(session)
        assert isinstance(provider, AssumeRoleProvider)
        assert provider._role_arn == "acs:ram::999:role/my-role"
        assert provider._session_name == "custom-session"
        assert provider._duration == 1800
        assert provider._region_id == "cn-shanghai"

    async def test_defaults_to_direct_ak(self, session: AsyncSession, encryption_key):
        """When no credential mode is stored, defaults to direct_ak."""
        # Don't seed anything — should use schema defaults
        provider = await build_credential_provider(session)
        assert isinstance(provider, DirectAKProvider)

    async def test_custom_region_propagated(self, session: AsyncSession, encryption_key):
        await _seed_settings(session, encryption_key, {
            "aliyun_credential_mode": "direct_ak",
            "pool_region_id": "ap-southeast-1",
        })
        provider = await build_credential_provider(session)
        creds = await provider.get_credentials()
        assert creds.region_id == "ap-southeast-1"
