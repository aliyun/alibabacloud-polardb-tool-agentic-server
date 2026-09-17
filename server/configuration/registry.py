from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from server.configuration.aliyun_access import (
    ALIYUN_ACCESS_SCHEMA_VERSION,
    AliyunAccessConfig,
)
from server.configuration.secrets import SecretFieldSpec
from server.configuration.types import ModuleState
from server.configuration.url_policy import is_pas_ipv4_loopback_http_origin
from server.config import EnterpriseIdentitySyncConfig, PolarRAGToolLimitsConfig


SQL_SECURITY_SCHEMA_VERSION = 2
OBSERVABILITY_SCHEMA_VERSION = 2


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class KnowledgeModuleConfig(_StrictModel):
    enabled: bool = False
    validation_resource_id: str | None = Field(default=None, max_length=36)
    validation_user_id: str | None = Field(default=None, max_length=36)


class CoreAdminConfig(_StrictModel):
    username: str = Field(default="admin", min_length=1, max_length=255)


class AgentTokenAuthConfig(_StrictModel):
    enabled: Literal[True] = True


class ExternalTokenTrustConfig(_StrictModel):
    enabled: bool = False
    provider: Literal[
        "oidc_jwt",
        "oauth2_introspection",
        "oauth2_userinfo",
        "feishu",
        "buc",
    ] = "oidc_jwt"
    direct_mcp_enabled: bool = False
    identity_source_id: str | None = None
    expected_audience: str | None = None
    introspection_endpoint: AnyHttpUrl | None = None
    userinfo_endpoint: AnyHttpUrl | None = None
    client_id: str | None = None
    client_secret: str | None = None
    introspection_auth_method: Literal[
        "client_secret_basic",
        "client_secret_post",
    ] = "client_secret_basic"
    access_token_ttl_seconds: int = Field(default=28800, ge=60, le=86400)

    @model_validator(mode="after")
    def validate_provider_requirements(self) -> "ExternalTokenTrustConfig":
        if self.direct_mcp_enabled and not self.enabled:
            raise ValueError(
                "Direct MCP external tokens require external token trust"
            )
        if not self.enabled:
            return self
        if (
            self.provider == "feishu"
            and self.direct_mcp_enabled
            and not self.identity_source_id
        ):
            raise ValueError(
                "Direct MCP Feishu token trust requires identity_source_id"
            )
        if (
            self.provider == "oauth2_introspection"
            and self.introspection_endpoint is None
        ):
            raise ValueError(
                "OAuth token introspection requires introspection_endpoint"
            )
        if bool(self.client_id) != bool(self.client_secret):
            raise ValueError(
                "External validation client_id and client_secret must be configured together"
            )
        if (
            self.userinfo_endpoint is not None
            and self.provider == "oauth2_introspection"
            and not self.identity_source_id
        ):
            raise ValueError(
                "External UserInfo requires identity_source_id"
            )
        return self


