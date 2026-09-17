"""Explicitly assemble the enabled capability in API tests without a lifespan."""
from contextlib import asynccontextmanager
from types import SimpleNamespace


def enable_knowledge_routes(app):
    from server.api.router import build_knowledge_router

    @asynccontextmanager
    async def operation(*_args, **_kwargs):
        yield

    app.state.knowledge_runtime = SimpleNamespace(operation=operation)
    app.router.routes[0:0] = build_knowledge_router().routes
    return app
