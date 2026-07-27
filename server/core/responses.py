# server/core/responses.py
"""Shared response builders for MCP tool handlers and provisioning paths."""
from __future__ import annotations

import json
from typing import Any


def error_response(code: str, message: str, **extra: Any) -> dict:
    """Build a standardized MCP error response.

    The returned shape matches the MCP tool result contract:
        {"content": [{"type": "text", "text": "<json>"}], "isError": True}

    The JSON payload contains ``error`` and ``message`` fields, plus any
    additional keyword arguments merged into the payload.
    """
    payload: dict[str, Any] = {"error": code, "message": message}
    if extra:
        payload.update(extra)
    return {
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "isError": True,
    }