class UserSSOConfig(_StrictModel):
    browser_login_enabled: bool = True
    protocol_mode: Literal["oidc", "oauth2_userinfo"] = "oidc"
    discovery_url: AnyHttpUrl | None = None
    issuer: AnyHttpUrl | None = None
    authorization_endpoint: AnyHttpUrl | None = None
    token_endpoint: AnyHttpUrl | None = None
    userinfo_endpoint: AnyHttpUrl | None = None
    jwks_uri: AnyHttpUrl | None = None
    client_id: str | None = Field(default=None, min_length=1)
    client_secret: str | None = Field(default=None, min_length=1)
    scopes: list[str] = Field(
        default_factory=lambda: ["openid", "profile", "email"]
    )
    user_id_claim: str = "sub"
    display_name_claim: str = "name"
    email_claim: str = "email"
    provider_name: str = "oidc"
    idp_pkce: bool = False
    userinfo_token_method: Literal[
        "bearer_header",
        "form_post",
        "query",
    ] = "bearer_header"
    id_token_algorithms: list[str] = Field(
        default_factory=lambda: ["RS256", "ES256"]
    )
    default_department: str = ""
    external_token_trust: ExternalTokenTrustConfig = Field(
        default_factory=ExternalTokenTrustConfig
    )

    @model_validator(mode="after")
    def validate_manual_endpoints(self) -> "UserSSOConfig":
        trust = self.external_token_trust
        if not self.browser_login_enabled:
            if not trust.enabled:
                raise ValueError(
                    "Token-exchange-only mode requires external token trust"
                )
            if trust.provider in {"oidc_jwt", "buc"}:
                raise ValueError(
                    "Token-exchange-only mode supports OAuth introspection, "
                    "OAuth UserInfo, or Feishu providers"
                )
            if (
                trust.provider == "oauth2_introspection"
                and (not trust.client_id or not trust.client_secret)
            ):
                raise ValueError(
                    "Token-exchange-only introspection requires dedicated "
                    "client_id and client_secret"
                )
            if (
                trust.provider == "oauth2_userinfo"
                and trust.userinfo_endpoint is None
            ):
                raise ValueError(
                    "Token-exchange-only UserInfo requires userinfo_endpoint"
                )
            return self
        if not self.client_id or not self.client_secret:
            raise ValueError(
                "Browser SSO requires client_id and client_secret"
            )
        manual_values = (
            self.issuer,
            self.authorization_endpoint,
            self.token_endpoint,
            self.userinfo_endpoint,
            self.jwks_uri,
        )
        if self.discovery_url is None and any(manual_values):
            if not (
                self.issuer
                and self.authorization_endpoint
                and self.token_endpoint):
                raise ValueError(
                    "Manual OIDC configuration requires issuer, authorization_endpoint, and token_endpoint"
                )
        if (
            self.protocol_mode == "oauth2_userinfo"
            and self.userinfo_endpoint is None
            and self.discovery_url is None
        ):
            raise ValueError(
                "OAuth 2.0 UserInfo mode requires userinfo_endpoint"
            )
        if trust.enabled and trust.provider == "buc":
            if self.protocol_mode != "oauth2_userinfo":
                raise ValueError(
                    "BUC token trust requires OAuth 2.0 UserInfo mode"
                )
            if self.userinfo_token_method != "form_post":
                raise ValueError(
                    "BUC token trust requires form_post UserInfo tokens"
                )
            if self.user_id_claim != "account_id":
                raise ValueError(
                    "BUC token trust requires user_id_claim=account_id"
                )
        return self


class RuntimePolicyConfig(_StrictModel):
    external_base_url: AnyHttpUrl | None = None
    cors_allowed_origins: list[AnyHttpUrl] = Field(default_factory=list)
    config_poll_interval_seconds: int = Field(default=5, ge=1, le=60)
    max_connections_per_pool: int = Field(default=5, ge=1)
    idle_timeout_seconds: int = Field(default=1800, ge=1)
    max_total_pools: int = Field(default=200, ge=1)
    worker_poll_interval_seconds: int = Field(default=1, ge=1, le=5)
    dedicated_worker_heartbeat_interval_seconds: int = Field(default=10, ge=1)
    dedicated_worker_heartbeat_stale_after_seconds: int = Field(
        default=30, ge=1
    )
    worker_claim_ttl_seconds: int = Field(default=120, ge=10)
    worker_claim_renew_seconds: int = Field(default=30, ge=1)
    dedicated_pool_enabled: bool = False
    dedicated_pool_simulation_enabled: bool = False
    dedicated_pool_preparation_mode: Literal["full", "openapi_only"] = Field(
        default="full",
        description=(
            "full requires private MySQL data-plane grants and verification; "
            "openapi_only creates billable cloud resources but pauses before "
            "private data-plane access"
        ),
    )
    delete_cooldown_duration_hours: int = Field(default=24, ge=1)
    invalidate_human_sessions_on_sso_change: bool = True

    @model_validator(mode="after")
    def _validate_dedicated_worker_heartbeat(self) -> "RuntimePolicyConfig":
        minimum = max(
            3 * self.dedicated_worker_heartbeat_interval_seconds,
            30,
        )
        if self.dedicated_worker_heartbeat_stale_after_seconds < minimum:
            raise ValueError(
                "Dedicated worker heartbeat stale threshold must be at least three intervals and 30 seconds"
            )
        return self


