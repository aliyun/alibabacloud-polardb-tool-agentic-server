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


MetricSink = Callable[[DBInstanceMetricSample], None]
_metric_sink: MetricSink | None = None


def set_db_instance_metric_sink(sink: MetricSink | None) -> None:
    global _metric_sink
    _metric_sink = sink


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
    logger.info(
        "agentic database tool completed",
        extra={
            "metric": sample.name,
            "duration_seconds": sample.duration_seconds,
            **sample.labels,
        },
    )
    if _metric_sink is not None:
        try:
            _metric_sink(sample)
        except Exception:
            logger.exception("agentic database metric sink failed")


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
