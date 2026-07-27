from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import JWTError, jwt
from sqlalchemy import select

from server.auth.principal import user_subject
from server.configuration.types import ModuleDocument
from server.core.config_crypto import ConfigCrypto, SecretEnvelope
from server.models import SystemConfig

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_private_key: str | None = None
_public_key: str | None = None


@dataclass(frozen=True, slots=True)
class JWTKeyRing:
    algorithm: str
    active_kid: str
    private_key: str
    public_keys: dict[str, str]
    access_token_expire_minutes: int
    refresh_token_expire_days: int
    session_epoch: int
    require_epoch: bool = True


_key_ring: JWTKeyRing | None = None


def _generate_rsa_key_pair() -> tuple[str, str]:
    """Generate the shared RSA key material persisted during first boot."""
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


def _legacy_test_key_ring() -> JWTKeyRing:
    if not _private_key or not _public_key:
        raise RuntimeError(
            "JWT keys are not initialized from module.token_security"
        )
    return JWTKeyRing(
        algorithm="RS256",
        active_kid="",
        private_key=_private_key,
        public_keys={"": _public_key},
        access_token_expire_minutes=480,
        refresh_token_expire_days=30,
        session_epoch=1,
        require_epoch=False,
    )


def _current_key_ring() -> JWTKeyRing:
    return _key_ring or _legacy_test_key_ring()


async def initialize_jwt_keys_from_db(
    session: AsyncSession,
    crypto: ConfigCrypto | None = None,
) -> None:
    """Load the encrypted, shared token-security module into memory."""
    global _key_ring, _private_key, _public_key
    if crypto is None:
        from server.bootstrap import load_bootstrap_settings

        crypto = ConfigCrypto(load_bootstrap_settings().encryption_key)

    row = (
        await session.execute(
            select(SystemConfig).where(
                SystemConfig.config_key == "module.token_security"
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise RuntimeError("module.token_security has not been initialized")

    document = ModuleDocument.model_validate_json(row.config_value)
    if document.effective is None:
        raise RuntimeError("module.token_security is not active")
    config = document.effective.config
    envelope_value = config.get("private_key")
    if not isinstance(envelope_value, dict) or "$secret" not in envelope_value:
        raise RuntimeError("token_security.private_key is not encrypted")
    private_key = crypto.decrypt_field(
        SecretEnvelope.model_validate(envelope_value["$secret"]),
        module="token_security",
        field_path="private_key",
        schema_version=document.schema_version,
    )
    public_keys = config.get("public_keys")
    active_kid = config.get("active_kid")
    if (
        not isinstance(public_keys, dict)
        or not isinstance(active_kid, str)
        or active_kid not in public_keys
    ):
        raise RuntimeError("token_security key ring is incomplete")

    _key_ring = JWTKeyRing(
        algorithm=str(config.get("algorithm", "RS256")),
        active_kid=active_kid,
        private_key=private_key,
        public_keys={
            str(kid): str(public_key)
            for kid, public_key in public_keys.items()
        },
        access_token_expire_minutes=int(
            config.get("access_token_expire_minutes", 480)
        ),
        refresh_token_expire_days=int(
            config.get("refresh_token_expire_days", 30)
        ),
        session_epoch=int(config.get("session_epoch", 1)),
    )
    _private_key = private_key
    _public_key = _key_ring.public_keys[active_kid]
    logger.info("Loaded JWT key ring from modular configuration")


def _load_keys() -> tuple[str, str]:
    """Return active signing and verification keys."""
    ring = _current_key_ring()
    return ring.private_key, ring.public_keys[ring.active_kid]


def get_public_key() -> str:
    """Get the active public key PEM."""
    return _load_keys()[1]


def _create_token(data: dict[str, object], *, token_type: str) -> str:
    ring = _current_key_ring()
    now = int(time.time())
    payload: dict[str, Any] = dict(data)
    subject = payload.get("sub")
    if isinstance(subject, str):
        payload["sub"] = user_subject(subject)
    lifetime = (
        ring.access_token_expire_minutes * 60
        if token_type == "access"
        else ring.refresh_token_expire_days * 86400
    )
    payload.update(
        {
            "exp": now + lifetime,
            "iat": now,
            "type": token_type,
            "session_epoch": ring.session_epoch,
        }
    )
    headers = {"kid": ring.active_kid} if ring.active_kid else None
    return str(
        jwt.encode(
            payload,
            ring.private_key,
            algorithm=ring.algorithm,
            headers=headers,
        )
    )


def create_access_token(data: dict[str, object]) -> str:
    """Create a signed JWT access token."""
    return _create_token(data, token_type="access")


def create_refresh_token(data: dict[str, object]) -> str:
    """Create a signed JWT refresh token."""
    return _create_token(data, token_type="refresh")


def verify_token(token: str) -> dict[str, object]:
    """Verify a token against the configured key ring and session epoch."""
    ring = _current_key_ring()
    header = jwt.get_unverified_header(token)
    kid = header.get("kid", "")
    if not isinstance(kid, str) or kid not in ring.public_keys:
        raise JWTError("unknown JWT signing key")
    result: dict[str, object] = jwt.decode(
        token,
        ring.public_keys[kid],
        algorithms=[ring.algorithm],
    )
    if ring.require_epoch and result.get("session_epoch") != ring.session_epoch:
        raise JWTError("JWT session epoch is no longer active")
    return result


def reset_keys() -> None:
    """Reset cached keys (for testing and snapshot reload)."""
    global _key_ring, _private_key, _public_key
    _key_ring = None
    _private_key = None
    _public_key = None