class SQLSecurityModuleConfig(_StrictModel):
    max_rows: int = Field(default=1000, ge=1)
    timeout_ms: int = Field(default=10000, ge=1)
    max_timeout_ms: int = Field(default=30000, ge=1)
    blocked_keywords: list[str] = Field(
        default_factory=lambda: ["DROP DATABASE"]
    )
    confirmable_statement_types: list[str] = Field(
        default_factory=lambda: ["DROP", "TRUNCATE", "ALTER", "DELETE"]
    )
    rate_limit_enabled: bool = True
    requests_per_minute: int = Field(default=60, ge=1)
    burst: int = Field(default=10, ge=1)


class ObservabilityConfig(_StrictModel):
    log_level: str = Field(
        default="info", pattern="^(debug|info|warning|error)$"
    )
    log_dir: str = "log"
    log_file: str = "alibabacloud-polardb-tool-agentic-server.log"
    max_bytes: int = Field(default=104_857_600, ge=1)
    backup_count: int = Field(default=10, ge=0)
    timezone: str = "UTC+8"
    audit_enabled: bool = True
    audit_retention_days: int = Field(default=180, ge=1)


class TokenSecurityConfig(_StrictModel):
    algorithm: str = "RS256"
    active_kid: str = ""
    private_key: str = ""
    public_keys: dict[str, str] = Field(default_factory=dict)
    access_token_expire_minutes: int = Field(default=480, ge=1)
    refresh_token_expire_days: int = Field(default=30, ge=1)
    session_epoch: int = Field(default=1, ge=1)


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    name: str
    model: type[BaseModel]
    initial_state: ModuleState
    dependencies: tuple[str, ...] = ()
    secret_fields: tuple[SecretFieldSpec, ...] = ()
    optional: bool = True
    system_owned: bool = False
    configurable: bool = True
    ui_hints: dict[str, Any] = field(default_factory=dict)
    schema_version: int = 1


MODULE_REGISTRY: dict[str, ModuleDefinition] = {
    "core_admin": ModuleDefinition(
        "core_admin",
        CoreAdminConfig,
        ModuleState.NOT_CONFIGURED,
        dependencies=("token_security",),
        optional=False,
    ),
    "agent_token_auth": ModuleDefinition(
        "agent_token_auth",
        AgentTokenAuthConfig,
        ModuleState.ACTIVE,
        optional=False,
        system_owned=True,
        configurable=False,
    ),
    "user_sso": ModuleDefinition(
        "user_sso",
        UserSSOConfig,
        ModuleState.SKIPPED,
        dependencies=("token_security",),
        secret_fields=(
            SecretFieldSpec("client_secret"),
            SecretFieldSpec("external_token_trust.client_secret"),
        ),
    ),
    "aliyun_access": ModuleDefinition(
        "aliyun_access",
        AliyunAccessConfig,
        ModuleState.SKIPPED,
        secret_fields=(
            SecretFieldSpec("direct_ak.access_key_id", display_mask=True),
            SecretFieldSpec("direct_ak.access_key_secret"),
            SecretFieldSpec(
                "assume_role.source_access_key_id", display_mask=True
            ),
            SecretFieldSpec("assume_role.source_access_key_secret"),
            SecretFieldSpec("assume_role.external_id"),
        ),
        schema_version=ALIYUN_ACCESS_SCHEMA_VERSION,
        ui_hints={
            "docs": [
                {
                    "label": "RAM: create and use AccessKey pairs",
                    "url": "https://help.aliyun.com/zh/ram/product-overview/quick-start-create-and-use-accesskey-pairs-for-programmatic-calls",
                    "description": (
                        "Use a RAM identity whose policy grants PolarDB "
                        "cluster creation (for example AliyunPolardbFullAccess)."
                    ),
                }
            ]
        },
    ),
    "runtime_policy": ModuleDefinition(
        "runtime_policy",
        RuntimePolicyConfig,
        ModuleState.ACTIVE,
        optional=False,
        system_owned=True,
    ),
    "knowledge": ModuleDefinition(
        "knowledge", KnowledgeModuleConfig, ModuleState.ACTIVE,
        optional=False, system_owned=True,
        ui_hints={"restart_required": True, "feature": "knowledge"},
    ),
    "polarrag_tool_limits": ModuleDefinition(
        "polarrag_tool_limits",
        PolarRAGToolLimitsConfig,
        ModuleState.ACTIVE,
        optional=False,
        system_owned=True,
        ui_hints={
            "local_limit_semantics": (
                "Limits apply independently in each PAS replica; aggregate "
                "capacity is approximately the configured value multiplied "
                "by the number of replicas."
            )
        },
    ),
    "enterprise_identity_sync": ModuleDefinition(
        "enterprise_identity_sync",
        EnterpriseIdentitySyncConfig,
        ModuleState.ACTIVE,
        optional=False,
        system_owned=True,
    ),
    "sql_security": ModuleDefinition(
        "sql_security",
        SQLSecurityModuleConfig,
        ModuleState.ACTIVE,
        optional=False,
        system_owned=True,
        schema_version=SQL_SECURITY_SCHEMA_VERSION,
    ),
    "observability": ModuleDefinition(
        "observability",
        ObservabilityConfig,
        ModuleState.ACTIVE,
        optional=False,
        system_owned=True,
        schema_version=OBSERVABILITY_SCHEMA_VERSION,
    ),
    "token_security": ModuleDefinition(
        "token_security",
        TokenSecurityConfig,
        ModuleState.ACTIVE,
        secret_fields=(SecretFieldSpec("private_key"),),
        optional=False,
        system_owned=True,
    ),
}


