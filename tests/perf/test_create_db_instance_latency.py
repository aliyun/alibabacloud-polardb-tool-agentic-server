from __future__ import annotations

import asyncio
import json
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass

import httpx
import pytest

REQUEST_COUNT = 1000
CONCURRENCY = 20

MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@dataclass(frozen=True)
class LatencySummary:
    count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    maximum_ms: float


def percentile(values: list[float], percentile_value: float) -> float:
    if not values:
        raise ValueError("At least one latency sample is required")
    if not 0 <= percentile_value <= 100:
        raise ValueError("Percentile must be between 0 and 100")
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile_value / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize(values: list[float]) -> LatencySummary:
    return LatencySummary(
        count=len(values),
        p50_ms=percentile(values, 50),
        p95_ms=percentile(values, 95),
        p99_ms=percentile(values, 99),
        maximum_ms=max(values),
    )


def _jsonrpc(tool: str, arguments: dict, request_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


def _event(text: str) -> dict:
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AssertionError("MCP response did not contain an SSE data event")


def _payload(response: httpx.Response) -> dict:
    event = _event(response.text)
    if "error" in event:
        raise AssertionError(f"MCP JSON-RPC error: {event['error']}")
    result = event["result"]
    payload = json.loads(result["content"][0]["text"])
    if result.get("isError"):
        raise AssertionError(f"MCP Tool error: {payload}")
    return payload


def _server_duration_ms(response: httpx.Response) -> float:
    value = response.headers.get("server-timing", "")
    prefix = "agentic_db_tool;dur="
    if not value.startswith(prefix):
        raise AssertionError("Server-Timing does not contain agentic_db_tool")
    return float(value[len(prefix) :])


def test_latency_summary_uses_nearest_rank_percentiles():
    summary = summarize([float(value) for value in range(1, 101)])
    assert summary == LatencySummary(
        count=100,
        p50_ms=50.0,
        p95_ms=95.0,
        p99_ms=99.0,
        maximum_ms=100.0,
    )


@pytest.mark.performance
async def test_create_db_instance_reports_configured_environment_latency():
    url = os.getenv("PAS_PERF_MCP_URL", "").rstrip("/")
    token = os.getenv("PAS_PERF_AGENT_TOKEN", "")
    metadb_type = os.getenv("PAS_PERF_METADB_TYPE", "").lower()
    expected_environment = os.getenv("PAS_PERF_ENVIRONMENT", "")
    if not url or not token or not metadb_type or not expected_environment:
        pytest.skip(
            "Set PAS_PERF_MCP_URL, PAS_PERF_AGENT_TOKEN, and "
            "PAS_PERF_METADB_TYPE and PAS_PERF_ENVIRONMENT to measure an "
            "explicit environment"
        )
    if metadb_type not in {"mysql", "postgresql"}:
        pytest.fail("Performance acceptance requires MySQL or PostgreSQL metadb")

    endpoint = url if url.endswith("/mcp") else f"{url}/mcp"
    headers = {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
    semaphore = asyncio.Semaphore(CONCURRENCY)
    client_latencies_ms: list[float] = []
    server_latencies_ms: list[float] = []
    created_instance_ids: list[str] = []
    cleanup_request_failures = 0
    run_id = uuid.uuid4().hex[:12]

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        limits=httpx.Limits(
            max_connections=CONCURRENCY,
            max_keepalive_connections=CONCURRENCY,
        ),
    ) as client:

        async def create_one(index: int) -> None:
            async with semaphore:
                started_at = time.perf_counter()
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json=_jsonrpc(
                        "create_db_instance",
                        {
                            "client_token": f"perf-{run_id}-{index:04d}",
                            "db_type": "polardb_mysql",
                        },
                        index + 1,
                    ),
                )
                client_latencies_ms.append(
                    (time.perf_counter() - started_at) * 1000
                )
                response.raise_for_status()
                payload = _payload(response)
                if payload.get("status") != "CREATING":
                    raise AssertionError(f"Unexpected create response: {payload}")
                created_instance_ids.append(payload["db_instance_id"])
                server_latencies_ms.append(_server_duration_ms(response))

        try:
            await asyncio.gather(
                *(create_one(index) for index in range(REQUEST_COUNT))
            )
        finally:
            async def delete_one(index: int, instance_id: str) -> None:
                async with semaphore:
                    response = await client.post(
                        endpoint,
                        headers=headers,
                        json=_jsonrpc(
                            "delete_db_instance",
                            {"db_instance_id": instance_id},
                            REQUEST_COUNT + index + 1,
                        ),
                    )
                    response.raise_for_status()
                    payload = _payload(response)
                    if payload.get("status") not in {"DELETING", "DELETED"}:
                        raise AssertionError(f"Unexpected delete response: {payload}")

            cleanup_results = await asyncio.gather(
                *(
                    delete_one(index, instance_id)
                    for index, instance_id in enumerate(created_instance_ids)
                ),
                return_exceptions=True,
            )
            cleanup_request_failures = sum(
                isinstance(result, BaseException) for result in cleanup_results
            )

    server = summarize(server_latencies_ms)
    client_side = summarize(client_latencies_ms)
    report = {
        "request_count": REQUEST_COUNT,
        "concurrency": CONCURRENCY,
        "environment": expected_environment,
        "metadb_type": metadb_type,
        "cleanup_request_failures": cleanup_request_failures,
        "server": asdict(server),
        "client": asdict(client_side),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    assert server.count == REQUEST_COUNT
    assert client_side.count == REQUEST_COUNT
    assert cleanup_request_failures == 0, report
