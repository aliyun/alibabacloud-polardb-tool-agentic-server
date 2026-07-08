from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from server.config import get_config

logger = logging.getLogger(__name__)

_private_key: str | None = None
_public_key: str | None = None


def _generate_rsa_key_pair() -> tuple[str, str]:
    """Generate an RSA key pair for development/single-node use."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem

async def initialize_jwt_keys_from_db(session: "AsyncSession") -> None:
    """Initialize JWT keys from the shared database for multi-node consistency.

    Called during app startup. Priority: already-loaded globals -> inline
    config/env -> configured file paths -> DB system_settings -> generate once
    into DB. No local data/ file is used (DB is the single persistent source).
    """
    global _private_key, _public_key
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from server.models.system_setting import SystemSetting

    if _private_key and _public_key:
        return

    config = get_config().auth.jwt
    if config.private_key and config.public_key:
        _load_keys()
        return
    if config.private_key_path and config.public_key_path:
        _load_keys()
        return

    result = await session.execute(
        select(SystemSetting).where(SystemSetting.key == "jwt_rsa_keys")
    )
    setting = result.scalar_one_or_none()

    if setting:
        data = json.loads(setting.value)
        _private_key = data["private_key"]
        _public_key = data["public_key"]
        logger.info("Loaded JWT keys from database")
        return

    private_pem, public_pem = _generate_rsa_key_pair()
    new_setting = SystemSetting(
        key="jwt_rsa_keys",
        value=json.dumps({"private_key": private_pem, "public_key": public_pem}),
        description="Auto-generated JWT RSA key pair for token signing",
    )
    try:
        session.add(new_setting)
        await session.commit()
        _private_key = private_pem
        _public_key = public_pem
        logger.info("Generated and stored JWT keys in database")
    except IntegrityError:
        await session.rollback()
        result = await session.execute(
            select(SystemSetting).where(SystemSetting.key == "jwt_rsa_keys")
        )
        setting = result.scalar_one()
        data = json.loads(setting.value)
        _private_key = data["private_key"]
        _public_key = data["public_key"]
        logger.info("JWT keys already in database (created by another pod)")


def _load_keys() -> tuple[str, str]:
    """Load RSA key pair (sync, request-time).

    Priority: already-loaded globals → inline config/env → configured file
              paths → RuntimeError if still empty (DB init must run at startup).
    """
    global _private_key, _public_key
    if _private_key and _public_key:
        return _private_key, _public_key

    config = get_config().auth.jwt

    # Priority 1: Inline key content (from config or PAS_AUTH_JWT_PRIVATE_KEY env var)
    if config.private_key and config.public_key:
        _private_key = config.private_key
        _public_key = config.public_key
        logger.info("Loaded JWT keys from inline configuration")
        return _private_key, _public_key

    # Priority 2: Key files from configured paths
    if config.private_key_path and config.public_key_path:
        private_path = Path(config.private_key_path)
        public_path = Path(config.public_key_path)
        if private_path.exists() and public_path.exists():
            _private_key = private_path.read_text()
            _public_key = public_path.read_text()
            logger.info("Loaded JWT keys from configured paths")
            return _private_key, _public_key
        raise FileNotFoundError(
            f"JWT key files not found: private={config.private_key_path}, public={config.public_key_path}"
        )

    # Keys must have been initialized at startup from DB (or config). If globals
    # are still empty here, startup init did not run — refuse rather than silently
    # diverge across pods.
    raise RuntimeError(
        "JWT keys not initialized. initialize_jwt_keys_from_db() must run at startup, "
        "or auth.jwt.private_key/public_key (or *_path) must be configured."
    )


def get_public_key() -> str:
    """Get the public key PEM."""
    _, pub = _load_keys()
    return pub


def create_access_token(data: dict[str, object]) -> str:
    """Create a signed JWT access token."""
    config = get_config().auth.jwt
    private_key, _ = _load_keys()
    payload = dict(data)
    payload["exp"] = int(time.time()) + config.access_token_expire_minutes * 60
    payload["iat"] = int(time.time())
    payload["type"] = "access"
    return str(jwt.encode(payload, private_key, algorithm=config.algorithm))


def create_refresh_token(data: dict[str, object]) -> str:
    """Create a signed JWT refresh token."""
    config = get_config().auth.jwt
    private_key, _ = _load_keys()
    payload = dict(data)
    payload["exp"] = int(time.time()) + config.refresh_token_expire_days * 86400
    payload["iat"] = int(time.time())
    payload["type"] = "refresh"
    return str(jwt.encode(payload, private_key, algorithm=config.algorithm))


def verify_token(token: str) -> dict[str, object]:
    """Verify and decode a JWT token. Raises jose.JWTError on failure."""
    config = get_config().auth.jwt
    _, public_key = _load_keys()
    result: dict[str, object] = jwt.decode(token, public_key, algorithms=[config.algorithm])
    return result


def reset_keys() -> None:
    """Reset cached keys (for testing)."""
    global _private_key, _public_key
    _private_key = None
    _public_key = None
