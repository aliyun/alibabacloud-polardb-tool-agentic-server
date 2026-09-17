from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from server.mcp.knowledge_tools import POLARRAG_TOOL_NAMES


def _tool_name(body: bytes) -> str | None:
    try:
        request = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return None
    params = request.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    if isinstance(name, str) and name in POLARRAG_TOOL_NAMES:
        return name
    return None


def _limited_retry_after(body: bytes) -> int | None:
    try:
        lines = body.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    for line in lines:
        if not line.startswith("data: "):
            continue
        try:
            event = json.loads(line[6:])
            content = event.get("result", {}).get("content", [])
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                payload = json.loads(item.get("text", ""))
                if not isinstance(payload, dict):
                    continue
                retry_after = payload.get("retry_after_seconds")
                if (
                    payload.get("error") == "POLARRAG_TOOL_LIMITED"
                    and isinstance(retry_after, int)
                    and not isinstance(retry_after, bool)
                    and retry_after > 0
                ):
                    return retry_after
        except (AttributeError, json.JSONDecodeError, TypeError):
            continue
    return None


class PolarRAGGovernanceHTTPMiddleware:
    """Map a PolarRAG governance Tool result to HTTP overload semantics."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return

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

        await self._app(scope, replay_receive, capture_send)
        response_body = b"".join(
            message.get("body", b"") for message in response_messages if message["type"] == "http.response.body"
        )
        retry_after = _limited_retry_after(response_body)
        for message in response_messages:
            if retry_after is not None and message["type"] == "http.response.start":
                headers = [header for header in message.get("headers", []) if header[0].lower() != b"retry-after"]
                headers.append((b"retry-after", str(retry_after).encode("ascii")))
                message = {**message, "status": 429, "headers": headers}
            await send(message)
