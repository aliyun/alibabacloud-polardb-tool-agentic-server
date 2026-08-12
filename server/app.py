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
from server.db.schema import check_database_compatibility
from server.logging import normalize_request_id, setup_logging, trace_id_var
from server.mcp.server import router as mcp_router
from server.mcp.agent_openapi import router as agent_openapi_router
from server.mcp.db_instance_rest import router as agent_db_instance_router
from server.auth.cleanup import sweep_expired_oauth_rows
from server.mcp.transport import LazyMCPApplication, mcp_lifespan
from server.version import __version__

logger = logging.getLogger(__name__)


@dataclass
class ProvisioningRuntime:
    dispatcher: Any
    health: Any
    dedicated: Any
    pool_manager: Any
    dedicated_client_available: bool


async def _build_dedicated_worker_runtime(
    session_factory,
    config: TenantProvisioningConfig,
) -> tuple[Any, bool]:
    from server.aliyun.polardb_client import (
        AliyunCredentialsUnavailable,
        get_polardb_client_async,
    )
    from server.core.dedicated_mysql import DedicatedMySQL
    from server.core.dedicated_pool_provisioner import DedicatedPoolProvisioner
    from server.core.dedicated_pool_worker import DedicatedPoolWorker

    client_available = True
    try:
        async with session_factory() as session:
            polardb_client = await get_polardb_client_async(session)
    except AliyunCredentialsUnavailable:
        client_available = False
        polardb_client = None
    dedicated_mysql = DedicatedMySQL()
    dedicated_provisioner = DedicatedPoolProvisioner(
        session_factory,
        polardb_client,
        dedicated_mysql,
    )
    return (
        DedicatedPoolWorker(
            session_factory,
            config,
            dedicated_provisioner,
            dedicated_mysql,
            worker_id=f"dedicated-{uuid.uuid4().hex}",
        ),
        client_available,
    )


