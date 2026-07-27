from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from server.core.crypto import get_encryption_key

_CURSOR_VERSION = 1
_CURSOR_TTL_SECONDS = 15 * 60
_CURSOR_CONTEXT = b"db-list-cursor-v1"
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class InvalidCursor(ValueError):
    """Raised for every invalid cursor without revealing validation details."""


@dataclass(frozen=True)
class CursorPayload:
    version: int
    issued_at: int
    created_at: str
    db_instance_id: str
    filter_hash: str


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    if not value or not _BASE64URL_RE.fullmatch(value):
        raise InvalidCursor("Invalid cursor")
    try:
        decoded = base64.urlsafe_b64decode(
            value + "=" * (-len(value) % 4)
        )
    except (ValueError, UnicodeError) as exc:
        raise InvalidCursor("Invalid cursor") from exc
    if _encode_base64url(decoded) != value:
        raise InvalidCursor("Invalid cursor")
    return decoded


def hash_filters(
    *,
    db_type: str | None = None,
    source: str | None = None,
    status: str | None = None,
) -> str:
    payload = {
        "db_type": db_type,
        "source": source,
        "status": status,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


class SignedCursorCodec:
    def __init__(
        self,
        key: bytes | None = None,
        *,
        clock: Callable[[], float] = time.time,
        ttl_seconds: int = _CURSOR_TTL_SECONDS,
    ) -> None:
        master_key = get_encryption_key() if key is None else key
        if len(master_key) < 16:
            raise ValueError("Cursor master key is too short")
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=_CURSOR_CONTEXT,
        ).derive(master_key)
        self._clock = clock
        self._ttl_seconds = ttl_seconds

    def encode(self, payload: CursorPayload) -> str:
        self._validate_payload(payload, expected_filter_hash=None)
        encoded_payload = _encode_base64url(
            _canonical_json(asdict(payload))
        )
        signature = hmac.new(
            self._key,
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded_payload}.{_encode_base64url(signature)}"

    def decode(
        self, cursor: str, expected_filter_hash: str
    ) -> CursorPayload:
        try:
            if not isinstance(cursor, str) or len(cursor) > 4096:
                raise InvalidCursor("Invalid cursor")
            encoded_payload, separator, encoded_signature = cursor.partition(
                "."
            )
            if not separator or "." in encoded_signature:
                raise InvalidCursor("Invalid cursor")
            payload_bytes = _decode_base64url(encoded_payload)
            signature = _decode_base64url(encoded_signature)
            expected_signature = hmac.new(
                self._key,
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected_signature):
                raise InvalidCursor("Invalid cursor")
            raw = json.loads(payload_bytes)
            if not isinstance(raw, dict) or set(raw) != {
                "version",
                "issued_at",
                "created_at",
                "db_instance_id",
                "filter_hash",
            }:
                raise InvalidCursor("Invalid cursor")
            if isinstance(raw["version"], bool) or not isinstance(
                raw["version"], int
            ):
                raise InvalidCursor("Invalid cursor")
            if isinstance(raw["issued_at"], bool) or not isinstance(
                raw["issued_at"], int
            ):
                raise InvalidCursor("Invalid cursor")
            if not all(
                isinstance(raw[field], str)
                for field in (
                    "created_at",
                    "db_instance_id",
                    "filter_hash",
                )
            ):
                raise InvalidCursor("Invalid cursor")
            payload = CursorPayload(**raw)
            if _canonical_json(asdict(payload)) != payload_bytes:
                raise InvalidCursor("Invalid cursor")
            self._validate_payload(payload, expected_filter_hash)
            return payload
        except InvalidCursor:
            raise
        except (TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise InvalidCursor("Invalid cursor") from exc

    def _validate_payload(
        self,
        payload: CursorPayload,
        expected_filter_hash: str | None,
    ) -> None:
        if (
            isinstance(payload.version, bool)
            or payload.version != _CURSOR_VERSION
            or isinstance(payload.issued_at, bool)
            or not isinstance(payload.issued_at, int)
            or not isinstance(payload.created_at, str)
            or not isinstance(payload.db_instance_id, str)
            or not payload.db_instance_id
            or len(payload.db_instance_id) > 255
            or not isinstance(payload.filter_hash, str)
            or not _SHA256_RE.fullmatch(payload.filter_hash)
        ):
            raise InvalidCursor("Invalid cursor")
        try:
            created_at = datetime.fromisoformat(payload.created_at)
        except ValueError as exc:
            raise InvalidCursor("Invalid cursor") from exc
        if created_at.tzinfo is None:
            raise InvalidCursor("Invalid cursor")
        now = int(self._clock())
        if (
            payload.issued_at > now
            or now - payload.issued_at > self._ttl_seconds
        ):
            raise InvalidCursor("Invalid cursor")
        if (
            expected_filter_hash is not None
            and payload.filter_hash != expected_filter_hash
        ):
            raise InvalidCursor("Invalid cursor")
