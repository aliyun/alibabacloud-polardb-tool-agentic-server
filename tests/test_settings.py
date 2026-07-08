import base64
import os

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from server.app import create_app
from server.auth.builtin import hash_password
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.core.crypto import decrypt
from server.core.settings_manager import (
    get_setting, set_setting, get_all_settings, batch_update_settings,
    get_setting_raw,
)
from server.db import engine as engine_mod
from server.mcp.transport import reset_mcp
from server.models import Base, AuthProvider, User, UserRole, UserStatus
from server.models.system_setting import SystemSetting, SettingDef, SETTINGS_SCHEMA


@pytest.fixture(autouse=True)
def clean():
    reset_config()
    init_test_jwt_keys()
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


class TestSystemSettingModel:
    async def test_create_setting(self, session: AsyncSession):
        setting = SystemSetting(key="pool_target_size", value="5", description="Pool target")
        session.add(setting)
        await session.commit()
        await session.refresh(setting)
        assert setting.id is not None
        assert setting.key == "pool_target_size"
        assert setting.value == "5"

    async def test_unique_key(self, session: AsyncSession):
        from sqlalchemy.exc import IntegrityError
        s1 = SystemSetting(key="pool_target_size", value="5")
        s2 = SystemSetting(key="pool_target_size", value="10")
        session.add(s1)
        await session.flush()
        session.add(s2)
        with pytest.raises(IntegrityError):
            await session.flush()


class TestSettingsSchema:
    def test_pool_target_size_in_schema(self):
        assert "pool_target_size" in SETTINGS_SCHEMA
        assert SETTINGS_SCHEMA["pool_target_size"].type == "int"
        assert SETTINGS_SCHEMA["pool_target_size"].default == "0"

    def test_required_network_settings(self):
        for key in ("pool_region_id", "pool_vpc_id", "pool_vswitch_id", "pool_zone_id"):
            assert key in SETTINGS_SCHEMA
            assert SETTINGS_SCHEMA[key].required is True

    def test_setting_def_fields(self):
        sd = SettingDef(type="int", default="42", required=False, description="test")
        assert sd.type == "int"
        assert sd.default == "42"


class TestSettingsManager:
    async def _seed_defaults(self, session: AsyncSession):
        """Seed all SETTINGS_SCHEMA defaults into the DB."""
        for key, sd in SETTINGS_SCHEMA.items():
            setting = SystemSetting(key=key, value=sd.default, description=sd.description)
            session.add(setting)
        await session.commit()

    async def test_get_setting_returns_value(self, session: AsyncSession):
        await self._seed_defaults(session)
        val = await get_setting(session, "pool_target_size")
        assert val == "0"

    async def test_get_setting_not_found_returns_default(self, session: AsyncSession):
        val = await get_setting(session, "nonexistent_key", default="fallback")
        assert val == "fallback"

    async def test_set_setting_updates_value(self, session: AsyncSession):
        await self._seed_defaults(session)
        await set_setting(session, "pool_target_size", "5")
        val = await get_setting(session, "pool_target_size")
        assert val == "5"

    async def test_set_setting_rejects_unknown_key(self, session: AsyncSession):
        with pytest.raises(ValueError, match="Unknown setting"):
            await set_setting(session, "unknown_key", "value")

    async def test_set_setting_validates_int_type(self, session: AsyncSession):
        await self._seed_defaults(session)
        with pytest.raises(ValueError, match="must be an integer"):
            await set_setting(session, "pool_target_size", "not_a_number")

    async def test_set_setting_validates_bool_type(self, session: AsyncSession):
        await self._seed_defaults(session)
        with pytest.raises(ValueError, match="must be"):
            await set_setting(session, "pool_allow_shut_down", "yes")

    async def test_batch_update_all_or_nothing(self, session: AsyncSession):
        await self._seed_defaults(session)
        with pytest.raises(ValueError):
            await batch_update_settings(session, {
                "pool_target_size": "10",
                "pool_scale_min": "not_a_number",
            })
        val = await get_setting(session, "pool_target_size")
        assert val == "0"

    async def test_batch_update_success(self, session: AsyncSession):
        await self._seed_defaults(session)
        await batch_update_settings(session, {
            "pool_target_size": "3",
            "pool_region_id": "cn-hangzhou",
        })
        assert await get_setting(session, "pool_target_size") == "3"
        assert await get_setting(session, "pool_region_id") == "cn-hangzhou"

    async def test_get_all_settings(self, session: AsyncSession):
        await self._seed_defaults(session)
        all_settings = await get_all_settings(session)
        assert len(all_settings) == len(SETTINGS_SCHEMA)
        keys = {s["key"] for s in all_settings}
        assert "pool_target_size" in keys


