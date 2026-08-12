from __future__ import annotations

import re
from collections.abc import Mapping

from server.logging import safe_request_id

_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$", re.ASCII)
_SENSITIVE_DETAIL_RE = re.compile(
    r"(?:access[_-]?key|secret|password|security[_-]?token|signature)\s*[:=]",
    re.IGNORECASE | re.ASCII,
)
_MAX_ERROR_DETAIL_LENGTH = 512


def safe_error_code(value: object) -> str | None:
    if isinstance(value, str) and _ERROR_CODE_RE.fullmatch(value):
        return value
    return None


def safe_error_detail(value: object) -> str | None:
    """Return a bounded structured cloud message without credential material."""
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized or _SENSITIVE_DETAIL_RE.search(normalized):
        return None
    return normalized[:_MAX_ERROR_DETAIL_LENGTH]


def _known_exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and not any(current is item for item in chain):
        chain.append(current)
        try:
            current = current.__cause__
        except Exception:
            break
    return tuple(chain)


def request_id_from_error(error: BaseException) -> str | None:
    """Read only structured SDK diagnostic fields, never response text/body."""
    for item in _known_exception_chain(error):
        for attribute in ("request_id", "requestId"):
            try:
                value = getattr(item, attribute, None)
            except Exception:
                continue
            request_id = safe_request_id(value)
            if request_id is not None:
                return request_id
        try:
            data = getattr(item, "data", None)
        except Exception:
            data = None
        if isinstance(data, Mapping):
            for field in ("RequestId", "requestId", "request_id"):
                try:
                    value = data.get(field)
                except Exception:
                    continue
                request_id = safe_request_id(value)
                if request_id is not None:
                    return request_id
        try:
            response = getattr(item, "response", None)
            headers = getattr(response, "headers", None)
        except Exception:
            headers = None
        if isinstance(headers, Mapping):
            try:
                value = headers.get("x-acs-request-id")
            except Exception:
                continue
            request_id = safe_request_id(value)
            if request_id is not None:
                return request_id
    return None


def error_code_from_error(error: BaseException) -> str | None:
    """Return a bounded SDK error code without reading its message/body."""
    for item in _known_exception_chain(error):
        try:
            code = getattr(item, "code", None)
        except Exception:
            continue
        if safe_error_code(code) is not None:
            return code
    return None


def error_detail_from_error(error: BaseException) -> str | None:
    """Read only structured SDK message fields, never ``str(error)`` or bodies."""
    for item in _known_exception_chain(error):
        try:
            detail = safe_error_detail(getattr(item, "message", None))
        except Exception:
            detail = None
        if detail is not None:
            return detail
        try:
            data = getattr(item, "data", None)
        except Exception:
            data = None
        if isinstance(data, Mapping):
            for field in ("Message", "message"):
                try:
                    detail = safe_error_detail(data.get(field))
                except Exception:
                    continue
                if detail is not None:
                    return detail
    return None
