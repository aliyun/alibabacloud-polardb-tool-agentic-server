from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from markdown import markdown

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


_IDENTITY_SOURCE_HELP_PROVIDERS = ("feishu", "sharepoint")


def _identity_source_help_labels(language: str) -> dict[str, str]:
    if language == "zh-cn":
        return {
            "title": "企业身份源接入",
            "description": "选择要配置的企业身份源。",
            "feishu": "飞书接入",
            "sharepoint": "SharePoint 接入",
            "api": "企业身份源管理员 API",
            "back": "返回接入指南",
        }
    return {
        "title": "Enterprise identity source integration",
        "description": "Choose the enterprise identity source to configure.",
        "feishu": "Feishu integration",
        "sharepoint": "SharePoint integration",
        "api": "Enterprise identity source administrator API",
        "back": "Back to integration guides",
    }


def _extract_identity_source_help_section(
    document_markdown: str,
    heading: str,
) -> str:
    marker = f"## {heading}\n"
    start = document_markdown.find(marker)
    if start < 0:
        raise ValueError("identity source help section is missing")
    section = document_markdown[start:]
    next_section = section.find("\n## ", len(marker))
    return section if next_section < 0 else section[:next_section]


def _render_enterprise_identity_source_help(
    content: str,
    *,
    language: str,
    title: str,
    provider: str | None = None,
) -> str:
    alternate_locale = "en" if language == "zh-cn" else "zh-cn"
    alternate_label = "English" if language == "zh-cn" else "简体中文"
    zoom_label = "阅读字号" if language == "zh-cn" else "Reading size"
    language_query = {"locale": alternate_locale}
    if provider is not None:
        language_query["provider"] = provider
    language_link = escape(
        "/help/enterprise-identity-sources?"
        + urlencode(language_query),
        quote=True,
    )
    safe_language = escape(language, quote=True)
    safe_title = escape(title)
    safe_zoom_label = escape(zoom_label, quote=True)
    safe_alternate_label = escape(alternate_label)
    return f"""<!doctype html>
<html lang="{safe_language}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{safe_title} · PAS</title>
  <style>
    :root {{ color: #1f2937; background: #f6f8fb; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; }}
    header {{ background: #fff; border-bottom: 1px solid #e5e7eb; }}
    header > div, main {{ box-sizing: border-box; max-width: 980px; margin: 0 auto; padding: 20px 28px; }}
    header > div {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; }}
    header strong {{ color: #1677ff; font-size: 18px; }}
    a {{ color: #1677ff; }}
    .help-actions {{ display: inline-flex; align-items: center; gap: 12px; }}
    .zoom {{ display: inline-flex; overflow: hidden; border: 1px solid #cbd5e1; border-radius: 6px; }}
    .zoom input {{ position: absolute; opacity: 0; }}
    .zoom label {{ min-width: 30px; padding: 5px 8px; cursor: pointer; text-align: center; }}
    .zoom input:checked + label {{ color: #fff; background: #1677ff; }}
    main {{ background: #fff; margin-top: 28px; margin-bottom: 28px; border: 1px solid #e5e7eb; border-radius: 12px; box-shadow: 0 4px 18px rgb(15 23 42 / 6%); }}
    .guide-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; margin-top: 28px; }}
    .guide-card {{ display: block; padding: 24px; border: 1px solid #bfdbfe; border-radius: 10px; background: #eff6ff; color: #1d4ed8; font-size: 20px; font-weight: 600; text-decoration: none; }}
    .guide-card:hover {{ background: #dbeafe; }}
    .guide-back {{ margin-bottom: 24px; }}
    h1 {{ margin-top: 0; font-size: 30px; }}
    h2 {{ margin-top: 36px; padding-top: 8px; border-top: 1px solid #e5e7eb; font-size: 23px; }}
    p, li {{ line-height: 1.75; }}
    code {{ padding: 2px 5px; border-radius: 4px; background: #f1f5f9; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    pre {{ overflow-x: auto; padding: 16px; border-radius: 8px; background: #0f172a; color: #e2e8f0; }}
    pre code {{ padding: 0; background: transparent; color: inherit; }}
    table {{ width: 100%; border-collapse: collapse; margin: 16px 0; }}
    th, td {{ padding: 10px 12px; border: 1px solid #dbe3ef; text-align: left; vertical-align: top; }}
    th {{ background: #f1f5f9; }}
    body:has(#help-zoom-small:checked) main {{ font-size: 0.9rem; }}
    body:has(#help-zoom-large:checked) main {{ font-size: 1.12rem; }}
    @media (max-width: 640px) {{ header > div, main {{ padding: 16px; }} main {{ margin: 16px 12px; }} }}
  </style>
</head>
<body>
  <header><div><strong>PAS</strong><div class="help-actions"><div class="zoom" aria-label="{safe_zoom_label}">
    <input id="help-zoom-small" name="help-zoom" type="radio"><label for="help-zoom-small">A−</label>
    <input id="help-zoom-normal" name="help-zoom" type="radio" checked><label for="help-zoom-normal">A</label>
    <input id="help-zoom-large" name="help-zoom" type="radio"><label for="help-zoom-large">A+</label>
  </div><a href="{language_link}">{safe_alternate_label}</a></div></div></header>
  <main>{content}</main>
</body>
</html>"""


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
    from server.enterprise_identity.sync import identity_source_sync_loop
    from server.polarrag.catalog import catalog_sync_loop
    from server.polarrag.upload_cleanup import upload_cleanup_loop

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
    polarrag_upload_cleanup_task = asyncio.create_task(
        upload_cleanup_loop(session_factory)
    )
    identity_source_sync_task = asyncio.create_task(
        identity_source_sync_loop(session_factory)
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
        polarrag_upload_cleanup_task,
        identity_source_sync_task,
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

    @app.get("/help/enterprise-identity-sources", include_in_schema=False)
    async def enterprise_identity_source_help(
        locale: str = "en",
        provider: str | None = None,
    ):
        language = "zh-cn" if locale.casefold().startswith("zh") else "en"
        document = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / language
            / "administration"
            / "enterprise-identity-sources.md"
        )
        if not document.is_file():
            return Response(status_code=404)
        labels = _identity_source_help_labels(language)
        document_markdown = document.read_text(encoding="utf-8")
        document_markdown = document_markdown.replace(
            "[English](../../en/administration/enterprise-identity-sources.md)\n\n",
            "",
            1,
        ).replace(
            "[简体中文](../../zh-cn/administration/enterprise-identity-sources.md)\n\n",
            "",
            1,
        )
        document_markdown = document_markdown.replace(
            "(../reference/enterprise-identity-sources-api.md)",
            f"(/help/enterprise-identity-sources-api?locale={language})",
        )
        normalized_provider = provider.casefold() if provider else None
        if normalized_provider is None:
            content = (
                f"<h1>{labels['title']}</h1>"
                f"<p>{labels['description']}</p>"
                '<div class="guide-grid">'
                f'<a class="guide-card" href="?locale={language}&provider=feishu">'
                f"{labels['feishu']}</a>"
                f'<a class="guide-card" href="?locale={language}&provider=sharepoint">'
                f"{labels['sharepoint']}</a>"
                "</div>"
            )
            title = labels["title"]
        elif normalized_provider in _IDENTITY_SOURCE_HELP_PROVIDERS:
            headings = {
                "zh-cn": {
                    "feishu": "配置飞书身份源",
                    "sharepoint": "配置 SharePoint 身份源",
                },
                "en": {
                    "feishu": "Configure a Feishu identity source",
                    "sharepoint": "Configure a SharePoint identity source",
                },
            }
            section = _extract_identity_source_help_section(
                document_markdown,
                headings[language][normalized_provider],
            )
            content = (
                f'<p class="guide-back"><a href="?locale={language}">'
                f"← {labels['back']}</a></p>"
                + markdown(
                    f"# {labels[normalized_provider]}\n\n{section}",
                    extensions=("fenced_code", "sane_lists", "tables"),
                )
            )
            title = labels[normalized_provider]
        else:
            return Response(status_code=404)
        return HTMLResponse(
            _render_enterprise_identity_source_help(
                content,
                language=language,
                title=title,
                provider=normalized_provider,
            ),
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "base-uri 'none'; form-action 'none'"
                )
            },
        )

    @app.get("/help/enterprise-identity-sources-api", include_in_schema=False)
    async def enterprise_identity_source_api_help(locale: str = "en"):
        language = "zh-cn" if locale.casefold().startswith("zh") else "en"
        document = (
            Path(__file__).resolve().parents[1]
            / "docs"
            / language
            / "reference"
            / "enterprise-identity-sources-api.md"
        )
        if not document.is_file():
            return Response(status_code=404)
        document_markdown = document.read_text(encoding="utf-8")
        document_markdown = document_markdown.replace(
            "[English](../../en/reference/enterprise-identity-sources-api.md)\n\n",
            "",
            1,
        ).replace(
            "[简体中文](../../zh-cn/reference/enterprise-identity-sources-api.md)\n\n",
            "",
            1,
        ).replace(
            "(../administration/enterprise-identity-sources.md)",
            f"(/help/enterprise-identity-sources?locale={language})",
        )
        labels = _identity_source_help_labels(language)
        content = (
            f'<p class="guide-back"><a href="/help/enterprise-identity-sources?locale={language}">'
            f"← {labels['back']}</a></p>"
            + markdown(
                document_markdown,
                extensions=("fenced_code", "sane_lists", "tables"),
            )
        )
        return HTMLResponse(
            _render_enterprise_identity_source_help(
                content,
                language=language,
                title=labels["api"],
            ),
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "base-uri 'none'; form-action 'none'"
                )
            },
        )

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
