import time

import pytest
from jwt import PyJWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import server.auth.jwt_manager as jwt_manager
from server.auth.jwt_manager import (
    _generate_rsa_key_pair,
    create_access_token,
    create_refresh_token,
    get_public_key,
    initialize_jwt_keys_from_db,
    reset_keys,
    verify_token,
)
from server.configuration.bootstrap import initialize_configuration
from server.configuration.repository import ConfigRepository
from server.core.config_crypto import ConfigCrypto
from server.models import Base, SystemConfig
from tests._helpers import init_test_jwt_keys

ROOT_KEY = b"01234567890123456789012345678901"

@pytest.fixture(autouse=True)
def clean_state():
    reset_keys()
    init_test_jwt_keys()
    yield
    reset_keys()


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
        assert payload["sub"] == "user:user-123"
        assert payload["role"] == "admin"
        assert payload["type"] == "access"

    def test_create_and_verify_refresh_token(self):
        token = create_refresh_token({"sub": "user-456"})
        payload = verify_token(token)
        assert payload["sub"] == "user:user-456"
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
        with pytest.raises(PyJWTError):
            verify_token("invalid.token.here")

    def test_token_from_different_key_rejected(self):
        """Tokens signed with one key pair are rejected under a different key pair."""
        import server.auth.jwt_manager as jm

        token = create_access_token({"sub": "u1"})

        # Replace keys with a completely different pair
        priv_pem, pub_pem = _generate_rsa_key_pair()
        jm._private_key = priv_pem
        jm._public_key = pub_pem

        with pytest.raises(PyJWTError):
            verify_token(token)

    def test_key_persistence_across_calls(self):
        t1 = create_access_token({"sub": "u1"})
        t2 = create_access_token({"sub": "u2"})
        # Both should verify with the same key
        p1 = verify_token(t1)
        p2 = verify_token(t2)
        assert p1["sub"] == "user:u1"
        assert p2["sub"] == "user:u2"

class TestJWTDatabaseInit:
    """JWT keys come only from the encrypted token_security module."""

    async def test_loads_encrypted_keys_from_modular_config(
        self, engine
    ):
        factory = async_sessionmaker(engine, expire_on_commit=False)
        crypto = ConfigCrypto(ROOT_KEY)
        repository = ConfigRepository(factory)
        await initialize_configuration(repository, crypto)
        reset_keys()
        async with factory() as session:
            await initialize_jwt_keys_from_db(session, crypto)

        token = create_access_token({"sub": "db-test"})
        payload = verify_token(token)
        assert payload["sub"] == "user:db-test"
        assert payload["session_epoch"] == 1

        async with factory() as session:
            row = (
                await session.execute(
                    select(SystemConfig).where(
                        SystemConfig.config_key
                        == "module.token_security"
                    )
                )
            ).scalar_one()
        assert "BEGIN PRIVATE KEY" not in row.config_value

    async def test_second_pod_loads_same_key_from_db(self, engine):
        """Simulates multi-node: Pod A stores in DB, Pod B loads from DB."""
        factory = async_sessionmaker(engine, expire_on_commit=False)
        crypto = ConfigCrypto(ROOT_KEY)
        repository = ConfigRepository(factory)
        await initialize_configuration(repository, crypto)
        reset_keys()
        async with factory() as session:
            await initialize_jwt_keys_from_db(session, crypto)
        token = create_access_token({"sub": "cross-node"})

        reset_keys()
        async with factory() as session:
            await initialize_jwt_keys_from_db(session, crypto)

        payload = verify_token(token)
        assert payload["sub"] == "user:cross-node"

    async def test_key_rotation_kid_rejects_unknown_key(self, engine):
        factory = async_sessionmaker(engine, expire_on_commit=False)
        crypto = ConfigCrypto(ROOT_KEY)
        repository = ConfigRepository(factory)
        await initialize_configuration(repository, crypto)
        reset_keys()
        async with factory() as session:
            await initialize_jwt_keys_from_db(session, crypto)

        token = create_access_token({"sub": "keyed"})
        assert jwt_manager.jwt.get_unverified_header(token)["kid"]
        unknown = jwt_manager.jwt.encode(
            {"sub": "user:keyed", "exp": time.time() + 60},
            _generate_rsa_key_pair()[0],
            algorithm="RS256",
            headers={"kid": "unknown"},
        )
        with pytest.raises(PyJWTError):
            verify_token(unknown)
