from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 18760
    public_base_url: str = ""
    workers: int = 4
    log_level: str = "info"
    dev_mode: bool = False
    cors_origins: list[str] = Field(default_factory=list)


class OIDCConfig(BaseModel):
    preset: str | None = None
    discovery_url: str | None = None
    issuer: str | None = None
    client_id: str = ""
    client_secret: str = ""
    scopes: list[str] = Field(default_factory=lambda: ["openid", "profile", "email"])
    user_id_claim: str = "sub"
    display_name_claim: str = "name"
    email_claim: str = "email"
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    userinfo_endpoint: str | None = None
    jwks_uri: str | None = None
    redirect_uri: str | None = None
    userinfo_token_method: str = "bearer_header"
    provider_name: str = "oidc"
    idp_pkce: bool = False
    id_token_algorithms: list[str] = Field(
        default_factory=lambda: ["RS256", "ES256"]
    )


class BuiltinAuthConfig(BaseModel):
    admin_username: str = "admin"


class WebSSOGuardConfig(BaseModel):
    enabled: bool = False
    session_ttl_hours: int = 8
    excluded_paths: list[str] = Field(default_factory=list)


class JWTConfig(BaseModel):
    algorithm: str = "RS256"
    private_key_path: str = ""
    public_key_path: str = ""
    private_key: str = ""
    public_key: str = ""
    access_token_expire_minutes: int = 480
    refresh_token_expire_days: int = 30


class OAuthClientConfig(BaseModel):
    redirect_uris: list[str] = Field(default_factory=list)


class AuthConfig(BaseModel):
    mode: str = "builtin"
    oidc: OIDCConfig = Field(default_factory=OIDCConfig)
    builtin: BuiltinAuthConfig = Field(default_factory=BuiltinAuthConfig)
    jwt: JWTConfig = Field(default_factory=JWTConfig)
    oauth_clients: dict[str, OAuthClientConfig] = Field(default_factory=dict)
    default_department: str = ""
    web_sso_guard: WebSSOGuardConfig = Field(default_factory=WebSSOGuardConfig)


