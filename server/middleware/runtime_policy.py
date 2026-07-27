from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp


@dataclass(frozen=True, slots=True)
class RuntimeAccessPolicy:
    mode: Literal["SETUP", "READY"] = "READY"
    cors_allowed_origins: tuple[str, ...] = ()
    sso_active: bool = False


class RuntimePolicyMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: ASGIApp,
        *,
        snapshot_provider: Callable[[], RuntimeAccessPolicy],
    ) -> None:
        super().__init__(app)
        self.snapshot_provider = snapshot_provider

    @staticmethod
    def _setup_path_allowed(path: str) -> bool:
        return (
            path in {"/", "/livez", "/readyz", "/api/config"}
            or path.startswith(("/assets/", "/setup"))
        )

    async def dispatch(
        self, request: Request, call_next
    ) -> Response:
        policy = self.snapshot_provider()
        origin = request.headers.get("origin")
        if (
            request.method == "OPTIONS"
            and origin in policy.cors_allowed_origins
        ):
            response: Response = Response(status_code=204)
        elif (
            policy.mode == "SETUP"
            and not self._setup_path_allowed(request.url.path)
        ):
            response = JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "SETUP_REQUIRED",
                        "message": "Initial configuration is required",
                    }
                },
            )
        else:
            response = await call_next(request)

        if origin in policy.cors_allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Credentials"] = "true"
            if request.method == "OPTIONS":
                response.headers["Access-Control-Allow-Methods"] = (
                    "GET, POST, PUT, PATCH, DELETE, OPTIONS"
                )
                response.headers["Access-Control-Allow-Headers"] = (
                    request.headers.get(
                        "access-control-request-headers",
                        "authorization, content-type",
                    )
                )
        if request.url.path == "/api/config":
            response.headers["Cache-Control"] = "no-store"
        return response
