from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from server.bootstrap import load_root_encryption_key


def get_encryption_key() -> bytes:
    """Return the validated bootstrap root encryption key."""
    return load_root_encryption_key()


_get_encryption_key = get_encryption_key


def encrypt(plaintext: str, key: bytes | None = None) -> str:
    """Encrypt plaintext using AES-256-GCM. Returns base64-encoded nonce+ciphertext."""
    if key is None:
        key = get_encryption_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt(encrypted: str, key: bytes | None = None) -> str:
    """Decrypt AES-256-GCM encrypted string. Input is base64-encoded nonce+ciphertext."""
    if key is None:
        key = get_encryption_key()
    raw = base64.b64decode(encrypted)
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
