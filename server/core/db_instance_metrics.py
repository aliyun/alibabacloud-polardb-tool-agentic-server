from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

_TOOLS = {
    "create_db_instance",
    "describe_db_instance",
    "delete_db_instance",
}


@dataclass(frozen=True)
class DBInstanceMetricSample:
    name: str
    duration_seconds: float
    labels: dict[str, str]
    value: float = 1.0


MetricSink = Callable[[DBInstanceMetricSample], None]
_metric_sink: MetricSink | None = None

_DEDICATED_SIGNAL_OUTCOMES = {
    "allocation": frozenset(
        {"hot_hit", "cold_miss", "idempotent_replay", "failed"}
    ),
    "capacity": frozenset(
        {
            "snapshot",
            "planning_deficit",
            "capacity_limited",
            "rate_limited",
        }
    ),
    "readiness": frozenset(
        {
            "stale_excluded",
            "check_started",
            "fresh",
            "failed",
            "inconclusive",
        }
    ),
    "replenishment": frozenset(
        {
            "purchase_reserved",
            "not_required",
            "capacity_limited",
            "rate_limited",
            "succeeded",
            "failed",
        }
    ),
    "lifecycle": frozenset(
        {
            "delete_requested",
            "disconnected",
            "cooling_down",
            "restored",
            "sanitized",
            "destroyed",
            "failed",
        }
    ),
    "permission_sync": frozenset(
        {"dry_run", "succeeded", "failed"}
    ),
}
_DEDICATED_CAPACITY_KINDS = frozenset(
    {
        "allocatable",
        "planning",
        "billable_total",
        "surplus",
        "stale",
        "checking",
        "quarantined",
    }
)


def set_db_instance_metric_sink(sink: MetricSink | None) -> None:
    global _metric_sink
    _metric_sink = sink


def _publish(sample: DBInstanceMetricSample, message: str) -> None:
    logger.info(
        message,
        extra={
            "metric": sample.name,
            "metric_value": sample.value,
            "duration_seconds": sample.duration_seconds,
            **sample.labels,
        },
    )
    if _metric_sink is not None:
        try:
            _metric_sink(sample)
        except Exception:
            logger.exception("agentic database metric sink failed")


def emit_dedicated_pool_signal(
    *,
    signal: str,
    outcome: str,
    value: float = 1.0,
    duration_seconds: float = 0.0,
) -> None:
    """Emit a Dedicated lifecycle signal with strictly bounded labels.

    Pool, member, Agent, database, account, endpoint, token, and exception
    values are deliberately absent. Operators correlate individual requests
    through the structured request/resource logs instead of metric labels.
    """
    outcomes = _DEDICATED_SIGNAL_OUTCOMES.get(signal)
    if outcomes is None or outcome not in outcomes:
        raise ValueError("Unsupported Dedicated metric signal or outcome")
    sample = DBInstanceMetricSample(
        name="agentic_db_dedicated_pool_event_total",
        duration_seconds=max(0.0, duration_seconds),
        labels={
            "signal": signal,
            "outcome": outcome,
            "backend_type": "dedicated_pool",
        },
        value=max(0.0, value),
    )
    _publish(sample, "dedicated pool lifecycle signal")


def emit_dedicated_pool_capacity(*, kind: str, value: float) -> None:
    if kind not in _DEDICATED_CAPACITY_KINDS:
        raise ValueError("Unsupported Dedicated capacity metric kind")
    sample = DBInstanceMetricSample(
        name="agentic_db_dedicated_pool_capacity",
        duration_seconds=0.0,
        labels={
            "kind": kind,
            "backend_type": "dedicated_pool",
        },
        value=max(0.0, value),
    )
    _publish(sample, "dedicated pool capacity snapshot")


def _emit(tool: str, outcome: str, duration_seconds: float) -> None:
    sample = DBInstanceMetricSample(
        name="agentic_db_tool_duration_seconds",
        duration_seconds=max(0.0, duration_seconds),
        labels={
            "tool": tool,
            "outcome": outcome,
            "backend_type": "multitenant",
        },
    )
    _publish(sample, "agentic database tool completed")


def emit_mcp_omitted_provisioning_mode() -> None:
    sample = DBInstanceMetricSample(
        name="agentic_db_mcp_omitted_provisioning_mode_total",
        duration_seconds=0.0,
        labels={
            "surface": "mcp",
            "default_mode": "multitenant",
        },
    )
    logger.warning(
        "MCP create_db_instance omitted provisioning_mode; defaulting to "
        "multitenant. Removal requires zero omitted-mode calls in every "
        "active environment for four consecutive weeks and one announced "
        "release cycle.",
        extra={
            "metric": sample.name,
            "deprecation": "mcp_omitted_provisioning_mode",
            **sample.labels,
        },
    )
    _publish(sample, "MCP provisioning mode compatibility signal")


def emit_dedicated_pool_guard_metric(
    *,
    guard: str,
    outcome: str,
) -> None:
    sample = DBInstanceMetricSample(
        name="agentic_db_dedicated_pool_guard_total",
        duration_seconds=0.0,
        labels={
            "guard": guard,
            "outcome": outcome,
            "backend_type": "dedicated_pool",
        },
    )
    logger.info(
        "dedicated pool guard evaluated",
        extra={"metric": sample.name, **sample.labels},
    )
    _publish(sample, "dedicated pool guard signal")


def _tool_name(body: bytes) -> str | None:
    try:
        request = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(request, dict):
        return None
    if request.get("method") != "tools/call":
        return None
    params = request.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    return name if isinstance(name, str) and name in _TOOLS else None


def _response_outcome(status_code: int, body: bytes) -> str:
    if status_code >= 400:
        return "error"
    try:
        for line in body.decode("utf-8").splitlines():
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if "error" in event or event.get("result", {}).get("isError") is True:
                return "error"
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return "error"
    return "ok"


class DBInstanceMetricsMiddleware:
    """Measure selected MCP Tool calls across auth, dispatch, and serialization."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

        started_at = time.perf_counter()
        request_messages: list[Message] = []
        request_body = bytearray()
        while True:
            message = await receive()
            request_messages.append(message)
            if message["type"] == "http.request":
                request_body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        tool = _tool_name(bytes(request_body))
        message_index = 0

        async def replay_receive() -> Message:
            nonlocal message_index
            if message_index < len(request_messages):
                message = request_messages[message_index]
                message_index += 1
                return message
            return await receive()

        if tool is None:
            await self._app(scope, replay_receive, send)
            return

        response_messages: list[Message] = []

        async def capture_send(message: Message) -> None:
            response_messages.append(message)

        try:
            await self._app(scope, replay_receive, capture_send)
        except BaseException:
            _emit(tool, "error", time.perf_counter() - started_at)
            raise

        duration_seconds = max(0.0, time.perf_counter() - started_at)
        status_code = 500
        response_body = bytearray()
        try:
            for message in response_messages:
                if message["type"] == "http.response.start":
                    status_code = message["status"]
                    headers = [
                        header
                        for header in message.get("headers", [])
                        if header[0].lower() != b"server-timing"
                    ]
                    headers.append(
                        (
                            b"server-timing",
                            f"agentic_db_tool;dur={duration_seconds * 1000:.3f}".encode(
                                "ascii"
                            ),
                        )
                    )
                    message = {**message, "headers": headers}
                elif message["type"] == "http.response.body":
                    response_body.extend(message.get("body", b""))
                await send(message)
        except BaseException:
            _emit(tool, "error", time.perf_counter() - started_at)
            raise
        _emit(
            tool,
            _response_outcome(status_code, bytes(response_body)),
            duration_seconds,
        )
