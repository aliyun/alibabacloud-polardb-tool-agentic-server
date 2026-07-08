import base64
import json

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
