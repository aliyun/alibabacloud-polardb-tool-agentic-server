import pytest

from server.auth.principal import Principal, PrincipalKind
from server.config import reset_config
from server.core.tool_rate_limit import (
    RateLimitExceeded,
    TokenBucketRateLimiter,
    check_describe_rate_limit,
    check_list_rate_limit,
    reset_tool_rate_limiters,
)


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


async def test_token_bucket_refills_with_monotonic_time():
    clock = Clock()
    limiter = TokenBucketRateLimiter(
        requests_per_second=5,
        max_entries=10,
        idle_ttl_seconds=60,
        clock=clock,
    )
    principal = Principal(PrincipalKind.AGENT, "agent-1")
    for _ in range(5):
        await limiter.check((principal.kind.value, principal.id))
    with pytest.raises(RateLimitExceeded) as error:
        await limiter.check((principal.kind.value, principal.id))
    assert 0 < error.value.retry_after_seconds <= 0.2

    clock.value += 0.2
    await limiter.check((principal.kind.value, principal.id))


async def test_token_bucket_cleanup_remains_bounded():
    clock = Clock()
    limiter = TokenBucketRateLimiter(
        requests_per_second=2,
        max_entries=3,
        idle_ttl_seconds=60,
        clock=clock,
    )
    for index in range(20):
        await limiter.check(("agent", str(index)))
    assert limiter.entry_count <= 3

    clock.value += 61
    await limiter.check(("agent", "last"))
    assert limiter.entry_count == 1


async def test_list_and_describe_limits_use_independent_scopes():
    reset_tool_rate_limiters()
    principal = Principal(PrincipalKind.AGENT, "agent-global")
    for _ in range(5):
        await check_list_rate_limit(principal)
    with pytest.raises(RateLimitExceeded):
        await check_list_rate_limit(principal)

    for _ in range(2):
        await check_describe_rate_limit(principal, "dbi-1")
    with pytest.raises(RateLimitExceeded):
        await check_describe_rate_limit(principal, "dbi-1")
    await check_describe_rate_limit(principal, "dbi-2")
    reset_tool_rate_limiters()


async def test_describe_limit_uses_runtime_snapshot_and_reset():
    from server import config as config_module
    from server.config import AppConfig

    principal = Principal(PrincipalKind.AGENT, "agent-configured")
    config_module._config = AppConfig(
        polardb={
            "tenant_provisioning": {
                "describe_max_requests_per_second": 7
            }
        }
    )
    reset_tool_rate_limiters()
    try:
        for _ in range(7):
            await check_describe_rate_limit(principal, "dbi-configured")
        with pytest.raises(RateLimitExceeded):
            await check_describe_rate_limit(principal, "dbi-configured")

        reset_tool_rate_limiters()
        for _ in range(7):
            await check_describe_rate_limit(principal, "dbi-configured")
    finally:
        reset_tool_rate_limiters()
        reset_config()
