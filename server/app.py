from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from server.api.router import router as api_router
from server.auth.router import router as auth_router
from server.config import get_config
from server.config import TenantProvisioningConfig
from server.db.engine import get_engine, get_session_factory
from server.db.schema import check_database_schema
from server.logging import normalize_request_id, setup_logging, trace_id_var
from server.mcp.server import router as mcp_router
from server.auth.cleanup import sweep_expired_oauth_rows
from server.mcp.transport import LazyMCPApplication, mcp_lifespan
from server.version import __version__

logger = logging.getLogger(__name__)


@dataclass
class ProvisioningRuntime:
    dispatcher: Any
    health: Any
    pool_manager: Any


async def _build_provisioning_runtime(
    session_factory, config: TenantProvisioningConfig
) -> ProvisioningRuntime:
    from server.core.adapter_registry import AdapterRegistry
    from server.core.db_instance_dispatcher import DBInstanceDispatcher
    from server.core.multitenant_health import ProvisioningHealthWorker
    from server.core.provisioning_adapter import (
        PolarDBMySQLMultitenantAdapter,
    )
    from server.core.super_connection_pool import SuperConnectionPoolManager
    from server.models import (
        InstanceEngine,
        InstanceTopology,
    )

    pool_manager = SuperConnectionPoolManager()
    registry = AdapterRegistry()
    registry.register(
        InstanceEngine.POLARDB_MYSQL,
        InstanceTopology.MULTITENANT,
        PolarDBMySQLMultitenantAdapter(session_factory, pool_manager),
    )
    dispatcher = DBInstanceDispatcher(
        session_factory,
        config,
        registry,
        worker_id=f"mcp-{uuid.uuid4().hex}",
    )
    health = ProvisioningHealthWorker(
        session_factory,
        config,
        registry,
        pool_manager,
    )
    return ProvisioningRuntime(dispatcher, health, pool_manager)


@asynccontextmanager
async def provisioning_runtime_lifespan(
    app: FastAPI, session_factory, config: TenantProvisioningConfig
):
    runtime = await _build_provisioning_runtime(session_factory, config)
    app.state.provisioning_runtime = runtime
    await runtime.health.run_once()
    stop_event = asyncio.Event()
    tasks = [
        asyncio.create_task(runtime.dispatcher.run_forever(stop_event)),
        asyncio.create_task(runtime.health.run_forever(stop_event)),
    ]
    app.state.background_tasks.update(tasks)
    try:
        yield
    finally:
        stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.pool_manager.close_all()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_config = get_config()
    setup_logging(setup_config.server.log_level, setup_config.logging)
    logger.info("alibabacloud polardb tool agentic server starting", extra={"action": "startup"})

    await check_database_schema()
    get_engine()
    session_factory = get_session_factory()
    app.state.session_factory = session_factory
    app.state.background_tasks = set()

    from server.bootstrap import load_bootstrap_settings
    from server.configuration.bootstrap import initialize_configuration
    from server.configuration.repository import ConfigRepository
    from server.configuration.external_validation import (
        AlibabaCloudExternalValidator,
    )
    from server.configuration.runtime import (
        ModuleLifecycleManager,
        RuntimeConfigStore,
        RuntimeSectionProxy,
    )
    from server.configuration.service import ConfigService
    from server.config import install_runtime_config_store
    from server.core.config_crypto import ConfigCrypto
    from server.middleware.runtime_policy import RuntimeAccessPolicy

    repository = ConfigRepository(session_factory)
    crypto = ConfigCrypto(load_bootstrap_settings().encryption_key)
    initialization = await initialize_configuration(repository, crypto)
    if initialization.bootstrap_token is not None:
        print(
            "\n"
            "Guided configuration bootstrap token (shown once):\n"
            f"Bootstrap token: {initialization.bootstrap_token}\n"
            "Enter this token in the setup UI. Treat it as a password.\n",
            flush=True,
        )
    config_service = ConfigService(
        repository,
        crypto,
        external_validator=AlibabaCloudExternalValidator(),
    )
    app.state.config_service = config_service

    from server.auth.jwt_manager import initialize_jwt_keys_from_db

    async def apply_access_policy(_old, new) -> None:
        app.state.runtime_access_policy = RuntimeAccessPolicy(
            mode=(await config_service._system_state()).value,
            cors_allowed_origins=tuple(
                new.server.cors_origins
            ),
            sso_active=(
                new.auth.mode == "oidc"
                and new.auth.web_sso_guard.enabled
            ),
        )

    async def apply_token_security(_old, _new) -> None:
        async with session_factory() as session:
            await initialize_jwt_keys_from_db(session, crypto)

    async def apply_observability(_old, new) -> None:
        setup_logging(new.server.log_level, new.logging)

    runtime_store = RuntimeConfigStore(
        repository,
        crypto,
        lifecycle_manager=ModuleLifecycleManager(
            {
                "core_admin": apply_access_policy,
                "runtime_policy": apply_access_policy,
                "user_sso": apply_access_policy,
                "token_security": apply_token_security,
                "observability": apply_observability,
            }
        ),
    )
    await runtime_store.poll_once()
    install_runtime_config_store(runtime_store)
    app.state.runtime_config_store = runtime_store
    config = runtime_store.current()
    stop_config_poll = asyncio.Event()
    config_poll_task = asyncio.create_task(
        runtime_store.poll_forever(stop_config_poll)
    )
    app.state.background_tasks.add(config_poll_task)

    system_state = await config_service._system_state()
    if system_state.value == "SETUP":
        logger.info("server is ready for guided initial configuration")

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
    pool = get_config().polardb.resource_pool
    if pool.target_size > 0 and pool.security_ip_list == "127.0.0.1":
        logger.warning(
            "resource pool target_size=%d but security_ip_list is still "
            "127.0.0.1 — agents cannot connect",
            pool.target_size,
        )

    # Share background_tasks with MCP transport module
    from server.mcp.transport import set_background_tasks

    set_background_tasks(app.state.background_tasks)

    # Background loops
    from server.core.audit_retention import audit_retention_loop
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
    audit_retention_task = (
        asyncio.create_task(
            audit_retention_loop(
                session_factory,
                RuntimeSectionProxy(
                    lambda: get_config().sql_security.audit
                ),
            )
        )
        if config.sql_security.audit.cleanup_interval_seconds > 0
        else None
    )

    lifecycle_tasks = [
        config_poll_task,
        cleanup_task,
        replenish_task,
        health_task,
    ]
    if audit_retention_task is not None:
        lifecycle_tasks.append(audit_retention_task)
    app.state.background_tasks.update(lifecycle_tasks)

    async with provisioning_runtime_lifespan(
        app,
        session_factory,
        RuntimeSectionProxy(
            lambda: get_config().polardb.tenant_provisioning
        ),
    ):
        async with mcp_lifespan():
            yield

    for task in lifecycle_tasks:
        task.cancel()
    stop_config_poll.set()
    await asyncio.gather(*lifecycle_tasks, return_exceptions=True)

    logger.info("alibabacloud polardb tool agentic server shutting down", extra={"action": "shutdown"})