@dataclass(frozen=True, slots=True)
class ModuleValidationResult:
    valid: bool
    normalized_config: dict[str, Any]
    error_code: str | None = None
    message: str | None = None


def topological_modules(
    registry: dict[str, ModuleDefinition],
) -> tuple[str, ...]:
    indegree = {name: 0 for name in registry}
    dependents: dict[str, list[str]] = {
        name: [] for name in registry
    }
    for name, definition in registry.items():
        for dependency in definition.dependencies:
            if dependency not in registry:
                raise ValueError(
                    f"module '{name}' has unknown dependency '{dependency}'"
                )
            indegree[name] += 1
            dependents[dependency].append(name)

    queue = deque(name for name in registry if indegree[name] == 0)
    result: list[str] = []
    while queue:
        name = queue.popleft()
        result.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if len(result) != len(registry):
        raise ValueError("module dependency graph contains a cycle")
    return tuple(result)


def validate_module_config(
    module: str,
    config: dict[str, Any],
    *,
    effective_configs: dict[str, dict[str, Any]],
    allow_insecure_loopback_urls: bool = False,
) -> ModuleValidationResult:
    definition = MODULE_REGISTRY.get(module)
    if definition is None:
        return ModuleValidationResult(
            valid=False,
            normalized_config={},
            error_code="UNKNOWN_MODULE",
            message=f"Unknown module: {module}",
        )
    try:
        normalized = definition.model.model_validate(config).model_dump(
            mode="json"
        )
    except ValidationError:
        return ModuleValidationResult(
            valid=False,
            normalized_config={},
            error_code="INVALID_MODULE_CONFIG",
            message="Module configuration is invalid",
        )

    if module == "user_sso":
        runtime = effective_configs.get("runtime_policy", {})
        external_base_url = runtime.get("external_base_url")
        valid_external_base_url = (
            isinstance(external_base_url, str)
            and (
                external_base_url.startswith("https://")
                or (
                    allow_insecure_loopback_urls
                    and is_pas_ipv4_loopback_http_origin(external_base_url)
                )
            )
        )
        if not valid_external_base_url:
            return ModuleValidationResult(
                valid=False,
                normalized_config=normalized,
                error_code="EXTERNAL_BASE_URL_REQUIRED",
                message=(
                    "An explicit HTTPS external_base_url is required. "
                    "Local development mode additionally permits an exact "
                    "localhost or 127.0.0.1 HTTP origin."
                ),
            )
    return ModuleValidationResult(
        valid=True,
        normalized_config=normalized,
    )
