from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from server.config import AppConfig
from server.configuration.registry import (
    MODULE_REGISTRY,
    topological_modules,
)
from server.configuration.repository import ConfigRepository
from server.configuration.types import ModuleDocument, ModuleState
from server.core.config_crypto import ConfigCrypto, SecretEnvelope

logger = logging.getLogger(__name__)

LifecycleCallable = Callable[
    [AppConfig, AppConfig], Awaitable[None]
]


class RuntimeSectionProxy:
    """Resolve startup-injected config attributes from the latest snapshot."""

    def __init__(self, provider: Callable[[], Any]) -> None:
        object.__setattr__(self, "_provider", provider)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_provider")(), name)


@dataclass(frozen=True, slots=True)
class ReloadResult:
    reloaded: bool
    config_version: int
    changed_modules: tuple[str, ...] = ()
    optional_failures: tuple[str, ...] = ()
    error_code: str | None = None


class ModuleLifecycleManager:
    def __init__(
        self,
        adapters: dict[str, LifecycleCallable] | None = None,
    ) -> None:
        self.adapters = adapters or {}

    async def apply(
        self,
        old: AppConfig,
        new: AppConfig,
        changed_modules: tuple[str, ...],
    ) -> tuple[str, ...]:
        optional_failures: list[str] = []
        for module in topological_modules(MODULE_REGISTRY):
            if module not in changed_modules:
                continue
            adapter = self.adapters.get(module)
            if adapter is None:
                continue
            try:
                await adapter(old, new)
            except Exception:
                if not MODULE_REGISTRY[module].optional:
                    raise
                logger.exception(
                    "optional runtime configuration adapter failed",
                    extra={"module": module},
                )
                optional_failures.append(module)
        return tuple(optional_failures)


def _decrypt_effective(
    module: str,
    document: ModuleDocument,
    crypto: ConfigCrypto,
) -> dict[str, Any]:
    if (
        document.effective is None
        or document.effective.state != ModuleState.ACTIVE
    ):
        return {}
    config = dict(document.effective.config)
    for field in MODULE_REGISTRY[module].secret_fields:
        value = config.get(field)
        if isinstance(value, dict) and "$secret" in value:
            config[field] = crypto.decrypt_field(
                SecretEnvelope.model_validate(value["$secret"]),
                module=module,
                field_path=field,
                schema_version=document.schema_version,
            )
    return config


def project_app_config(
    documents: dict[str, ModuleDocument],
    crypto: ConfigCrypto,
) -> AppConfig:
    """Project validated effective module documents into the legacy facade."""
    config = AppConfig()
    effective = {
        module: _decrypt_effective(module, document, crypto)
        for module, document in documents.items()
    }

    runtime = effective.get("runtime_policy", {})
    config.server.public_base_url = str(
        runtime.get("external_base_url") or ""
    )
    config.server.cors_origins = [
        str(origin)
        for origin in runtime.get("cors_allowed_origins", [])
    ]
    pool = config.polardb.connection_pool
    pool.max_connections_per_pool = int(
        runtime.get(
            "max_connections_per_pool",
            pool.max_connections_per_pool,
        )
    )
    pool.idle_timeout_seconds = int(
        runtime.get(
            "idle_timeout_seconds", pool.idle_timeout_seconds
        )
    )
    pool.max_total_pools = int(
        runtime.get("max_total_pools", pool.max_total_pools)
    )
    provisioning = config.polardb.tenant_provisioning
    for field in (
        "worker_poll_interval_seconds",
        "worker_claim_ttl_seconds",
        "worker_claim_renew_seconds",
    ):
        if field in runtime:
            setattr(provisioning, field, int(runtime[field]))

    observability = effective.get("observability", {})
    config.server.log_level = str(
        observability.get("log_level", config.server.log_level)
    )
    for field in (
        "log_dir",
        "log_file",
        "max_bytes",
        "backup_count",
        "timezone",
    ):
        if field in observability:
            setattr(config.logging, field, observability[field])

    sql = effective.get("sql_security", {})
    for field in (
        "max_rows",
        "timeout_ms",
        "max_timeout_ms",
        "blocked_keywords",
        "confirmable_statement_types",
    ):
        if field in sql:
            setattr(config.sql_security, field, sql[field])
    config.sql_security.rate_limit.enabled = bool(
        sql.get(
            "rate_limit_enabled",
            config.sql_security.rate_limit.enabled,
        )
    )
    for field in ("requests_per_minute", "burst"):
        if field in sql:
            setattr(config.sql_security.rate_limit, field, sql[field])
    config.sql_security.audit.enabled = bool(
        sql.get("audit_enabled", config.sql_security.audit.enabled)
    )
    if "audit_retention_days" in sql:
        config.sql_security.audit.retention_days = int(
            sql["audit_retention_days"]
        )

    token = effective.get("token_security", {})
    config.auth.jwt.algorithm = str(
        token.get("algorithm", config.auth.jwt.algorithm)
    )
    config.auth.jwt.access_token_expire_minutes = int(
        token.get(
            "access_token_expire_minutes",
            config.auth.jwt.access_token_expire_minutes,
        )
    )
    config.auth.jwt.refresh_token_expire_days = int(
        token.get(
            "refresh_token_expire_days",
            config.auth.jwt.refresh_token_expire_days,
        )
    )

    sso_document = documents.get("user_sso")
    if (
        sso_document is not None
        and sso_document.effective is not None
        and sso_document.effective.state == ModuleState.ACTIVE
    ):
        sso = effective["user_sso"]
        config.auth.mode = "oidc"
        config.auth.web_sso_guard.enabled = True
        for field in (
            "discovery_url",
            "client_id",
            "client_secret",
            "scopes",
            "user_id_claim",
            "display_name_claim",
            "email_claim",
            "authorization_endpoint",
            "token_endpoint",
            "userinfo_endpoint",
            "jwks_uri",
            "userinfo_token_method",
            "provider_name",
            "idp_pkce",
            "id_token_algorithms",
        ):
            if field in sso:
                setattr(config.auth.oidc, field, sso[field])
        config.auth.default_department = str(
            sso.get("default_department") or ""
        )
        if config.server.public_base_url:
            config.auth.oidc.redirect_uri = (
                config.server.public_base_url
                + "/auth/oidc/callback"
            )

    aliyun = effective.get("aliyun_access", {})
    for field in (
        "credential_mode",
        "access_key_id",
        "access_key_secret",
        "role_arn",
        "role_session_name",
        "sts_duration_seconds",
        "region_id",
        "openapi_network",
    ):
        if field in aliyun:
            setattr(config.aliyun, field, aliyun[field])

    purchase = effective.get("agentic_db_purchase", {})
    for field, value in purchase.items():
        if hasattr(config.polardb.agentic_db, field):
            setattr(config.polardb.agentic_db, field, value)

    resource_pool = effective.get("resource_pool", {})
    for field, value in resource_pool.items():
        if hasattr(config.polardb.resource_pool, field):
            setattr(config.polardb.resource_pool, field, value)
    return config


