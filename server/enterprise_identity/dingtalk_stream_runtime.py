"""Cancellable transport around the released Stream SDK's message dispatcher."""

from __future__ import annotations

import json
from urllib.parse import quote_plus, urlparse

import httpx
import websockets


class StreamReconnect(Exception):
    """The established socket closed normally and needs a fresh ticket."""


async def stream_once(client, config):
    # SDK 0.24.3 start() blocks on requests.post and swallows cancellation.
    # Keep its public event registration/dispatch; own the bounded transport.
    client.pre_start()
    async with httpx.AsyncClient(timeout=10) as http:
        response = await http.post(
            client.OPEN_CONNECTION_API,
            json={
                "clientId": config["client_id"],
                "clientSecret": config["client_secret"],
                "subscriptions": [{"type": "EVENT", "topic": "*"}],
                "ua": "pas-dingtalk-stream",
                "localIp": client.get_host_ip(),
            },
        )
        response.raise_for_status()
        connection = response.json()
    if not isinstance(connection, dict):
        raise ValueError("INVALID_STREAM_CONNECTION")
    endpoint, ticket = connection.get("endpoint"), connection.get("ticket")
    parsed = urlparse(endpoint if isinstance(endpoint, str) else "")
    if (
        parsed.scheme != "wss"
        or not (parsed.hostname or "").endswith(".dingtalk.com")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not isinstance(ticket, str)
        or not ticket
    ):
        raise ValueError("INVALID_STREAM_CONNECTION")
    try:
        async with websockets.connect(
            endpoint + "?ticket=" + quote_plus(ticket),
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=1048576,
            logger=client.logger,
        ) as websocket:
            client.websocket = websocket
            async for message in websocket:
                # Ordered dispatch avoids untracked tasks surviving lease loss.
                await client.background_task(json.loads(message))
        raise StreamReconnect()
    finally:
        client.websocket = None