class DedicatedPoolWorkerSupervisor:
    """Reconcile the in-process Dedicated worker with active runtime config."""

    def __init__(
        self,
        app: FastAPI,
        session_factory,
        config: TenantProvisioningConfig,
        runtime: ProvisioningRuntime,
    ) -> None:
        self._app = app
        self._session_factory = session_factory
        self._config = config
        self._runtime = runtime
        self._wake = asyncio.Event()
        self._rebuild_required = False
        self._worker_task: asyncio.Task[Any] | None = None
        self._worker_stop: asyncio.Event | None = None

    def request_reconcile(self, *, rebuild: bool = False) -> None:
        self._rebuild_required = self._rebuild_required or rebuild
        self._wake.set()

    def request_run(self) -> None:
        """Wake the active worker, reconciling first when it is unavailable."""
        request_run = getattr(self._runtime.dedicated, "request_run", None)
        if callable(request_run):
            request_run()
        self._wake.set()

    async def _stop_worker(self, *, cancel: bool = False) -> None:
        task = self._worker_task
        stop_event = self._worker_stop
        self._worker_task = None
        self._worker_stop = None
        self._app.state.dedicated_pool_worker_enabled = False
        if task is None:
            return
        if stop_event is not None:
            stop_event.set()
        if cancel:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _rebuild_worker(self) -> None:
        await self._stop_worker()
        worker, client_available = await _build_dedicated_worker_runtime(
            self._session_factory,
            self._config,
        )
        self._runtime.dedicated = worker
        self._runtime.dedicated_client_available = client_available

    async def _reconcile(self) -> None:
        enabled = bool(self._config.dedicated_pool_enabled)
        if not enabled:
            self._rebuild_required = False
            await self._stop_worker()
            return
        if self._rebuild_required:
            await self._rebuild_worker()
            self._rebuild_required = False
        client_available = bool(
            getattr(self._runtime, "dedicated_client_available", True)
        )
        if not client_available:
            await self._stop_worker()
            return
        if self._worker_task is not None and self._worker_task.done():
            await self._stop_worker()
        if self._worker_task is not None:
            return
        self._worker_stop = asyncio.Event()
        self._worker_task = asyncio.create_task(
            self._runtime.dedicated.run_forever(self._worker_stop)
        )
        self._app.state.background_tasks.add(self._worker_task)
        self._worker_task.add_done_callback(
            self._app.state.background_tasks.discard
        )
        self._app.state.dedicated_pool_worker_enabled = True

    async def _reconcile_safely(self) -> None:
        try:
            await self._reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._app.state.dedicated_pool_worker_enabled = False
            logger.exception(
                "Dedicated pool worker runtime reconciliation failed",
                extra={"feature": "dedicated_pool"},
            )

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        try:
            await self._reconcile_safely()
            while not stop_event.is_set():
                stop_wait = asyncio.create_task(stop_event.wait())
                wake_wait = asyncio.create_task(self._wake.wait())
                retry_wait = asyncio.create_task(
                    asyncio.sleep(
                        max(
                            float(
                                self._config.worker_poll_interval_seconds
                            ),
                            1.0,
                        )
                    )
                )
                done, pending = await asyncio.wait(
                    {stop_wait, wake_wait, retry_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stop_wait in done and stop_event.is_set():
                    break
                if wake_wait in done:
                    self._wake.clear()
                await self._reconcile_safely()
        finally:
            await self._stop_worker(cancel=True)


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
    dedicated, dedicated_client_available = (
        await _build_dedicated_worker_runtime(
            session_factory,
            config,
        )
    )
    return ProvisioningRuntime(
        dispatcher,
        health,
        dedicated,
        pool_manager,
        dedicated_client_available,
    )


@asynccontextmanager
async def provisioning_runtime_lifespan(
    app: FastAPI, session_factory, config: TenantProvisioningConfig
):
    runtime = await _build_provisioning_runtime(session_factory, config)
    app.state.provisioning_runtime = runtime
    await runtime.health.run_once()
    stop_event = asyncio.Event()
    supervisor = DedicatedPoolWorkerSupervisor(
        app,
        session_factory,
        config,
        runtime,
    )
    app.state.dedicated_pool_worker_supervisor = supervisor
    app.state.dedicated_pool_worker_enabled = False
    tasks = [
        asyncio.create_task(runtime.dispatcher.run_forever(stop_event)),
        asyncio.create_task(runtime.health.run_forever(stop_event)),
        asyncio.create_task(supervisor.run_forever(stop_event)),
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

    await check_database_compatibility()
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

    def request_dedicated_worker_reconcile() -> None:
        supervisor = getattr(
            app.state,
            "dedicated_pool_worker_supervisor",
            None,
        )
        if supervisor is not None:
            # The lifecycle adapter runs before RuntimeConfigStore publishes
            # its candidate. Waking the supervisor schedules reconciliation
            # for the next event-loop turn, after the new snapshot is visible.
            supervisor.request_reconcile(rebuild=True)

    async def apply_runtime_policy(old, new) -> None:
        await apply_access_policy(old, new)
        request_dedicated_worker_reconcile()

    async def apply_aliyun_access(_old, _new) -> None:
        request_dedicated_worker_reconcile()

    runtime_store = RuntimeConfigStore(
        repository,
        crypto,
        lifecycle_manager=ModuleLifecycleManager(
            {
                "core_admin": apply_access_policy,
                "runtime_policy": apply_runtime_policy,
                "user_sso": apply_access_policy,
                "token_security": apply_token_security,
                "aliyun_access": apply_aliyun_access,
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

    # Share background_tasks with MCP transport module
    from server.mcp.transport import set_background_tasks

    set_background_tasks(app.state.background_tasks)

    # Background loops
    from server.core.audit_retention import audit_retention_loop
    from server.polarrag.catalog import catalog_sync_loop

    async def _oauth_cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            try:
                await sweep_expired_oauth_rows(session_factory)
            except Exception:
                logger.exception("OAuth cleanup sweep failed")

    cleanup_task = asyncio.create_task(_oauth_cleanup_loop())
    polarrag_catalog_task = asyncio.create_task(
        catalog_sync_loop(session_factory)
    )
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
        polarrag_catalog_task,
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


def _discover_static_dir() -> Path | None:
    """Locate the built web console across supported layouts.

    The package may be imported from site-packages (packaged image), where
    __file__-relative paths contain no build output, so the working
    directory and an explicit override are also consulted.
    """
    import os

    override = os.environ.get("PAS_STATIC_DIR")
    module_root = Path(__file__).resolve().parent.parent
    candidates = [
        *( [Path(override)] if override else [] ),
        module_root / "web" / "dist",
        module_root / "static",
        Path.cwd() / "web" / "dist",
        Path.cwd() / "static",
    ]
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


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
    # Agent lifecycle routes deliberately use a different principal under the
    # shared REST prefix and are absent from the application OpenAPI schema.
    app.include_router(agent_db_instance_router)
    app.include_router(agent_openapi_router)

    # Serve frontend static files if build exists.
    _static_dir = _discover_static_dir()

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
        @app.middleware("http")
        async def _spa_fallback(request: Request, call_next):
            response = await call_next(request)
            if (
                response.status_code == 404
                and "text/html" in request.headers.get("accept", "")
                and not request.url.path.startswith(("/api/", "/auth/", "/mcp", "/.well-known/"))
            ):
                return FileResponse(str(_static_dir / "index.html"))
            return response

    return app