def _module_signature(
    document: ModuleDocument,
) -> tuple[int | None, str | None]:
    if document.effective is None:
        return None, None
    return (
        document.effective.revision,
        document.effective.state.value,
    )


class RuntimeConfigStore:
    def __init__(
        self,
        repository: ConfigRepository,
        crypto: ConfigCrypto,
        *,
        lifecycle_manager: ModuleLifecycleManager | None = None,
    ) -> None:
        self.repository = repository
        self.crypto = crypto
        self.lifecycle_manager = (
            lifecycle_manager or ModuleLifecycleManager()
        )
        self._config = AppConfig()
        self._documents: dict[str, ModuleDocument] = {}
        self._config_version = 0
        self._lock = asyncio.Lock()
        self.local_errors: dict[str, str] = {}
        self.last_error_code: str | None = None

    def current(self) -> AppConfig:
        return self._config

    @property
    def config_version(self) -> int:
        return self._config_version

    @property
    def poll_interval_seconds(self) -> int:
        documents = self._documents
        runtime = documents.get("runtime_policy")
        if runtime is None or runtime.effective is None:
            return 5
        return int(
            runtime.effective.config.get(
                "config_poll_interval_seconds", 5
            )
        )

    def module_active(self, module: str) -> bool:
        document = self._documents.get(module)
        return bool(
            document is not None
            and document.effective is not None
            and document.effective.state == ModuleState.ACTIVE
        )

    async def poll_once(self) -> ReloadResult:
        version = await self.repository.global_version()
        if version == self._config_version and self._documents:
            self.last_error_code = None
            return ReloadResult(False, version)
        async with self._lock:
            version = await self.repository.global_version()
            if version == self._config_version and self._documents:
                self.last_error_code = None
                return ReloadResult(False, version)
            documents = await self.repository.list_modules()
            if self._documents:
                changed = tuple(
                    module
                    for module in topological_modules(
                        MODULE_REGISTRY
                    )
                    if _module_signature(documents[module])
                    != _module_signature(self._documents[module])
                )
            else:
                changed = tuple(
                    module
                    for module in topological_modules(
                        MODULE_REGISTRY
                    )
                    if documents[module].effective is not None
                )
            candidate = project_app_config(documents, self.crypto)
            try:
                optional_failures = (
                    await self.lifecycle_manager.apply(
                        self._config, candidate, changed
                    )
                )
            except Exception:
                logger.exception(
                    "required runtime configuration adapter failed"
                )
                self.last_error_code = "RUNTIME_APPLY_FAILED"
                return ReloadResult(
                    False,
                    self._config_version,
                    changed_modules=changed,
                    error_code="RUNTIME_APPLY_FAILED",
                )
            if optional_failures:
                for module in optional_failures:
                    if module in self._documents:
                        documents[module] = self._documents[module]
                    self.local_errors[module] = (
                        "RUNTIME_APPLY_FAILED"
                    )
                candidate = project_app_config(
                    documents, self.crypto
                )
            for module in changed:
                if module not in optional_failures:
                    self.local_errors.pop(module, None)
            self._documents = documents
            self._config = candidate
            self._config_version = version
            self.last_error_code = None
            return ReloadResult(
                True,
                version,
                changed_modules=changed,
                optional_failures=optional_failures,
            )

    async def poll_forever(
        self, stop_event: asyncio.Event
    ) -> None:
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except Exception:
                self.last_error_code = "CONFIG_POLL_FAILED"
                logger.exception("runtime configuration poll failed")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except TimeoutError:
                pass
