from __future__ import annotations

from typing import Any

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
    credential_mode: str = "direct_ak"
    access_key_id: str = ""
    access_key_secret: str = ""
    role_arn: str = ""
    role_session_name: str = "polardb-agentic"
    sts_duration_seconds: int = 3600
    region_id: str = "cn-hangzhou"
    openapi_network: str = "public"


def _stringify_settings(values: dict[str, object]) -> dict[str, str]:
    return {
        key: str(value).lower() if isinstance(value, bool) else str(value)
        for key, value in values.items()
    }


class AgenticDBConfig(BaseModel):
    enabled: bool = True
    auto_stop_minutes: int = 30
    auto_delete_days: int = 90
    notify_before_delete_days: int = 7
    db_type: str = "MySQL"
    db_version: str = "8.0"
    db_minor_version: str = "8.0.2"
    db_node_class: str = "polar.mysql.sl.small.c"
    proxy_class: str = "polar.maxscale.g2.medium.c"
    proxy_type: str = "GENERAL"
    architecture: str = "X86"
    loose_polar_log_bin: str = "OFF"
    loose_x_engine: str = "OFF"
    pay_type: str = "Postpaid"
    serverless_type: str = "AgileServerless"
    scale_min: int = 0
    scale_max: int = 4
    allow_shut_down: bool = True
    scale_ro_num_min: int = 0
    scale_ro_num_max: int = 1
    storage_type: str = "essdpl1"
    storage_space: int = 20

    def spec_settings(self) -> dict[str, str]:
        lifecycle = {
            "enabled",
            "auto_stop_minutes",
            "auto_delete_days",
            "notify_before_delete_days",
        }
        return _stringify_settings(
            {
                key: value
                for key, value in self.model_dump().items()
                if key not in lifecycle
            }
        )


class ResourcePoolRuntimeConfig(BaseModel):
    target_size: int = 0
    region_id: str = ""
    vpc_id: str = ""
    vswitch_id: str = ""
    zone_id: str = ""
    security_ip_list: str = "127.0.0.1"
    endpoint_net_type: str = "Private"
    provisioning_poll_timeout_seconds: int = 600
    retry_after_seconds: int = 10

    def network_settings(self) -> dict[str, str]:
        keys = {
            "region_id",
            "vpc_id",
            "vswitch_id",
            "zone_id",
            "security_ip_list",
        }
        return _stringify_settings(
            {
                key: value
                for key, value in self.model_dump().items()
                if key in keys
            }
        )


class ConnectionPoolConfig(BaseModel):
    max_connections_per_pool: int = 5
    idle_timeout_seconds: int = 1800
    max_total_pools: int = 200
    health_check: bool = True
    cleanup_interval_s: int = 60


class TenantProvisioningConfig(BaseModel):
    enabled: bool = False
    # Deprecated lease-named compatibility fields. Remove after operators have
    # migrated to the resource-named environment settings below.
    max_active_leases: int = Field(default=100, ge=1)
    max_active_leases_per_agent: int = Field(default=20, ge=1)
    max_active_resources_per_agent: int | None = Field(default=None, ge=1)
    resource_min_cpu: int = Field(default=0, ge=0)
    resource_max_cpu: int = Field(default=2, ge=1)
    ddl_concurrency: int = Field(default=4, ge=1)
    worker_poll_interval_seconds: int = Field(default=1, ge=1, le=5)
    worker_claim_ttl_seconds: int = Field(default=120, ge=10)
    worker_claim_renew_seconds: int = Field(default=30, ge=1)
    worker_max_retries: int = Field(default=5, ge=0)
    worker_initial_backoff_seconds: int = Field(default=1, ge=1)
    worker_max_backoff_seconds: int = Field(default=30, ge=1)
    health_check_interval_seconds: int = Field(default=10, ge=1)
    health_stale_after_seconds: int = Field(default=30, ge=2)
    backend_health_stale_after_seconds: int | None = Field(default=None, ge=2)
    describe_max_requests_per_second: int = Field(default=2, ge=1)

    @model_validator(mode="after")
    def _validate_tenant_provisioning(self) -> "TenantProvisioningConfig":
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
    agentic_db: AgenticDBConfig = Field(default_factory=AgenticDBConfig)
    connection_pool: ConnectionPoolConfig = Field(default_factory=ConnectionPoolConfig)
    tenant_provisioning: TenantProvisioningConfig = Field(
        default_factory=TenantProvisioningConfig
    )
    resource_pool: ResourcePoolRuntimeConfig = Field(
        default_factory=ResourcePoolRuntimeConfig
    )
    endpoint_cache_ttl_seconds: int = 300

    def provisioning_settings(self) -> dict[str, str]:
        settings = self.agentic_db.spec_settings()
        settings.update(self.resource_pool.network_settings())
        return settings


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