class AliyunConfig(BaseModel):
    """The one active Alibaba Cloud credential configuration for this process."""

    credential_mode: Literal["direct_ak", "assume_role", "ecs_ram_role"] = (
        "direct_ak"
    )
    direct_ak: "RuntimeDirectAKConfig | None" = None
    assume_role: "RuntimeAssumeRoleConfig | None" = None
    ecs_ram_role: "RuntimeECSRamRoleConfig | None" = None
    region_id: str = "cn-hangzhou"
    openapi_network: str = "public"
    credential_digest: str = ""
    config_revision: int = 0

    @model_validator(mode="before")
    @classmethod
    def _project_legacy_flat_credentials(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        result = dict(values)
        mode = result.get("credential_mode", "direct_ak")
        if mode == "direct_ak" and "direct_ak" not in result:
            key_id = result.get("access_key_id")
            key_secret = result.get("access_key_secret")
            if key_id or key_secret:
                result["direct_ak"] = {
                    "access_key_id": key_id or "",
                    "access_key_secret": key_secret or "",
                }
        elif mode == "assume_role" and "assume_role" not in result:
            key_id = result.get("access_key_id")
            key_secret = result.get("access_key_secret")
            if key_id or key_secret or result.get("role_arn"):
                result["assume_role"] = {
                    "source_access_key_id": key_id or "",
                    "source_access_key_secret": key_secret or "",
                    "role_arn": result.get("role_arn", ""),
                    "role_session_name": result.get(
                        "role_session_name", "polardb-agentic"
                    ),
                    "duration_seconds": result.get(
                        "sts_duration_seconds", 3600
                    ),
                    "external_id": result.get("external_id"),
                }
        elif mode == "ecs_ram_role" and "ecs_ram_role" not in result:
            result["ecs_ram_role"] = {
                "role_name": result.get("ecs_role_name"),
            }
        return result

    def has_active_credentials(self) -> bool:
        if self.credential_mode == "direct_ak":
            return bool(
                self.direct_ak
                and self.direct_ak.access_key_id
                and self.direct_ak.access_key_secret
            )
        if self.credential_mode == "assume_role":
            return bool(
                self.assume_role
                and self.assume_role.source_access_key_id
                and self.assume_role.source_access_key_secret
                and self.assume_role.role_arn
            )
        return self.ecs_ram_role is not None

    # Compatibility accessors for callers that still consume the v1 flat
    # runtime facade. New runtime code must use the mode-scoped blocks above.
    @property
    def access_key_id(self) -> str:
        if self.direct_ak is not None:
            return self.direct_ak.access_key_id
        if self.assume_role is not None:
            return self.assume_role.source_access_key_id
        return ""

    @property
    def access_key_secret(self) -> str:
        if self.direct_ak is not None:
            return self.direct_ak.access_key_secret
        if self.assume_role is not None:
            return self.assume_role.source_access_key_secret
        return ""

    @property
    def role_arn(self) -> str:
        return self.assume_role.role_arn if self.assume_role else ""

    @property
    def role_session_name(self) -> str:
        return (
            self.assume_role.role_session_name
            if self.assume_role
            else "polardb-agentic"
        )

    @property
    def sts_duration_seconds(self) -> int:
        return self.assume_role.duration_seconds if self.assume_role else 3600

    @property
    def external_id(self) -> str | None:
        return self.assume_role.external_id if self.assume_role else None

    @property
    def ecs_role_name(self) -> str | None:
        return self.ecs_ram_role.role_name if self.ecs_ram_role else None


class RuntimeDirectAKConfig(BaseModel):
    access_key_id: str
    access_key_secret: str


class RuntimeAssumeRoleConfig(BaseModel):
    source_access_key_id: str
    source_access_key_secret: str
    role_arn: str
    role_session_name: str = "polardb-agentic"
    duration_seconds: int = 3600
    external_id: str | None = None


class RuntimeECSRamRoleConfig(BaseModel):
    role_name: str | None = None
    metadata_policy: Literal["v2_only"] = "v2_only"


class ConnectionPoolConfig(BaseModel):
    max_connections_per_pool: int = 5
    idle_timeout_seconds: int = 1800
    max_total_pools: int = 200
    health_check: bool = True
    cleanup_interval_s: int = 60


class TenantProvisioningConfig(BaseModel):
    enabled: bool = False
    dedicated_pool_enabled: bool = False
    dedicated_pool_simulation_enabled: bool = False
    dedicated_pool_preparation_mode: Literal["full", "openapi_only"] = "full"
    # Deprecated lease-named compatibility fields. Remove after operators have
    # migrated to the resource-named environment settings below.
    max_active_leases: int = Field(default=100, ge=1)
    max_active_leases_per_agent: int = Field(default=20, ge=1)
    max_active_resources_per_agent: int | None = Field(default=None, ge=1)
    resource_min_cpu: int = Field(default=0, ge=0)
    resource_max_cpu: int = Field(default=2, ge=1)
    ddl_concurrency: int = Field(default=4, ge=1)
    worker_poll_interval_seconds: int = Field(default=1, ge=1, le=5)
    dedicated_worker_heartbeat_interval_seconds: int = Field(
        default=10, ge=1
    )
    dedicated_worker_heartbeat_stale_after_seconds: int = Field(
        default=30, ge=1
    )
    worker_claim_ttl_seconds: int = Field(default=120, ge=10)
    worker_claim_renew_seconds: int = Field(default=30, ge=1)
    worker_max_retries: int = Field(default=5, ge=0)
    worker_initial_backoff_seconds: int = Field(default=1, ge=1)
    worker_max_backoff_seconds: int = Field(default=30, ge=1)
    health_check_interval_seconds: int = Field(default=10, ge=1)
    health_stale_after_seconds: int = Field(default=30, ge=2)
    backend_health_stale_after_seconds: int | None = Field(default=None, ge=2)
    describe_max_requests_per_second: int = Field(default=2, ge=1)
    delete_cooldown_duration_hours: int = Field(default=24, ge=1)

    @model_validator(mode="after")
    def _validate_tenant_provisioning(self) -> "TenantProvisioningConfig":
        heartbeat_stale_floor = max(
            3 * self.dedicated_worker_heartbeat_interval_seconds,
            30,
        )
        if self.dedicated_worker_heartbeat_stale_after_seconds < 30:
            raise ValueError(
                "worker heartbeat stale threshold must be at least 30 seconds"
            )
        if (
            self.dedicated_worker_heartbeat_stale_after_seconds
            < heartbeat_stale_floor
        ):
            raise ValueError(
                "worker heartbeat stale threshold must be at least three heartbeat intervals"
            )
        if self.worker_claim_renew_seconds >= self.worker_claim_ttl_seconds:
            raise ValueError("claim renew interval must be less than claim TTL")
        if self.health_check_interval_seconds >= self.health_stale_after_seconds:
            raise ValueError("health check interval must be less than stale threshold")
        if (
            self.backend_health_stale_after_seconds is not None
            and self.health_check_interval_seconds
            >= self.backend_health_stale_after_seconds
        ):
            raise ValueError(
                "health check interval must be less than backend health stale threshold"
            )
        if self.resource_min_cpu > self.resource_max_cpu:
            raise ValueError("resource_min_cpu must not exceed resource_max_cpu")
        if self.worker_initial_backoff_seconds > self.worker_max_backoff_seconds:
            raise ValueError("initial worker backoff must not exceed maximum backoff")
        return self

    @property
    def effective_max_active_resources_per_agent(self) -> int:
        return (
            self.max_active_resources_per_agent
            if self.max_active_resources_per_agent is not None
            else self.max_active_leases_per_agent
        )

    @property
    def effective_backend_health_stale_after_seconds(self) -> int:
        return (
            self.backend_health_stale_after_seconds
            if self.backend_health_stale_after_seconds is not None
            else self.health_stale_after_seconds
        )


class PolarDBConfig(BaseModel):
    connection_pool: ConnectionPoolConfig = Field(default_factory=ConnectionPoolConfig)
    tenant_provisioning: TenantProvisioningConfig = Field(
        default_factory=TenantProvisioningConfig
    )
    endpoint_cache_ttl_seconds: int = 300


class RateLimitConfig(BaseModel):
    enabled: bool = True
    requests_per_minute: int = 60
    burst: int = 10


class AuditConfig(BaseModel):
    enabled: bool = True
    encrypt_sql_text: bool = False
    retention_days: int = Field(default=180, ge=1)
    cleanup_interval_seconds: int = Field(default=3600, ge=0)
    cleanup_batch_size: int = Field(default=500, ge=1, le=10000)


class SQLSecurityConfig(BaseModel):
    max_rows: int = 1000
    timeout_ms: int = 10000
    max_timeout_ms: int = 30000
    blocked_keywords: list[str] = Field(default_factory=lambda: ["DROP DATABASE"])
    blocked_statement_types: list[str] | None = None
    confirmable_statement_types: list[str] = Field(
        default_factory=lambda: ["DROP", "TRUNCATE", "ALTER", "DELETE"]
    )
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)

    @model_validator(mode="after")
    def _migrate_blocked_statement_types(self) -> "SQLSecurityConfig":
        if self.blocked_statement_types is not None:
            import warnings

            if "confirmable_statement_types" not in self.model_fields_set:
                self.confirmable_statement_types = self.blocked_statement_types
                warnings.warn(
                    "sql_security.blocked_statement_types is deprecated. "
                    "Use sql_security.confirmable_statement_types instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            else:
                warnings.warn(
                    "sql_security.blocked_statement_types is deprecated and ignored "
                    "when confirmable_statement_types is set.",
                    DeprecationWarning,
                    stacklevel=2,
                )
        return self


class LoggingConfig(BaseModel):
    log_dir: str = "log"
    log_file: str = "alibabacloud-polardb-tool-agentic-server.log"
    max_bytes: int = 100 * 1024 * 1024
    backup_count: int = 10
    timezone: str = "UTC+8"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    aliyun: AliyunConfig = Field(default_factory=AliyunConfig)
    polardb: PolarDBConfig = Field(default_factory=PolarDBConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    sql_security: SQLSecurityConfig = Field(default_factory=SQLSecurityConfig)


_config: AppConfig | None = None
_runtime_store: Any | None = None


def install_runtime_config_store(store: Any) -> None:
    """Install the process-wide immutable runtime configuration source."""
    global _runtime_store, _config
    _runtime_store = store
    _config = None


def get_config() -> AppConfig:
    """Return one immutable runtime snapshot reference for this call."""
    global _config
    if _runtime_store is not None:
        return _runtime_store.current()
    if _config is None:
        _config = AppConfig()
    return _config


def reset_config() -> None:
    """Reset config singleton (for testing)."""
    global _config, _runtime_store
    _config = None
    _runtime_store = None
