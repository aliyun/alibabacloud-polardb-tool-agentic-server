import json
import time

import pytest
from jose import JWTError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from server.auth.jwt_manager import (
    create_access_token,
    create_refresh_token,
    initialize_jwt_keys_from_db,
    verify_token,
    get_public_key,
    reset_keys,
    _generate_rsa_key_pair,
)
from server.config import reset_config
from tests._helpers import init_test_jwt_keys
from server.models import Base


@pytest.fixture(autouse=True)
def clean_state():
    reset_keys()
    reset_config()
    init_test_jwt_keys()
    yield
    reset_keys()
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


class TestJWTManager:
    def test_create_and_verify_access_token(self):
        token = create_access_token({"sub": "user-123", "role": "admin"})
        payload = verify_token(token)
        assert payload["sub"] == "user-123"
        assert payload["role"] == "admin"
        assert payload["type"] == "access"

    def test_create_and_verify_refresh_token(self):
        token = create_refresh_token({"sub": "user-456"})
        payload = verify_token(token)
        assert payload["sub"] == "user-456"
        assert payload["type"] == "refresh"

    def test_access_token_has_expiration(self):
        token = create_access_token({"sub": "u1"})
        payload = verify_token(token)
        assert "exp" in payload
        assert payload["exp"] > time.time()

    def test_get_public_key_returns_pem(self):
        pub = get_public_key()
        assert "BEGIN PUBLIC KEY" in pub

    def test_invalid_token_rejected(self):
        with pytest.raises(JWTError):
            verify_token("invalid.token.here")

    def test_token_from_different_key_rejected(self):
        """Tokens signed with one key pair are rejected under a different key pair."""
        import server.auth.jwt_manager as jm

        token = create_access_token({"sub": "u1"})

        # Replace keys with a completely different pair
        priv_pem, pub_pem = _generate_rsa_key_pair()
        jm._private_key = priv_pem
        jm._public_key = pub_pem

        with pytest.raises(JWTError):
            verify_token(token)

    def test_key_persistence_across_calls(self):
        t1 = create_access_token({"sub": "u1"})
        t2 = create_access_token({"sub": "u2"})
        # Both should verify with the same key
        p1 = verify_token(t1)
        p2 = verify_token(t2)
        assert p1["sub"] == "u1"
        assert p2["sub"] == "u2"

    def test_load_keys_from_files(self, tmp_path):
        from server.config import load_config
        from server.auth.jwt_manager import _generate_rsa_key_pair

        priv_pem, pub_pem = _generate_rsa_key_pair()
        priv_file = tmp_path / "jwt.key"
        pub_file = tmp_path / "jwt.pub"
        priv_file.write_text(priv_pem)
        pub_file.write_text(pub_pem)

        # Create config with key paths
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            f"auth:\n  jwt:\n    private_key_path: '{priv_file}'\n    public_key_path: '{pub_file}'\n"
        )
        reset_keys()
        reset_config()
        import server.config as cfg
        cfg._config = load_config(config_file)

        token = create_access_token({"sub": "file-key-test"})
        payload = verify_token(token)
        assert payload["sub"] == "file-key-test"

    def test_inline_key_config(self, tmp_path):
        """Inline key content via config (simulates PAS_AUTH_JWT_PRIVATE_KEY env var)."""
        from server.config import load_config
        from server.auth.jwt_manager import _generate_rsa_key_pair

        priv_pem, pub_pem = _generate_rsa_key_pair()
        config_file = tmp_path / "config.yaml"
        config_file.write_text("server:\n  dev_mode: true\n")
        reset_keys()
        reset_config()
        import server.config as cfg
        cfg._config = load_config(config_file)
        cfg._config.auth.jwt.private_key = priv_pem
        cfg._config.auth.jwt.public_key = pub_pem

        token = create_access_token({"sub": "inline-key-test"})
        payload = verify_token(token)
        assert payload["sub"] == "inline-key-test"



class TestJWTDatabaseInit:
    """Tests for DB-backed JWT key initialization (multi-node support)."""

    async def test_generates_and_stores_keys_in_db(self, session):
        """First pod: generates keys and stores in DB."""
        reset_keys()
        reset_config()
        await initialize_jwt_keys_from_db(session)

        token = create_access_token({"sub": "db-test"})
        payload = verify_token(token)
        assert payload["sub"] == "db-test"

        from sqlalchemy import select
        from server.models.system_setting import SystemSetting

        result = await session.execute(
            select(SystemSetting).where(SystemSetting.key == "jwt_rsa_keys")
        )
        setting = result.scalar_one()
        data = json.loads(setting.value)
        assert "private_key" in data
        assert f"BEGIN {'PRIVATE'} KEY" in data["private_key"]

    async def test_second_pod_loads_from_db(self, session):
        """Simulates multi-node: Pod A stores in DB, Pod B loads from DB."""
        reset_keys()
        reset_config()
        # Pod A initializes
        await initialize_jwt_keys_from_db(session)
        token = create_access_token({"sub": "cross-node"})

        # Pod B: fresh in-memory state, different data dir (simulating different node)
        reset_keys()
        await initialize_jwt_keys_from_db(session)

        payload = verify_token(token)
        assert payload["sub"] == "cross-node"

    async def test_db_init_skipped_when_config_keys_set(self, session, tmp_path):
        """DB init is a no-op when inline keys are configured."""
        from server.auth.jwt_manager import _generate_rsa_key_pair
        from server.config import load_config
        import server.config as cfg

        priv_pem, pub_pem = _generate_rsa_key_pair()
        config_file = tmp_path / "config.yaml"
        config_file.write_text("server:\n  dev_mode: true\n")
        reset_keys()
        reset_config()
        cfg._config = load_config(config_file)
        cfg._config.auth.jwt.private_key = priv_pem
        cfg._config.auth.jwt.public_key = pub_pem

        await initialize_jwt_keys_from_db(session)

        from sqlalchemy import select
        from server.models.system_setting import SystemSetting

        result = await session.execute(
            select(SystemSetting).where(SystemSetting.key == "jwt_rsa_keys")
        )
        assert result.scalar_one_or_none() is None
