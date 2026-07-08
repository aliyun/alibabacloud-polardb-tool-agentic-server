from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from server.auth.builtin import ensure_admin_exists
from server.api.router import router as api_router
from server.auth.router import router as auth_router
from server.config import get_config
from server.db.engine import get_engine, get_session_factory
from server.logging import setup_logging, trace_id_var, generate_request_id
from server.mcp.server import router as mcp_router
from server.auth.cleanup import sweep_expired_oauth_rows
from server.mcp.transport import create_mcp_app, mcp_lifespan

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = get_config()
    setup_logging(config.server.log_level, config.logging)
    logger.info("alibabacloud polardb tool agentic server starting", extra={"action": "startup"})

    get_engine()
    session_factory = get_session_factory()
    app.state.session_factory = session_factory
    app.state.background_tasks = set()

    async with session_factory() as session:
        await ensure_admin_exists(session)

    from server.auth.jwt_manager import initialize_jwt_keys_from_db

    async with session_factory() as session:
        await initialize_jwt_keys_from_db(session)

    # Ensure default department exists if configured
    default_dept = config.auth.default_department
    if default_dept:
        async with session_factory() as session:
            from sqlalchemy import select
            from server.models.department import Department

            existing = (await session.execute(
                select(Department).where(Department.name == default_dept)
            )).scalar_one_or_none()
            if existing is None:
                session.add(Department(name=default_dept, description="Default department"))
                await session.commit()
                logger.info("Created default department '%s'", default_dept)

    # Startup recovery sweep
    try:
        from server.core.provisioner import startup_recovery_sweep

        await startup_recovery_sweep(session_factory, app.state.background_tasks)
    except Exception:
        logger.exception("startup recovery sweep failed, continuing")

    # Startup safety check
    async with session_factory() as session:
        from server.core.settings_manager import get_setting

        pool_target = int(await get_setting(session, "pool_target_size", default="0") or "0")
        ip_list = await get_setting(session, "pool_security_ip_list", default="127.0.0.1")
        if pool_target > 0 and ip_list == "127.0.0.1":
            logger.warning(
                "pool_target_size=%d but pool_security_ip_list is still 127.0.0.1 — agents cannot connect",
                pool_target,
            )

    # Share background_tasks with MCP transport module
    from server.mcp.transport import set_background_tasks

    set_background_tasks(app.state.background_tasks)

    # Background loops
    from server.core.pool_manager import health_check_loop, replenishment_loop

    async def _oauth_cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                await sweep_expired_oauth_rows(session_factory)
            except Exception:
                logger.exception("OAuth cleanup sweep failed")

    cleanup_task = asyncio.create_task(_oauth_cleanup_loop())
    replenish_task = asyncio.create_task(replenishment_loop(session_factory, app.state.background_tasks))
    health_task = asyncio.create_task(health_check_loop(session_factory))

    app.state.background_tasks.add(cleanup_task)
    app.state.background_tasks.add(replenish_task)
    app.state.background_tasks.add(health_task)

    async with mcp_lifespan():
        yield

    cleanup_task.cancel()
    replenish_task.cancel()
    health_task.cancel()

    logger.info("alibabacloud polardb tool agentic server shutting down", extra={"action": "shutdown"})


def _patch_issuer_url_validation() -> None:
    """Allow HTTP issuer URLs in dev mode (MCP SDK enforces HTTPS)."""
    import mcp.server.auth.routes as auth_routes
    auth_routes.validate_issuer_url = lambda _url: None  # type: ignore[assignment]


def create_app() -> FastAPI:
    config = get_config()

    if config.server.dev_mode:
        _patch_issuer_url_validation()

    app = FastAPI(
        title="alibabacloud polardb tool agentic server",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS middleware
    cors_origins = config.server.cors_origins
    if not cors_origins and config.server.dev_mode:
        cors_origins = ["http://localhost:18761"]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Web SSO Guard middleware (OIDC pre-authentication for Web UI)
    if config.auth.mode == "oidc" and config.auth.web_sso_guard.enabled:
        from server.auth.web_sso_guard import WebSSOGuardMiddleware, handle_web_sso_guard_callback

        app.add_middleware(
            WebSSOGuardMiddleware,
            config={"excluded_paths": config.auth.web_sso_guard.excluded_paths},
        )

        @app.get("/auth/web-sso-guard/callback")
        async def web_sso_guard_callback(request: Request):
            return await handle_web_sso_guard_callback(request)

        logger.info("Web SSO Guard enabled — Web UI protected by OIDC pre-authentication")
    elif config.auth.web_sso_guard.enabled and config.auth.mode != "oidc":
        import warnings
        warnings.warn(
            "auth.web_sso_guard.enabled=true but auth.mode is not 'oidc'. "
            "SSO Guard is disabled — it requires OIDC mode to function.",
            stacklevel=2,
        )

    # Request ID middleware
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or generate_request_id()
        token = trace_id_var.set(rid)
        try:
            response: Response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            trace_id_var.reset(token)

    # Health endpoints
    @app.get("/livez")
    async def livez():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        # Will be enhanced in M2 when DB is available
        return {"status": "ok"}

    @app.get("/healthz/dependencies")
    async def healthz_dependencies():
        # Will be enhanced when OpenAPI and instances are available
        return {"status": "ok", "dependencies": {}}

    # Auth router
    app.include_router(auth_router)

    # Admin API router
    app.include_router(api_router)

    # Legacy REST endpoints (must be before mount to avoid shadowing)
    app.include_router(mcp_router)

    # Serve frontend static files if build exists.
    # Try web/dist (dev/bare-metal) then static/ (Docker).
    _project_root = Path(__file__).resolve().parent.parent
    _static_dir: Path | None = None
    for candidate in [_project_root / "web" / "dist", _project_root / "static"]:
        if (candidate / "index.html").is_file():
            _static_dir = candidate
            break

    if _static_dir is not None:
        app.mount("/assets", StaticFiles(directory=str(_static_dir / "assets")), name="static-assets")

        from fastapi.responses import FileResponse

        @app.get("/favicon.ico")
        async def favicon():
            fav = _static_dir / "favicon.ico"  # type: ignore[operator]
            if fav.is_file():
                return FileResponse(str(fav))
            return Response(status_code=404)

    # MCP Streamable HTTP transport with SDK-managed OAuth endpoints.
    # Mounted at "/" LAST so FastAPI exact-match routes take priority.
    app.mount("/", create_mcp_app())

    # SPA fallback: middleware intercepts 404s from ALL sub-apps (including
    # the MCP mount) and serves index.html for browser navigation requests.
    if _static_dir is not None:
        _index_bytes = (_static_dir / "index.html").read_bytes()

        @app.middleware("http")
        async def _spa_fallback(request: Request, call_next):
            response = await call_next(request)
            if (
                response.status_code == 404
                and "text/html" in request.headers.get("accept", "")
                and not request.url.path.startswith(("/api/", "/auth/", "/mcp", "/.well-known/"))
            ):
                return Response(content=_index_bytes, media_type="text/html")
            return response

    return app
