import base64
import json
from datetime import datetime, timezone

import pytest

from server.core.signed_cursor import (
    CursorPayload,
    InvalidCursor,
    SignedCursorCodec,
    hash_filters,
)
from server.core.sql_executor import encode_cursor
from server.mcp.tools import _decode_cursor


def _cursor(payload: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def test_decode_cursor_returns_offset():
    assert _decode_cursor(encode_cursor(40)) == 40


def test_decode_cursor_falls_back_for_invalid_token():
    assert _decode_cursor("not-a-cursor") == 0


def test_decode_cursor_clamps_negative_offset():
    assert _decode_cursor(_cursor({"offset": -1})) == 0


def test_decode_cursor_rejects_boolean_offset():
    assert _decode_cursor(_cursor({"offset": True})) == 0


def test_decode_cursor_rejects_string_offset():
    assert _decode_cursor(_cursor({"offset": "10"})) == 0


def test_decode_cursor_rejects_float_offset():
    assert _decode_cursor(_cursor({"offset": 10.5})) == 0


NOW = 1_753_444_800


@pytest.fixture
def codec():
    return SignedCursorCodec(key=b"k" * 32, clock=lambda: NOW)


def _payload(**overrides):
    values = {
        "version": 1,
        "issued_at": NOW,
        "created_at": datetime(
            2026, 7, 25, 12, 0, tzinfo=timezone.utc
        ).isoformat(),
        "db_instance_id": "dbi-last",
        "filter_hash": hash_filters(source="bound"),
    }
    values.update(overrides)
    return CursorPayload(**values)


def test_signed_cursor_round_trip(codec):
    payload = _payload()
    assert codec.decode(
        codec.encode(payload),
        expected_filter_hash=hash_filters(source="bound"),
    ) == payload


def test_cursor_rejects_filter_change(codec):
    cursor = codec.encode(_payload())
    with pytest.raises(InvalidCursor):
        codec.decode(
            cursor,
            expected_filter_hash=hash_filters(source="provisioned"),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda token: "!" + token,
        lambda token: token + "=",
        lambda token: token[:-1] + ("A" if token[-1] != "A" else "B"),
        lambda token: token + ".extra",
    ],
)
def test_cursor_rejects_noncanonical_or_tampered_tokens(codec, mutate):
    cursor = codec.encode(_payload())
    with pytest.raises(InvalidCursor):
        codec.decode(
            mutate(cursor),
            expected_filter_hash=hash_filters(source="bound"),
        )


def test_cursor_rejects_expired_and_future_issued_tokens():
    expired = SignedCursorCodec(key=b"k" * 32, clock=lambda: NOW + 901)
    future = SignedCursorCodec(key=b"k" * 32, clock=lambda: NOW - 1)
    cursor = SignedCursorCodec(
        key=b"k" * 32, clock=lambda: NOW
    ).encode(_payload())
    for decoder in (expired, future):
        with pytest.raises(InvalidCursor):
            decoder.decode(
                cursor,
                expected_filter_hash=hash_filters(source="bound"),
            )


def test_cursor_rejects_key_rotation(codec):
    cursor = codec.encode(_payload())
    with pytest.raises(InvalidCursor):
        SignedCursorCodec(
            key=b"n" * 32, clock=lambda: NOW
        ).decode(
            cursor,
            expected_filter_hash=hash_filters(source="bound"),
        )
