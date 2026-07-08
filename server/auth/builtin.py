from __future__ import annotations

import hashlib
import hmac
import logging
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_config
from server.models import User, AuthProvider, UserRole, UserStatus

logger = logging.getLogger(__name__)


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


async def ensure_admin_exists(session: AsyncSession) -> None:
    """Ensure the builtin admin account exists in ALL auth modes.

    - First run: creates admin with PAS_ADMIN_INITIAL_PASSWORD.
    - Subsequent runs with env var set: resets password (forgot-password recovery).
    """
    config = get_config()
    password = os.environ.get("PAS_ADMIN_INITIAL_PASSWORD", "")
    admin_username = config.auth.builtin.admin_username

    result = await session.execute(
        select(User).where(
            User.external_id == admin_username,
            User.auth_provider == AuthProvider.BUILTIN,
        )
    )
    admin = result.scalar_one_or_none()

    if admin is None:
        if not password:
            logger.error("PAS_ADMIN_INITIAL_PASSWORD not set. Cannot create initial admin.")
            return
        admin = User(
            external_id=admin_username,
            display_name="Administrator",
            auth_provider=AuthProvider.BUILTIN,
            password_hash=hash_password(password),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        session.add(admin)
        await session.commit()
        logger.info("Initial admin user created", extra={"action": "admin_created"})
    elif password and not verify_password(password, admin.password_hash or ""):
        admin.password_hash = hash_password(password)
        await session.commit()
        logger.info("Admin password reset from PAS_ADMIN_INITIAL_PASSWORD", extra={"action": "admin_password_reset"})


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
