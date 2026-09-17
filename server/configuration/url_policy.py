from __future__ import annotations

from urllib.parse import SplitResult, urlsplit


STRICT_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
PAS_IPV4_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _strict_loopback_http_url(value: str) -> SplitResult | None:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in STRICT_LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        return None
    return parsed


def is_strict_loopback_http_url(value: str) -> bool:
    return _strict_loopback_http_url(value) is not None


def is_strict_loopback_http_origin(value: str) -> bool:
    parsed = _strict_loopback_http_url(value)
    return (
        parsed is not None
        and parsed.path in {"", "/"}
        and not parsed.query
    )


def is_pas_ipv4_loopback_http_origin(value: str) -> bool:
    parsed = _strict_loopback_http_url(value)
    return (
        parsed is not None
        and parsed.hostname in PAS_IPV4_LOOPBACK_HOSTS
        and parsed.path in {"", "/"}
        and not parsed.query
    )
