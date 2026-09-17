from __future__ import annotations

from starlette.responses import JSONResponse

from server.features.knowledge import KnowledgeUnavailable


def knowledge_path(path: str) -> bool:
    return (
        path.startswith(("/api/polarrag/", "/api/me/polarrag/"))
        or path == "/api/polarrag"
        or path.startswith("/api/v1/")
        and not path.startswith("/api/v1/external-auth")
        or path.startswith("/api/agents/")
        and any(part in path for part in ("/polarrag-bindings", "/enterprise-access/"))
    )


class KnowledgeAdmissionMiddleware:
    def __init__(self, app, runtime_provider):
        self.app = app
        self.runtime_provider = runtime_provider

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not knowledge_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        feature = self.runtime_provider()
        try:
            if feature is None:
                raise KnowledgeUnavailable()
            async with feature.operation("http:" + scope["method"]):
                await self.app(scope, receive, send)
        except KnowledgeUnavailable as error:
            response = JSONResponse(
                {"detail": {"code": error.code, "message": error.message}},
                status_code=503,
                headers={"Retry-After": "5", "Cache-Control": "no-store"},
            )
            await response(scope, receive, send)
