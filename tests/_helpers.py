"""Shared test helpers."""

import hashlib
import secrets
import time

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

import server.auth.jwt_manager as _jm
from server.auth.jwt_manager import _generate_rsa_key_pair, _load_keys
from server.models import AgentAPIToken


def init_test_jwt_keys() -> None:
    """Initialize JWT keys for tests after reset_keys()/reset_config().

    Generates a fresh RSA key pair and sets it on the jwt_manager module
    globals only.  _load_keys() checks globals first (already-loaded), so
    this is sufficient for all test calls to create_access_token/verify_token.

    Does NOT call get_config() or modify the config object, because doing so
    would cache a stale config (e.g. including PAS_ENCRYPTION_KEY from the
    shell env) that could break fixtures that override env vars later.
    """
    priv_pem, pub_pem = _generate_rsa_key_pair()
    _jm._private_key = priv_pem
    _jm._public_key = pub_pem


def create_test_access_token(subject: str, *, role: str = "admin") -> str:
    """Sign a test-only access token with an exact, unmodified subject."""
    private_key, _ = _load_keys()
    now = int(time.time())
    return str(
        jwt.encode(
            {
                "sub": subject,
                "role": role,
                "iat": now,
                "exp": now + 3600,
                "type": "access",
            },
            private_key,
            algorithm="RS256",
        )
    )


async def create_test_agent_token(
    session: AsyncSession, agent_id: str
) -> tuple[AgentAPIToken, str]:
    """Create an opaque Agent token without depending on admin token services."""
    plaintext = f"pas_agent_{secrets.token_urlsafe(32)}"
    row = AgentAPIToken(
        agent_id=agent_id,
        token_prefix=plaintext[:32],
        token_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
    )
    session.add(row)
    await session.flush()
    return row, plaintext
