from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 18760
    public_base_url: str = "http://localhost:18760"
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


class DatabaseConfig(BaseModel):
    url: str = "sqlite+aiosqlite:///data/polardb_agentic.db"
    pool_size: int = 10
    echo: bool = False


class AliyunConfig(BaseModel):
    access_key_id: str = ""
    access_key_secret: str = ""
    region_id: str = "cn-hangzhou"


class AgenticDBConfig(BaseModel):
    enabled: bool = True
    db_cluster_class: str = "polar.mysql.ag.xs"
    auto_stop_minutes: int = 30
    auto_delete_days: int = 90
    notify_before_delete_days: int = 7


class ConnectionPoolConfig(BaseModel):
    max_connections_per_pool: int = 5
    idle_timeout_seconds: int = 1800
    max_total_pools: int = 200
    health_check: bool = True
    cleanup_interval_s: int = 60


class PolarDBConfig(BaseModel):
    agentic_db: AgenticDBConfig = Field(default_factory=AgenticDBConfig)
    connection_pool: ConnectionPoolConfig = Field(default_factory=ConnectionPoolConfig)
    endpoint_cache_ttl_seconds: int = 300


class RateLimitConfig(BaseModel):
    enabled: bool = True
    requests_per_minute: int = 60
    burst: int = 10


class AuditConfig(BaseModel):
    enabled: bool = True
    encrypt_sql_text: bool = False
    retention_days: int = 90


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