def create_app() -> FastAPI:
    app = FastAPI(
        title="alibabacloud polardb tool agentic server",
        version=__version__,
        lifespan=lifespan,
    )
    from server.middleware.runtime_policy import (
        RuntimeAccessPolicy,
        RuntimePolicyMiddleware,
    )

    app.state.runtime_access_policy = RuntimeAccessPolicy()
    app.add_middleware(
        RuntimePolicyMiddleware,
        snapshot_provider=lambda: app.state.runtime_access_policy,
    )

    # SSO routes remain installed and consult the current runtime snapshot.
    from server.auth.web_sso_guard import (
        handle_web_sso_guard_callback,
    )

    @app.get("/auth/web-sso-guard/callback")
    async def web_sso_guard_callback(request: Request):
        if not request.app.state.runtime_access_policy.sso_active:
            return Response(status_code=404)
        return await handle_web_sso_guard_callback(request)

    # Request ID middleware
    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        rid = normalize_request_id(request.headers.get("X-Request-ID"))
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
    async def readyz(request: Request):
        store = getattr(
            request.app.state, "runtime_config_store", None
        )
        if store is None:
            return {
                "status": "ok",
                "mode": request.app.state.runtime_access_policy.mode,
            }
        try:
            desired_version = await store.repository.global_version()
        except Exception:
            logger.exception(
                "configuration readiness version check failed"
            )
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not_ready",
                    "mode": request.app.state.runtime_access_policy.mode,
                    "desired_config_version": None,
                    "loaded_config_version": store.config_version,
                    "config_status": "UNAVAILABLE",
                    "last_reload_error": "CONFIG_VERSION_UNAVAILABLE",
                    "module_errors": store.local_errors,
                },
            )
        stale = store.config_version != desired_version
        reload_failed = store.last_error_code is not None
        status_code = 503 if stale or reload_failed else 200
        config_status = (
            "STALE"
            if stale
            else "ERROR"
            if reload_failed
            else "DEGRADED"
            if store.local_errors
            else "CURRENT"
        )
        return JSONResponse(
            status_code=status_code,
            content={
                "status": (
                    "not_ready" if status_code == 503 else "ok"
                ),
                "mode": request.app.state.runtime_access_policy.mode,
                "desired_config_version": desired_version,
                "loaded_config_version": store.config_version,
                "config_status": config_status,
                "last_reload_error": store.last_error_code,
                "module_errors": store.local_errors,
            },
        )

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
    app.mount("/", LazyMCPApplication())

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
