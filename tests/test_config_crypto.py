from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidTag

from server.core.config_crypto import ConfigCrypto


def test_secret_envelope_uses_field_bound_aad() -> None:
    crypto = ConfigCrypto(b"r" * 32)
    envelope = crypto.encrypt_field(
        "secret",
        module="user_sso",
        field_path="client_secret",
        schema_version=1,
    )

    assert envelope.algorithm == "AES-256-GCM"
    assert envelope.key_version == 1
    assert len(base64.b64decode(envelope.nonce)) == 12
    assert (
        crypto.decrypt_field(
            envelope,
            module="user_sso",
            field_path="client_secret",
            schema_version=1,
        )
        == "secret"
    )

    with pytest.raises(InvalidTag):
        crypto.decrypt_field(
            envelope,
            module="aliyun_access",
            field_path="client_secret",
            schema_version=1,
        )


def test_encrypting_same_value_uses_fresh_nonce() -> None:
    crypto = ConfigCrypto(b"r" * 32)

    first = crypto.encrypt_field(
        "same", module="user_sso", field_path="client_secret", schema_version=1
    )
    second = crypto.encrypt_field(
        "same", module="user_sso", field_path="client_secret", schema_version=1
    )

    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext


def test_secret_bearing_digest_is_keyed_and_canonical() -> None:
    first = ConfigCrypto(b"a" * 32)
    same_key = ConfigCrypto(b"a" * 32)
    other_key = ConfigCrypto(b"b" * 32)

    assert first.digest({"b": 2, "a": 1}) == same_key.digest({"a": 1, "b": 2})
    assert first.digest({"secret": "value"}) != other_key.digest(
        {"secret": "value"}
    )

