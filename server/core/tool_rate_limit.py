from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass

from server.auth.principal import Principal
from server.config import get_config


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: float) -> None:
        super().__init__("Tool rate limit exceeded")
        self.retry_after_seconds = retry_after_seconds


@dataclass
class _Bucket:
    tokens: float
    last_refill: float
    last_seen: float


class TokenBucketRateLimiter:
    def __init__(
        self,
        *,
        requests_per_second: int,
        max_entries: int = 4096,
        idle_ttl_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if requests_per_second <= 0:
            raise ValueError("requests_per_second must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._rate = float(requests_per_second)
        self._capacity = float(requests_per_second)
        self._max_entries = max_entries
        self._idle_ttl_seconds = idle_ttl_seconds
        self._clock = clock
        self._buckets: dict[Hashable, _Bucket] = {}
        self._lock = asyncio.Lock()

    @property
    def entry_count(self) -> int:
        return len(self._buckets)

    async def check(self, key: Hashable) -> None:
        now = self._clock()
        async with self._lock:
            self._cleanup(now)
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_entries:
                    oldest = min(
                        self._buckets,
                        key=lambda item: self._buckets[item].last_seen,
                    )
                    del self._buckets[oldest]
                bucket = _Bucket(
                    tokens=self._capacity,
                    last_refill=now,
                    last_seen=now,
                )
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(
                self._capacity, bucket.tokens + elapsed * self._rate
            )
            bucket.last_refill = now
            bucket.last_seen = now
            if bucket.tokens < 1.0:
                retry_after = (1.0 - bucket.tokens) / self._rate
                raise RateLimitExceeded(
                    max(retry_after, math.ulp(1.0))
                )
            bucket.tokens -= 1.0

    def reset(self) -> None:
        self._buckets.clear()

    def _cleanup(self, now: float) -> None:
        expired = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.last_seen > self._idle_ttl_seconds
        ]
        for key in expired:
            del self._buckets[key]


_list_limiter: TokenBucketRateLimiter | None = None
_describe_limiter: TokenBucketRateLimiter | None = None


def _principal_key(principal: Principal) -> tuple[str, str]:
    return principal.kind.value, principal.id


async def check_list_rate_limit(principal: Principal) -> None:
    global _list_limiter
    if _list_limiter is None:
        _list_limiter = TokenBucketRateLimiter(requests_per_second=5)
    await _list_limiter.check(_principal_key(principal))


async def check_describe_rate_limit(
    principal: Principal, db_instance_id: str
) -> None:
    global _describe_limiter
    if _describe_limiter is None:
        rate = (
            get_config()
            .polardb.tenant_provisioning
            .describe_max_requests_per_second
        )
        _describe_limiter = TokenBucketRateLimiter(
            requests_per_second=rate
        )
    await _describe_limiter.check(
        (*_principal_key(principal), db_instance_id)
    )


def reset_tool_rate_limiters() -> None:
    global _list_limiter, _describe_limiter
    _list_limiter = None
    _describe_limiter = None
