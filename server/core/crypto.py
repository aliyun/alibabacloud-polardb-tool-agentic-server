from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from server.config import get_config


def _get_encryption_key() -> bytes:
    """Get the 32-byte encryption key from config or env."""
    config = get_config()
    key_str = config.encryption.key or os.environ.get("PAS_ENCRYPTION_KEY", "")
    if not key_str:
        raise ValueError("Encryption key is not configured. Set PAS_ENCRYPTION_KEY.")
    key_bytes = base64.b64decode(key_str)
    if len(key_bytes) != 32:
        raise ValueError(f"Encryption key must be 32 bytes (got {len(key_bytes)}). Provide a base64-encoded 32-byte key.")
    return key_bytes


def encrypt(plaintext: str, key: bytes | None = None) -> str:
    """Encrypt plaintext using AES-256-GCM. Returns base64-encoded nonce+ciphertext."""
    if key is None:
        key = _get_encryption_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt(encrypted: str, key: bytes | None = None) -> str:
    """Decrypt AES-256-GCM encrypted string. Input is base64-encoded nonce+ciphertext."""
    if key is None:
        key = _get_encryption_key()
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
