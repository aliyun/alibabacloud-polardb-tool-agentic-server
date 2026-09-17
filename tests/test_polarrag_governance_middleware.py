from __future__ import annotations

import asyncio
import json

import pytest

from server.core.polarrag_governance_http import (
    PolarRAGGovernanceHTTPMiddleware,
)


def _request(tool: str) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        }
    ).encode()


def _response(payload: dict) -> bytes:
    event = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload),
                }
            ],
            "isError": True,
        },
    }
    return f"event: message\ndata: {json.dumps(event)}\n\n".encode()


async def _invoke(tool: str, payload: dict):
    request = _request(tool)
    response = _response(payload)
    request_sent = False
    sent = []

    async def receive():
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {
            "type": "http.request",
            "body": request,
            "more_body": False,
        }

    async def send(message):
        sent.append(message)

    async def inner(_scope, inner_receive, inner_send):
        replayed = await inner_receive()
        assert replayed["body"] == request
        await inner_send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await inner_send(
            {
                "type": "http.response.body",
                "body": response,
                "more_body": False,
            }
        )

    middleware = PolarRAGGovernanceHTTPMiddleware(inner)
    await middleware(
        {"type": "http", "method": "POST", "path": "/mcp"},
        receive,
        send,
    )
    return sent


async def test_limited_polarrag_tool_response_becomes_http_429() -> None:
    sent = await _invoke(
        "kb_search",
        {
            "error": "POLARRAG_TOOL_LIMITED",
            "message": "PolarRAG Tool capacity is temporarily unavailable.",
            "reason": "RATE_LIMIT",
            "retry_after_seconds": 3,
        },
    )

    start = sent[0]
    assert start["status"] == 429
    assert (b"retry-after", b"3") in start["headers"]
    assert b"POLARRAG_TOOL_LIMITED" in sent[1]["body"]


async def test_other_polarrag_error_remains_http_200() -> None:
    sent = await _invoke(
        "kb_search",
        {"error": "POLARRAG_UNAVAILABLE", "message": "unavailable"},
    )

    assert sent[0]["status"] == 200
    assert all(name != b"retry-after" for name, _value in sent[0]["headers"])


async def test_non_polarrag_tool_is_not_rewritten() -> None:
    sent = await _invoke(
        "run_sql",
        {
            "error": "POLARRAG_TOOL_LIMITED",
            "retry_after_seconds": 3,
        },
    )

    assert sent[0]["status"] == 200


async def test_multichunk_governance_result_is_rewritten_without_body_change() -> None:
    request = _request("kb_search")
    response = _response(
        {
            "error": "POLARRAG_TOOL_LIMITED",
            "retry_after_seconds": 7,
        }
    )
    split = len(response) // 2
    sent = []
    request_sent = False

    async def send(message):
        sent.append(message)

    async def receive():
        nonlocal request_sent
        if request_sent:
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": request, "more_body": False}

    async def inner(_scope, inner_receive, inner_send):
        await inner_receive()
        await inner_send({"type": "http.response.start", "status": 200, "headers": []})
        await inner_send(
            {
                "type": "http.response.body",
                "body": response[:split],
                "more_body": True,
            }
        )
        await inner_send(
            {
                "type": "http.response.body",
                "body": response[split:],
                "more_body": False,
            }
        )

    await PolarRAGGovernanceHTTPMiddleware(inner)(
        {"type": "http", "method": "POST", "path": "/mcp"},
        receive,
        send,
    )

    assert sent[0]["status"] == 429
    assert (b"retry-after", b"7") in sent[0]["headers"]
    assert b"".join(message.get("body", b"") for message in sent[1:]) == response
    assert sent[1]["more_body"] is True


async def test_disconnect_after_request_is_replayed_to_inner_app() -> None:
    request = _request("kb_search")
    messages = [
        {"type": "http.request", "body": request, "more_body": False},
        {"type": "http.disconnect"},
    ]
    disconnect_seen = asyncio.Event()

    async def receive():
        return messages.pop(0)

    async def inner(_scope, inner_receive, _inner_send):
        await inner_receive()
        assert await inner_receive() == {"type": "http.disconnect"}
        disconnect_seen.set()

    await PolarRAGGovernanceHTTPMiddleware(inner)(
        {"type": "http", "method": "POST", "path": "/mcp"},
        receive,
        lambda _message: None,
    )

    assert disconnect_seen.is_set()


async def test_cancellation_propagates_to_slow_inner_app() -> None:
    request = _request("kb_search")
    request_sent = False
    inner_cancelled = asyncio.Event()

    async def receive():
        nonlocal request_sent
        if request_sent:
            await asyncio.Event().wait()
        request_sent = True
        return {"type": "http.request", "body": request, "more_body": False}

    async def send(_message):
        return None

    async def inner(_scope, inner_receive, inner_send):
        await inner_receive()
        await inner_send({"type": "http.response.start", "status": 200, "headers": []})
        try:
            await asyncio.Event().wait()
        finally:
            inner_cancelled.set()

    task = asyncio.create_task(
        PolarRAGGovernanceHTTPMiddleware(inner)(
            {"type": "http", "method": "POST", "path": "/mcp"},
            receive,
            send,
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner_cancelled.is_set()
