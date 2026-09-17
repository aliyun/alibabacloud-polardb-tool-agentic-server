from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from server.config import PolarRAGToolLimitsConfig, get_config

logger = logging.getLogger(__name__)

DEFAULT_MAX_RATE_BUCKETS = 10_000
DEFAULT_BUCKET_SWEEP_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class PolarRAGGovernanceMetricSample:
    name: str
    value: float
    labels: dict[str, str]


MetricSink = Callable[[PolarRAGGovernanceMetricSample], None]
_metric_sink: MetricSink | None = None


def set_polarrag_governance_metric_sink(
    sink: MetricSink | None,
) -> None:
    global _metric_sink
    _metric_sink = sink


def _emit(sample: PolarRAGGovernanceMetricSample) -> None:
    logger.info(
        "polarrag tool governance metric",
        extra={"metric": sample.name, "value": sample.value, **sample.labels},
    )
    if _metric_sink is None:
        return
    try:
        _metric_sink(sample)
    except Exception:
        logger.exception("polarrag governance metric sink failed")


class PolarRAGGovernanceError(RuntimeError):
    def __init__(
        self,
        *,
        internal_reason: str,
        public_reason: str,
        retry_after_seconds: int,
        requested_fanout: int | None = None,
        current_inflight: int | None = None,
    ) -> None:
        super().__init__("PolarRAG Tool capacity is temporarily unavailable.")
        self.internal_reason = internal_reason
        self.public_reason = public_reason
        self.retry_after_seconds = max(1, retry_after_seconds)
        self.requested_fanout = requested_fanout
        self.current_inflight = current_inflight

    def public_payload(self) -> dict[str, object]:
        return {
            "error": "POLARRAG_TOOL_LIMITED",
            "message": str(self),
            "reason": self.public_reason,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(slots=True)
class _TokenBucket:
    tokens: float
    last_refill: float


ConfigProvider = Callable[[], PolarRAGToolLimitsConfig]
Clock = Callable[[], float]


class PolarRAGToolGovernor:
    def __init__(
        self,
        config_provider: ConfigProvider,
        *,
        clock: Clock = time.monotonic,
        max_rate_buckets: int = DEFAULT_MAX_RATE_BUCKETS,
        bucket_sweep_interval_seconds: float = (DEFAULT_BUCKET_SWEEP_INTERVAL_SECONDS),
    ) -> None:
        if max_rate_buckets < 2:
            raise ValueError("max_rate_buckets must be at least 2")
        if bucket_sweep_interval_seconds <= 0:
            raise ValueError("bucket_sweep_interval_seconds must be positive")
        self._config_provider = config_provider
        self._clock = clock
        self._max_rate_buckets = max_rate_buckets
        self._bucket_sweep_interval_seconds = bucket_sweep_interval_seconds
        self._last_bucket_sweep = clock()
        self._lock = asyncio.Lock()
        self._signature: tuple[object, ...] | None = None
        self._buckets: dict[tuple[str, str], _TokenBucket] = {}
        self._instance_inflight: dict[str, int] = {}

    @property
    def rate_bucket_count(self) -> int:
        return len(self._buckets)

    @staticmethod
    def _config_signature(
        config: PolarRAGToolLimitsConfig,
    ) -> tuple[object, ...]:
        return tuple(config.model_dump().items())

    def _current_config_locked(self) -> PolarRAGToolLimitsConfig:
        config = self._config_provider()
        signature = self._config_signature(config)
        if signature != self._signature:
            self._signature = signature
            self._buckets.clear()
        return config

    def _bucket_locked(
        self,
        key: tuple[str, str],
        *,
        requests_per_minute: int,
        burst: int,
        now: float,
    ) -> _TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _TokenBucket(tokens=float(burst), last_refill=now)
            self._buckets[key] = bucket
            return bucket
        elapsed = max(0.0, now - bucket.last_refill)
        bucket.tokens = min(
            float(burst),
            bucket.tokens + elapsed * (requests_per_minute / 60.0),
        )
        bucket.last_refill = now
        return bucket

    @staticmethod
    def _rate_and_burst(
        scope: str,
        config: PolarRAGToolLimitsConfig,
    ) -> tuple[int, int]:
        if scope == "agent":
            return (
                config.agent_requests_per_minute,
                config.agent_burst,
            )
        return config.user_requests_per_minute, config.user_burst

    def _sweep_buckets_locked(
        self,
        config: PolarRAGToolLimitsConfig,
        now: float,
    ) -> None:
        if now - self._last_bucket_sweep < self._bucket_sweep_interval_seconds:
            return
        self._last_bucket_sweep = now
        expired = []
        for key, bucket in self._buckets.items():
            _rate, burst = self._rate_and_burst(key[0], config)
            refilled = self._refilled_tokens(key, bucket, config, now)
            if refilled >= burst:
                expired.append(key)
        for key in expired:
            self._buckets.pop(key, None)

    def _refilled_tokens(
        self,
        key: tuple[str, str],
        bucket: _TokenBucket,
        config: PolarRAGToolLimitsConfig,
        now: float,
    ) -> float:
        rate, burst = self._rate_and_burst(key[0], config)
        return min(
            float(burst),
            bucket.tokens + max(0.0, now - bucket.last_refill) * (rate / 60.0),
        )

    def _reserve_bucket_capacity_locked(
        self,
        keys: list[tuple[str, str]],
        config: PolarRAGToolLimitsConfig,
        now: float,
    ) -> int | None:
        missing = sum(key not in self._buckets for key in keys)
        excess = len(self._buckets) + missing - self._max_rate_buckets
        if excess <= 0:
            return None
        protected = set(keys)
        available = [(key, bucket) for key, bucket in self._buckets.items() if key not in protected]
        full = sorted(
            (
                (bucket.last_refill, key)
                for key, bucket in available
                if self._refilled_tokens(key, bucket, config, now) >= self._rate_and_burst(key[0], config)[1]
            ),
            key=lambda item: item[0],
        )
        for _last_refill, key in full[:excess]:
            self._buckets.pop(key, None)
        remaining = len(self._buckets) + missing - self._max_rate_buckets
        if remaining <= 0:
            return None
        waits = []
        for key, bucket in available:
            if key not in self._buckets:
                continue
            rate, burst = self._rate_and_burst(key[0], config)
            tokens = self._refilled_tokens(key, bucket, config, now)
            waits.append((burst - tokens) / (rate / 60.0))
        return max(1, math.ceil(min(waits))) if waits else 1

    async def check_rate(
        self,
        tool: str,
        user_id: str,
        agent_id: str | None,
    ) -> None:
        async with self._lock:
            config = self._current_config_locked()
            if not config.enabled:
                return
            now = self._clock()
            self._sweep_buckets_locked(config, now)
            candidates = [
                (
                    "user",
                    user_id,
                    config.user_requests_per_minute,
                    config.user_burst,
                )
            ]
            if agent_id is not None:
                candidates.append(
                    (
                        "agent",
                        agent_id,
                        config.agent_requests_per_minute,
                        config.agent_burst,
                    )
                )
            capacity_retry_after = self._reserve_bucket_capacity_locked(
                [(scope, identifier) for scope, identifier, _rate, _burst in candidates],
                config,
                now,
            )
            if capacity_retry_after is not None:
                _emit(
                    PolarRAGGovernanceMetricSample(
                        name="polarrag_tool_rejections_total",
                        value=1.0,
                        labels={
                            "tool": tool,
                            "reason": "rate_bucket_capacity",
                        },
                    )
                )
                raise PolarRAGGovernanceError(
                    internal_reason="rate_bucket_capacity",
                    public_reason="RATE_LIMIT",
                    retry_after_seconds=capacity_retry_after,
                )
            evaluated: list[tuple[str, _TokenBucket, int]] = []
            for scope, identifier, rate, burst in candidates:
                evaluated.append(
                    (
                        scope,
                        self._bucket_locked(
                            (scope, identifier),
                            requests_per_minute=rate,
                            burst=burst,
                            now=now,
                        ),
                        rate,
                    )
                )
            rejected = [(scope, bucket, rate) for scope, bucket, rate in evaluated if bucket.tokens < 1.0]
            if rejected:
                scope = rejected[0][0]
                retry_after = max(
                    max(
                        1,
                        math.ceil((1.0 - bucket.tokens) / (rate / 60.0)),
                    )
                    for _scope, bucket, rate in rejected
                )
                reason = f"{scope}_rate_limit"
                _emit(
                    PolarRAGGovernanceMetricSample(
                        name="polarrag_tool_rejections_total",
                        value=1.0,
                        labels={"tool": tool, "reason": reason},
                    )
                )
                raise PolarRAGGovernanceError(
                    internal_reason=reason,
                    public_reason="RATE_LIMIT",
                    retry_after_seconds=retry_after,
                )
            for _scope, bucket, _rate in evaluated:
                bucket.tokens -= 1.0

    async def upstream_wave_limit(self) -> int:
        async with self._lock:
            config = self._current_config_locked()
            if not config.enabled:
                return config.max_fanout
            return min(config.max_fanout, config.instance_max_inflight)

    @asynccontextmanager
    async def reserve_instance(
        self,
        tool: str,
        instance_id: str,
        fanout: int,
    ) -> AsyncIterator[None]:
        if fanout < 1:
            raise ValueError("fanout must be positive")
        reserved = False
        async with self._lock:
            config = self._current_config_locked()
            if config.enabled:
                if fanout > config.max_fanout:
                    _emit(
                        PolarRAGGovernanceMetricSample(
                            name="polarrag_tool_rejections_total",
                            value=1.0,
                            labels={
                                "tool": tool,
                                "reason": "fanout_limit",
                            },
                        )
                    )
                    raise PolarRAGGovernanceError(
                        internal_reason="fanout_limit",
                        public_reason="FANOUT_LIMIT",
                        retry_after_seconds=config.retry_after_seconds,
                        requested_fanout=fanout,
                    )
                current = self._instance_inflight.get(instance_id, 0)
                if current + fanout > config.instance_max_inflight:
                    _emit(
                        PolarRAGGovernanceMetricSample(
                            name="polarrag_tool_rejections_total",
                            value=1.0,
                            labels={
                                "tool": tool,
                                "reason": "instance_concurrency",
                            },
                        )
                    )
                    raise PolarRAGGovernanceError(
                        internal_reason="instance_concurrency",
                        public_reason="INSTANCE_CONCURRENCY",
                        retry_after_seconds=config.retry_after_seconds,
                        requested_fanout=fanout,
                        current_inflight=current,
                    )
                current += fanout
                self._instance_inflight[instance_id] = current
                reserved = True
                self._emit_inflight(instance_id, current)
        try:
            yield
        finally:
            if reserved:
                async with self._lock:
                    current = max(
                        0,
                        self._instance_inflight.get(instance_id, 0) - fanout,
                    )
                    if current:
                        self._instance_inflight[instance_id] = current
                    else:
                        self._instance_inflight.pop(instance_id, None)
                    self._emit_inflight(instance_id, current)

    @staticmethod
    def _emit_inflight(instance_id: str, current: int) -> None:
        scope = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:12]
        _emit(
            PolarRAGGovernanceMetricSample(
                name="polarrag_tool_instance_inflight",
                value=float(current),
                labels={"instance_scope": scope},
            )
        )


def _runtime_limits() -> PolarRAGToolLimitsConfig:
    return get_config().polarrag_tool_limits


_governor = PolarRAGToolGovernor(_runtime_limits)


def get_polarrag_tool_governor() -> PolarRAGToolGovernor:
    return _governor


def reset_polarrag_tool_governor() -> None:
    global _governor
    _governor = PolarRAGToolGovernor(_runtime_limits)
