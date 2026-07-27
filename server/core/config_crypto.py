from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict


class SecretEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: str = "AES-256-GCM"
    key_version: int = 1
    ciphertext: str
    nonce: str
    tag_encoding: str = "ciphertext_suffix"


class ConfigCrypto:
    """Purpose-separated encryption and digest operations for configuration."""

    def __init__(self, root_key: bytes, *, key_version: int = 1) -> None:
        if len(root_key) != 32:
            raise ValueError("root_key must be exactly 32 bytes")
        self._encryption_key = self._derive(
            root_key, b"pas/config/encryption/v1"
        )
        self._hmac_key = self._derive(root_key, b"pas/config/hmac/v1")
        self.key_version = key_version

    @staticmethod
    def _derive(root_key: bytes, info: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=info,
        ).derive(root_key)

    @staticmethod
    def _aad(
        *, module: str, field_path: str, schema_version: int, key_version: int
    ) -> bytes:
        return json.dumps(
            {
                "field_path": field_path,
                "key_version": key_version,
                "module": module,
                "schema_version": schema_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def encrypt_field(
        self,
        plaintext: str,
        *,
        module: str,
        field_path: str,
        schema_version: int,
    ) -> SecretEnvelope:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._encryption_key).encrypt(
            nonce,
            plaintext.encode("utf-8"),
            self._aad(
                module=module,
                field_path=field_path,
                schema_version=schema_version,
                key_version=self.key_version,
            ),
        )
        return SecretEnvelope(
            key_version=self.key_version,
            ciphertext=base64.b64encode(ciphertext).decode("ascii"),
            nonce=base64.b64encode(nonce).decode("ascii"),
        )

    def decrypt_field(
        self,
        envelope: SecretEnvelope,
        *,
        module: str,
        field_path: str,
        schema_version: int,
    ) -> str:
        if envelope.algorithm != "AES-256-GCM":
            raise ValueError("unsupported secret envelope algorithm")
        if envelope.tag_encoding != "ciphertext_suffix":
            raise ValueError("unsupported authentication tag encoding")
        plaintext = AESGCM(self._encryption_key).decrypt(
            base64.b64decode(envelope.nonce, validate=True),
            base64.b64decode(envelope.ciphertext, validate=True),
            self._aad(
                module=module,
                field_path=field_path,
                schema_version=schema_version,
                key_version=envelope.key_version,
            ),
        )
        return plaintext.decode("utf-8")

    def digest(self, value: Any) -> str:
        canonical = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hmac.new(
            self._hmac_key, canonical, hashlib.sha256
        ).hexdigest()