class EncryptionConfig(BaseModel):
    method: str = "aes-256-gcm"
    key: str = ""
    kms_key_id: str = ""


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    aliyun: AliyunConfig = Field(default_factory=AliyunConfig)
    polardb: PolarDBConfig = Field(default_factory=PolarDBConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    sql_security: SQLSecurityConfig = Field(default_factory=SQLSecurityConfig)
    encryption: EncryptionConfig = Field(default_factory=EncryptionConfig)

    @model_validator(mode="after")
    def _validate_config(self) -> "AppConfig":
        if self.auth.mode == "builtin":
            pwd = os.environ.get("PAS_ADMIN_INITIAL_PASSWORD", "")
            if not pwd:
                import warnings
                warnings.warn(
                    "PAS_ADMIN_INITIAL_PASSWORD is not set. "
                    "Required for builtin auth mode first-run admin creation.",
                    stacklevel=2,
                )

        if self.encryption.key:
            import base64 as _b64
            try:
                key_bytes = _b64.b64decode(self.encryption.key)
            except Exception:
                raise ValueError("PAS_ENCRYPTION_KEY must be a valid base64 string")
            if len(key_bytes) != 32:
                raise ValueError("PAS_ENCRYPTION_KEY must be a base64-encoded 32-byte key")

        if not self.server.dev_mode and not self.auth.jwt.private_key_path and not self.auth.jwt.private_key:
            import warnings
            warnings.warn(
                "JWT keys not configured. Auto-generated keys will be persisted to "
                "the shared database for multi-node consistency. "
                "For multi-node deployments, set auth.jwt.private_key_path/public_key_path "
                "or PAS_AUTH_JWT_PRIVATE_KEY/PAS_AUTH_JWT_PUBLIC_KEY env vars.",
                stacklevel=2,
            )

        if not self.server.dev_mode:
            url = self.server.public_base_url
            if "localhost" in url or "127.0.0.1" in url:
                raise ValueError(
                    f"server.public_base_url is '{url}' which contains localhost. "
                    "This MUST be set to the actual externally-reachable URL in "
                    "production (PAS_SERVER_PUBLIC_BASE_URL env var or config). "
                    "Set server.dev_mode=true to suppress this check."
                )

        return self


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _apply_env_overrides(config_dict: dict) -> dict:
    """Apply PAS_* environment variable overrides.

    Convention: PAS_SECTION_KEY maps to config_dict[section][key].
    For nested sections: PAS_SECTION_SUBSECTION_KEY maps to config_dict[section][subsection][key].
    """
    env_mappings: dict[str, tuple[list[str], type]] = {
        "PAS_SERVER_HOST": (["server", "host"], str),
        "PAS_SERVER_PORT": (["server", "port"], int),
        "PAS_SERVER_WORKERS": (["server", "workers"], int),
        "PAS_SERVER_LOG_LEVEL": (["server", "log_level"], str),
        "PAS_SERVER_DEV_MODE": (["server", "dev_mode"], bool),
        "PAS_SERVER_PUBLIC_BASE_URL": (["server", "public_base_url"], str),
        "PAS_SERVER_CORS_ORIGINS": (["server", "cors_origins"], str),
        "PAS_AUTH_MODE": (["auth", "mode"], str),
        "PAS_OIDC_DISCOVERY_URL": (["auth", "oidc", "discovery_url"], str),
        "PAS_OIDC_CLIENT_ID": (["auth", "oidc", "client_id"], str),
        "PAS_OIDC_CLIENT_SECRET": (["auth", "oidc", "client_secret"], str),
        "PAS_OIDC_AUTHORIZATION_ENDPOINT": (["auth", "oidc", "authorization_endpoint"], str),
        "PAS_OIDC_TOKEN_ENDPOINT": (["auth", "oidc", "token_endpoint"], str),
        "PAS_OIDC_USERINFO_ENDPOINT": (["auth", "oidc", "userinfo_endpoint"], str),
        "PAS_OIDC_JWKS_URI": (["auth", "oidc", "jwks_uri"], str),
        "PAS_OIDC_SCOPES": (["auth", "oidc", "scopes"], str),
        "PAS_OIDC_USER_ID_CLAIM": (["auth", "oidc", "user_id_claim"], str),
        "PAS_OIDC_DISPLAY_NAME_CLAIM": (["auth", "oidc", "display_name_claim"], str),
        "PAS_OIDC_EMAIL_CLAIM": (["auth", "oidc", "email_claim"], str),
        "PAS_OIDC_REDIRECT_URI": (["auth", "oidc", "redirect_uri"], str),
        "PAS_OIDC_USERINFO_TOKEN_METHOD": (["auth", "oidc", "userinfo_token_method"], str),
        "PAS_OIDC_PROVIDER_NAME": (["auth", "oidc", "provider_name"], str),
        "PAS_OIDC_IDP_PKCE": (["auth", "oidc", "idp_pkce"], bool),
        "PAS_OIDC_ID_TOKEN_ALGORITHMS": (["auth", "oidc", "id_token_algorithms"], str),
        "PAS_DATABASE_URL": (["database", "url"], str),
        "PAS_DATABASE_POOL_SIZE": (["database", "pool_size"], int),
        "PAS_DATABASE_ECHO": (["database", "echo"], bool),
        "PAS_ALIYUN_ACCESS_KEY_ID": (["aliyun", "access_key_id"], str),
        "PAS_ALIYUN_ACCESS_KEY_SECRET": (["aliyun", "access_key_secret"], str),
        "PAS_ALIYUN_REGION_ID": (["aliyun", "region_id"], str),
        "PAS_AUTH_DEFAULT_DEPARTMENT": (["auth", "default_department"], str),
        "PAS_AUTH_JWT_PRIVATE_KEY_PATH": (["auth", "jwt", "private_key_path"], str),
        "PAS_AUTH_JWT_PUBLIC_KEY_PATH": (["auth", "jwt", "public_key_path"], str),
        "PAS_AUTH_JWT_PRIVATE_KEY": (["auth", "jwt", "private_key"], str),
        "PAS_AUTH_JWT_PUBLIC_KEY": (["auth", "jwt", "public_key"], str),
        "PAS_AUTH_JWT_ACCESS_TOKEN_EXPIRE_MINUTES": (["auth", "jwt", "access_token_expire_minutes"], int),
        "PAS_AUTH_JWT_REFRESH_TOKEN_EXPIRE_DAYS": (["auth", "jwt", "refresh_token_expire_days"], int),
        "PAS_AUTH_WEB_SSO_GUARD_ENABLED": (["auth", "web_sso_guard", "enabled"], bool),
        "PAS_AUTH_WEB_SSO_GUARD_SESSION_TTL_HOURS": (["auth", "web_sso_guard", "session_ttl_hours"], int),
        "PAS_ENCRYPTION_KEY": (["encryption", "key"], str),
        "PAS_LOGGING_LOG_DIR": (["logging", "log_dir"], str),
        "PAS_LOGGING_LOG_FILE": (["logging", "log_file"], str),
        "PAS_LOGGING_MAX_BYTES": (["logging", "max_bytes"], int),
        "PAS_LOGGING_BACKUP_COUNT": (["logging", "backup_count"], int),
        "PAS_LOGGING_TIMEZONE": (["logging", "timezone"], str),
    }

    # Fields that accept comma-separated lists from env vars
    _list_env_vars = {"PAS_OIDC_SCOPES", "PAS_SERVER_CORS_ORIGINS", "PAS_OIDC_ID_TOKEN_ALGORITHMS"}

    for env_var, (path, typ) in env_mappings.items():
        value = os.environ.get(env_var)
        if value is not None:
            converted: Any
            if env_var in _list_env_vars:
                converted = [s.strip() for s in value.split(",") if s.strip()]
            elif typ is bool:
                converted = value.lower() in ("true", "1", "yes")
            elif typ is int:
                converted = int(value)
            else:
                converted = value

            d = config_dict
            for part in path[:-1]:
                d = d.setdefault(part, {})
            d[path[-1]] = converted

    return config_dict


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load configuration from YAML file and environment variables.

    Priority: Environment variable > .env file > config.yaml > defaults.
    """
    from dotenv import load_dotenv
    load_dotenv(override=False)
    config_dict: dict[str, Any] = {}

    if config_path is None:
        config_path = os.environ.get("PAS_CONFIG_PATH", "config.yaml")

    path = Path(config_path)
    if path.exists():
        with open(path) as f:
            file_config = yaml.safe_load(f)
            if file_config:
                config_dict = file_config

    config_dict = _apply_env_overrides(config_dict)
    return AppConfig(**config_dict)


_config: AppConfig | None = None


def get_config() -> AppConfig:
    """Get or initialize the global config singleton."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reset_config() -> None:
    """Reset config singleton (for testing)."""
    global _config
    _config = None
