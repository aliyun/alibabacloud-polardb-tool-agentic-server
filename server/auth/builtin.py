from __future__ import annotations

import hashlib
import hmac
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import AuthProvider, User


def hash_password(password: str) -> str:
    """Hash password using PBKDF2-HMAC-SHA256."""
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
    return salt.hex() + ":" + key.hex()


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against stored hash."""
    try:
        salt_hex, key_hex = password_hash.split(":")
        salt = bytes.fromhex(salt_hex)
        stored_key = bytes.fromhex(key_hex)
        new_key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        return hmac.compare_digest(new_key, stored_key)
    except (ValueError, AttributeError):
        return False


async def authenticate_builtin(
    session: AsyncSession, username: str, password: str
) -> User | None:
    """Authenticate with builtin credentials. Returns User if valid, None otherwise."""
    result = await session.execute(
        select(User).where(
            User.external_id == username,
            User.auth_provider == AuthProvider.BUILTIN,
        )
    )
    user = result.scalar_one_or_none()
    if user is None:
        return None
    if not user.password_hash:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
