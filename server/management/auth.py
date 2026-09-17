from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable

from fastapi import HTTPException, Request

from server.management.settings import ListenerSettings


ManagementAuthenticator = Callable[[Request], Awaitable[None]]


def management_authenticator(
    settings: ListenerSettings,
) -> ManagementAuthenticator:
    async def authenticate(request: Request) -> None:
        if settings.auth_mode == "trusted-network":
            return

        authorization = request.headers.get("authorization", "")
        scheme, separator, value = authorization.partition(" ")
        presented = (
            value.encode("utf-8")
            if separator and scheme.lower() == "bearer"
            else b""
        )
        expected = settings.token or b""
        if not hmac.compare_digest(presented, expected):
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "MANAGEMENT_UNAUTHORIZED",
                    "message": "Management authentication failed",
                },
            )

    return authenticate
