from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
from cryptography.exceptions import InvalidTag

from server.core.config_crypto import ConfigCrypto
from server.configuration.secrets import (
    SecretFieldSpec,
    decrypt_secret_tree,
    encrypt_stored_secret,
    export_secret_tree,
    redact_secret_tree,
)


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


def test_nested_secret_tree_keeps_plaintext_outside_runtime_projection() -> None:
    crypto = ConfigCrypto(b"r" * 32)
    spec = SecretFieldSpec("direct_ak.access_key_id", display_mask=True)
    stored = {
        "direct_ak": {
            "access_key_id": encrypt_stored_secret(
                "TEST1234567890ABCD",
                spec=spec,
                crypto=crypto,
                module="aliyun_access",
                schema_version=2,
                now=datetime(2026, 8, 9, tzinfo=timezone.utc),
            )
        }
    }

    assert "TEST1234567890ABCD" not in json.dumps(stored)
    assert redact_secret_tree(stored, secret_fields=(spec,)) == {
        "direct_ak": {
            "access_key_id": {
                "configured": True,
                "updated_at": "2026-08-09T00:00:00+00:00",
                    "display_hint": "TEST****ABCD",
            }
        }
    }
    assert export_secret_tree(stored, secret_fields=(spec,)) == {}
    assert decrypt_secret_tree(
        stored,
        secret_fields=(spec,),
        crypto=crypto,
        module="aliyun_access",
        schema_version=2,
    ) == {"direct_ak": {"access_key_id": "TEST1234567890ABCD"}}
