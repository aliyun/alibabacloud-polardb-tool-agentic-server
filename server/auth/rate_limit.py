from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse


@dataclass(frozen=True)
class AuthRateLimitExceeded(Exception):
    retry_after: int


class SlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        requests: int,
        window_seconds: int,
        max_entries: int = 4096,
    ) -> None:
        self._requests = requests
        self._window_seconds = window_seconds
        self._max_entries = max_entries
        self._entries: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self._window_seconds
        async with self._lock:
            if len(self._entries) >= self._max_entries:
                expired_keys = []
                for entry_key, entry_timestamps in self._entries.items():
                    while (
                        entry_timestamps
                        and entry_timestamps[0] <= cutoff
                    ):
                        entry_timestamps.popleft()
                    if not entry_timestamps:
                        expired_keys.append(entry_key)
                for entry_key in expired_keys:
                    del self._entries[entry_key]
            timestamps = self._entries.get(key)
            if timestamps is None:
                if len(self._entries) >= self._max_entries:
                    earliest = min(
                        entry_timestamps[0]
                        for entry_timestamps in self._entries.values()
                    )
                    raise AuthRateLimitExceeded(
                        retry_after=max(
                            1,
                            math.ceil(
                                earliest + self._window_seconds - now
                            ),
                        )
                    )
                timestamps = deque()
                self._entries[key] = timestamps
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self._requests:
                raise AuthRateLimitExceeded(
                    retry_after=max(
                        1,
                        math.ceil(
                            timestamps[0] + self._window_seconds - now
                        ),
                    )
                )
            timestamps.append(now)

    def reset(self) -> None:
        self._entries.clear()


_login_remote_limiter = SlidingWindowRateLimiter(
    requests=20, window_seconds=60
)
_login_account_limiter = SlidingWindowRateLimiter(
    requests=5, window_seconds=60
)
_registration_limiter = SlidingWindowRateLimiter(
    requests=10, window_seconds=60
)
_token_limiter = SlidingWindowRateLimiter(
    requests=20, window_seconds=60
)


def _remote_address(scope: Mapping[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])
    return "unknown"


async def check_builtin_login(request: Request, username: str) -> None:
    remote = _remote_address(request.scope)
    normalized_username = username.strip().casefold()[:255]
    account_digest = hashlib.sha256(
        normalized_username.encode("utf-8")
    ).hexdigest()
    await _login_remote_limiter.check(f"login-remote:{remote}")
    await _login_account_limiter.check(
        f"login-account:{account_digest}"
    )


class AuthEndpointRateLimitMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope.get("method") == "POST":
            path = scope.get("path", "")
            limiter = (
                _registration_limiter
                if path == "/register"
                else _token_limiter
                if path == "/token"
                else None
            )
            if limiter is not None:
                try:
                    await limiter.check(
                        f"{path}:{_remote_address(scope)}"
                    )
                except AuthRateLimitExceeded as exc:
                    response = JSONResponse(
                        {
                            "error": "rate_limit_exceeded",
                            "error_description": (
                                "Too many authentication requests."
                            ),
                        },
                        status_code=429,
                        headers={
                            "Retry-After": str(exc.retry_after),
                            "Cache-Control": "no-store",
                        },
                    )
                    await response(scope, receive, send)
                    return
        await self._app(scope, receive, send)


def reset_auth_rate_limiters() -> None:
    _login_remote_limiter.reset()
    _login_account_limiter.reset()
    _registration_limiter.reset()
    _token_limiter.reset()