# ---------------------------------------------------------------------------
# API-level tests
# ---------------------------------------------------------------------------

_ADMIN_PASSWORD = "TestPass123"


@pytest.fixture
async def app_client():
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()
    os.environ["PAS_SERVER_DEV_MODE"] = "true"
    os.environ["PAS_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
    os.environ["PAS_ADMIN_INITIAL_PASSWORD"] = _ADMIN_PASSWORD

    # Create in-memory engine and tables, then inject into engine module
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._engine = e
    engine_mod._session_factory = async_sessionmaker(e, expire_on_commit=False)

    # Seed admin user (lifespan may not run reliably under ASGITransport)
    async with engine_mod._session_factory() as session:
        admin = User(
            external_id="admin",
            display_name="Administrator",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password(_ADMIN_PASSWORD),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(admin)
        await session.commit()

    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
    await e.dispose()
    reset_config()
    engine_mod.reset_engine()
    reset_mcp()


async def _login_admin(client: AsyncClient) -> dict:
    resp = await client.post("/auth/login", json={"username": "admin", "password": _ADMIN_PASSWORD})
    assert resp.status_code == 200
    return resp.cookies


class TestSettingsAPI:
    async def test_get_settings_requires_admin(self, app_client):
        resp = await app_client.get("/api/settings")
        assert resp.status_code == 401

    async def test_get_settings(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.get("/api/settings", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) > 0
        keys = {s["key"] for s in data}
        assert "pool_target_size" in keys

    async def test_put_setting(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.put("/api/settings/pool_target_size", json={"value": "5"}, cookies=cookies)
        assert resp.status_code == 200
        resp = await app_client.get("/api/settings", cookies=cookies)
        target = next(s for s in resp.json() if s["key"] == "pool_target_size")
        assert target["value"] == "5"

    async def test_put_setting_unknown_key(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.put("/api/settings/unknown_key", json={"value": "x"}, cookies=cookies)
        assert resp.status_code == 400

    async def test_put_setting_invalid_type(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.put("/api/settings/pool_target_size", json={"value": "abc"}, cookies=cookies)
        assert resp.status_code == 400

    async def test_batch_update(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.post("/api/settings/batch", json={
            "settings": {"pool_target_size": "3", "pool_region_id": "cn-hangzhou"}
        }, cookies=cookies)
        assert resp.status_code == 200

    async def test_batch_update_partial_failure(self, app_client):
        cookies = await _login_admin(app_client)
        resp = await app_client.post("/api/settings/batch", json={
            "settings": {"pool_target_size": "abc", "pool_region_id": "cn-hangzhou"}
        }, cookies=cookies)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Secret-type settings tests
# ---------------------------------------------------------------------------

@pytest.fixture
def encryption_key():
    """Set a random base64-encoded 32-byte key for AES encryption tests."""
    key = base64.b64encode(os.urandom(32)).decode()
    os.environ["PAS_ENCRYPTION_KEY"] = key
    yield key
    os.environ.pop("PAS_ENCRYPTION_KEY", None)


class TestSecretSettings:
    async def test_secret_keys_in_schema(self):
        """All credential keys exist in SETTINGS_SCHEMA."""
        for key in (
            "aliyun_access_key_id",
            "aliyun_access_key_secret",
            "aliyun_credential_mode",
            "aliyun_role_arn",
            "aliyun_role_session_name",
            "aliyun_sts_duration_seconds",
        ):
            assert key in SETTINGS_SCHEMA

    async def test_secret_type_exists(self):
        """The 'secret' type is used for AK/SK fields."""
        assert SETTINGS_SCHEMA["aliyun_access_key_id"].type == "secret"
        assert SETTINGS_SCHEMA["aliyun_access_key_secret"].type == "secret"

    async def test_encrypt_on_write(self, session: AsyncSession, encryption_key):
        """set_setting encrypts secret-type values before storing in DB."""
        await set_setting(session, "aliyun_access_key_id", "TEST_ACCESS_KEY_ID_12345678")
        # Read raw value from DB — it should NOT be the plaintext
        row = await session.execute(
            select(SystemSetting).where(SystemSetting.key == "aliyun_access_key_id")
        )
        setting = row.scalar_one()
        assert setting.value != "TEST_ACCESS_KEY_ID_12345678"
        # But decrypting it should yield the original
        assert decrypt(setting.value) == "TEST_ACCESS_KEY_ID_12345678"

    async def test_mask_on_read(self, session: AsyncSession, encryption_key):
        """get_all_settings returns masked values for secret fields."""
        await set_setting(session, "aliyun_access_key_id", "TEST_ACCESS_KEY_ID_12345678")
        all_settings = await get_all_settings(session)
        ak_setting = next(s for s in all_settings if s["key"] == "aliyun_access_key_id")
        # Should be masked: first4 + **** + last4
        assert ak_setting["value"] == "TEST****5678"

    async def test_mask_short_secrets(self, session: AsyncSession, encryption_key):
        """Secrets shorter than 8 chars are fully masked."""
        await set_setting(session, "aliyun_access_key_id", "short")
        all_settings = await get_all_settings(session)
        ak_setting = next(s for s in all_settings if s["key"] == "aliyun_access_key_id")
        assert ak_setting["value"] == "****"

    async def test_get_setting_raw_decrypts(self, session: AsyncSession, encryption_key):
        """get_setting_raw returns decrypted plaintext for secret fields."""
        await set_setting(session, "aliyun_access_key_secret", "TEST_CREDENTIAL_VALUE_1234")
        raw = await get_setting_raw(session, "aliyun_access_key_secret")
        assert raw == "TEST_CREDENTIAL_VALUE_1234"

    async def test_get_setting_raw_non_secret(self, session: AsyncSession, encryption_key):
        """get_setting_raw returns plain value for non-secret fields."""
        await set_setting(session, "pool_region_id", "cn-hangzhou")
        raw = await get_setting_raw(session, "pool_region_id")
        assert raw == "cn-hangzhou"

    async def test_batch_skips_masked(self, session: AsyncSession, encryption_key):
        """batch_update_settings skips masked secret values."""
        await set_setting(session, "aliyun_access_key_id", "TEST_ACCESS_KEY_ID_12345678")
        # Now batch update with masked value — should not overwrite
        await batch_update_settings(session, {
            "aliyun_access_key_id": "TEST****5678",
            "pool_region_id": "cn-shanghai",
        })
        raw = await get_setting_raw(session, "aliyun_access_key_id")
        assert raw == "TEST_ACCESS_KEY_ID_12345678"
        assert await get_setting(session, "pool_region_id") == "cn-shanghai"

    async def test_batch_skips_masked_format(self, session: AsyncSession, encryption_key):
        """batch_update_settings skips values matching the masked pattern."""
        await set_setting(session, "aliyun_access_key_secret", "ORIGINAL_TEST_VALUE_1")
        # Pure mask
        await batch_update_settings(session, {
            "aliyun_access_key_secret": "****",
        })
        raw = await get_setting_raw(session, "aliyun_access_key_secret")
        assert raw == "ORIGINAL_TEST_VALUE_1"

    async def test_empty_secret_stores_empty(self, session: AsyncSession, encryption_key):
        """Setting a secret to empty string stores empty (no encryption)."""
        await set_setting(session, "aliyun_access_key_id", "")
        row = await session.execute(
            select(SystemSetting).where(SystemSetting.key == "aliyun_access_key_id")
        )
        setting = row.scalar_one()
        assert setting.value == ""


class TestCredentialModeValidation:
    async def test_valid_modes_accepted(self, session: AsyncSession):
        """Both direct_ak and assume_role are valid credential modes."""
        await set_setting(session, "aliyun_credential_mode", "direct_ak")
        assert await get_setting(session, "aliyun_credential_mode") == "direct_ak"
        await set_setting(session, "aliyun_credential_mode", "assume_role")
        assert await get_setting(session, "aliyun_credential_mode") == "assume_role"

    async def test_invalid_mode_rejected(self, session: AsyncSession):
        """Invalid credential mode raises ValueError."""
        with pytest.raises(ValueError, match="must be 'direct_ak' or 'assume_role'"):
            await set_setting(session, "aliyun_credential_mode", "invalid_mode")
