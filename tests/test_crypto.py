import os

import pytest

from server.core.crypto import encrypt, decrypt


@pytest.fixture
def encryption_key() -> bytes:
    return os.urandom(32)


class TestAESEncryption:
    def test_encrypt_decrypt_roundtrip(self, encryption_key):
        plaintext = "hello-world-password-123"
        encrypted = encrypt(plaintext, key=encryption_key)
        decrypted = decrypt(encrypted, key=encryption_key)
        assert decrypted == plaintext

    def test_different_nonce_each_time(self, encryption_key):
        plaintext = "same text"
        e1 = encrypt(plaintext, key=encryption_key)
        e2 = encrypt(plaintext, key=encryption_key)
        assert e1 != e2  # different nonces

    def test_decrypt_with_wrong_key_fails(self, encryption_key):
        plaintext = "secret"
        encrypted = encrypt(plaintext, key=encryption_key)
        wrong_key = os.urandom(32)
        with pytest.raises(Exception):
            decrypt(encrypted, key=wrong_key)

    def test_invalid_key_length(self):
        with pytest.raises(Exception):
            encrypt("test", key=b"short")

    def test_unicode_roundtrip(self, encryption_key):
        plaintext = "hello world \U0001F30D"
        encrypted = encrypt(plaintext, key=encryption_key)
        assert decrypt(encrypted, key=encryption_key) == plaintext

    def test_empty_string_roundtrip(self, encryption_key):
        encrypted = encrypt("", key=encryption_key)
        assert decrypt(encrypted, key=encryption_key) == ""
