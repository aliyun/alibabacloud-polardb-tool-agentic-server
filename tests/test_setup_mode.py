from __future__ import annotations

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.middleware.runtime_policy import (
    RuntimeAccessPolicy,
    RuntimePolicyMiddleware,
)


async def test_setup_mode_allows_only_setup_surface() -> None:
    app = FastAPI()
    app.add_middleware(
        RuntimePolicyMiddleware,
        snapshot_provider=lambda: RuntimeAccessPolicy(mode="SETUP"),
    )

    @app.get("/livez")
    async def livez():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        return {"status": "ok", "mode": "SETUP"}

    @app.post("/api/config")
    async def config():
        return {"ok": True}

    @app.post("/auth/login")
    async def login():
        return {"ok": True}

    @app.get("/api/users")
    async def users():
        return {"ok": True}

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        assert (await client.get("/livez")).status_code == 200
        ready = await client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["mode"] == "SETUP"
        assert (await client.post("/api/config")).status_code == 200
        login = await client.post("/auth/login")
        assert login.status_code == 503
        assert login.json()["detail"]["code"] == "SETUP_REQUIRED"
        assert (await client.get("/api/users")).status_code == 503


async def test_dynamic_cors_allows_only_configured_origin() -> None:
    policy = RuntimeAccessPolicy(
        mode="READY",
        cors_allowed_origins=("https://console.example.com",),
    )
    app = FastAPI()
    app.add_middleware(
        RuntimePolicyMiddleware,
        snapshot_provider=lambda: policy,
    )

    @app.get("/livez")
    async def livez():
        return {"status": "ok"}

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        allowed = await client.get(
            "/livez",
            headers={"Origin": "https://console.example.com"},
        )
        assert allowed.headers["access-control-allow-origin"] == (
            "https://console.example.com"
        )
        rejected = await client.get(
            "/livez",
            headers={"Origin": "https://evil.example"},
        )
        assert "access-control-allow-origin" not in rejected.headers
