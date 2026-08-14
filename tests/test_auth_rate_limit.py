from __future__ import annotations

import pytest
from starlette.requests import Request

from server.auth.rate_limit import (
    AuthRateLimitExceeded,
    SlidingWindowRateLimiter,
    check_builtin_login,
    reset_auth_rate_limiters,
)


@pytest.fixture(autouse=True)
def clean_rate_limiters():
    reset_auth_rate_limiters()
    yield
    reset_auth_rate_limiters()


async def test_active_entries_never_exceed_hard_limit() -> None:
    limiter = SlidingWindowRateLimiter(
        requests=5,
        window_seconds=60,
        max_entries=2,
    )

    await limiter.check("first")
    await limiter.check("second")
    with pytest.raises(AuthRateLimitExceeded):
        await limiter.check("third")

    assert len(limiter._entries) == 2
    assert "third" not in limiter._entries


async def test_account_limit_is_shared_across_remote_addresses() -> None:
    for attempt in range(5):
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [],
            "client": (f"192.0.2.{attempt + 1}", 12345),
        })
        await check_builtin_login(request, "Target.User")

    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/auth/login",
        "headers": [],
        "client": ("198.51.100.1", 12345),
    })
    with pytest.raises(AuthRateLimitExceeded):
        await check_builtin_login(request, " target.user ")
