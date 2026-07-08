from __future__ import annotations

from server.core.responses import error_response


_FORBIDDEN_IDENTIFIER_CHARS = set("`;',\"")
_MAX_IDENTIFIER_LENGTH = 256


def validate_identifier_minimal(value: object, field: str) -> str | dict:
    if not isinstance(value, str):
        return error_response("INVALID_IDENTIFIER", f"{field} must be a string.")
    if not value:
        return error_response("INVALID_IDENTIFIER", f"{field} must not be empty.")
    if len(value) > _MAX_IDENTIFIER_LENGTH:
        return error_response("INVALID_IDENTIFIER", f"{field} is too long.")
    if any(ch.isspace() for ch in value):
        return error_response("INVALID_IDENTIFIER", f"{field} must not contain whitespace.")
    if any(ch in _FORBIDDEN_IDENTIFIER_CHARS for ch in value):
        return error_response("INVALID_IDENTIFIER", f"{field} contains forbidden characters.")
    if "," in value or "#" in value or "/*" in value or "*/" in value or "--" in value:
        return error_response("INVALID_IDENTIFIER", f"{field} contains forbidden characters.")
    return value
