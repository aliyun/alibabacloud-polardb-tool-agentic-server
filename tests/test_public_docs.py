from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from server.aliyun.endpoints import POLARDB_ENDPOINTS


ROOT = Path(__file__).resolve().parents[1]
ENGLISH_DOCS = tuple(
    str(path.relative_to(ROOT))
    for path in sorted((ROOT / "docs" / "en").rglob("*.md"))
)
CHINESE_DOCS = tuple(
    str(path.relative_to(ROOT))
    for path in sorted((ROOT / "docs" / "zh-cn").rglob("*.md"))
)
PUBLIC_DOCS = (
    "README.md",
    "README_zh-CN.md",
    *ENGLISH_DOCS,
    *CHINESE_DOCS,
    "CONTRIBUTING.md",
    ".env.example",
)
PAIRED_DOCS = (
    ("README.md", "README_zh-CN.md"),
    *tuple(
        (path, path.replace("docs/en/", "docs/zh-cn/", 1))
        for path in ENGLISH_DOCS
    ),
)
TOOLS = {
    "list_db_instances",
    "create_db_instance",
    "describe_db_instance",
    "delete_db_instance",
}
TOOL_ERRORS = {
    "INVALID_CLIENT_TOKEN",
    "IDEMPOTENCY_CONFLICT",
    "UNSUPPORTED_DB_TYPE",
    "NO_PROVISIONING_BACKEND",
    "CAPACITY_EXHAUSTED",
    "DB_INSTANCE_NOT_FOUND",
    "INVALID_CURSOR",
    "RATE_LIMITED",
}
OFFICIAL_MULTITENANT_URL = (
    "https://help.aliyun.com/zh/polardb/polardb-for-mysql/"
    "user-guide/multi-tenant-management-instructions"
)
REQUIRED_GUIDES = {
    "administration/users-and-departments.md",
    "administration/authentication.md",
    "administration/agents-and-tokens.md",
    "administration/audit-and-security.md",
    "agents/connect-mcp-client.md",
    "agents/tool-reference.md",
    "agents/sql-access-model.md",
    "database-instances/registration.md",
    "database-instances/multitenant-provisioning.md",
    "database-instances/agent-rest-provisioning.md",
    "database-instances/dedicated-hot-pools.md",
    "operations/health-and-readiness.md",
    "operations/logs-and-observability.md",
    "operations/backup-and-restore.md",
    "operations/credential-and-key-rotation.md",
    "operations/troubleshooting.md",
    "reference/configuration-modules.md",
    "reference/rest-api.md",
    "reference/compatibility.md",
    "knowledge/polarrag-onboarding.md",
}

POLARRAG_MCP_TOOLS = {
    "list_knowledge_resources",
    "kb_search",
    "kb_fetch_context",
    "doc_find_by_name",
    "doc_status",
    "doc_recall",
    "doc_get_original",
    "doc_delete",
    "doc_rechunk",
    "prepare_document_upload",
    "complete_document_upload",
}


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/database-instances/agent-rest-provisioning.md",
        "docs/zh-cn/database-instances/agent-rest-provisioning.md",
    ),
)
def test_agent_rest_guides_cover_shipped_lifecycle_contract(path: str):
    text = _read(path)
    required = {
        "/mcp/rest/db-instances",
        "/mcp/rest/openapi.json",
        "Cache-Control: no-store",
        "client_token",
        "provisioning_mode",
        "multitenant",
        "dedicated",
        "CREATING",
        "READY",
        "FAILED",
        "DELETING",
        "COOLING_DOWN",
        "RESTORING",
        "DELETED",
        "DELETE_FAILED",
        "POOL_CAPACITY_LIMIT_REACHED",
        "RATE_LIMITED",
        "RESOURCE_NOT_FOUND",
    }
    assert not [term for term in required if term not in text]


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/database-instances/dedicated-hot-pools.md",
        "docs/zh-cn/database-instances/dedicated-hot-pools.md",
    ),
)
def test_dedicated_hot_pool_guides_cover_cost_and_recovery_guards(path: str):
    text = _read(path)
    required = {
        "dedicated_pool_enabled",
        "target_size",
        "max_total_members",
        "max_member_purchases_per_hour",
        "delete_cooldown_duration_hours",
        "24",
        "FRESH",
        "STALE",
        "CHECKING",
        "QUARANTINED",
        "CREATE USER",
        "dry_run",
        "apply",
        "PAS_ENCRYPTION_KEY",
        "agentic-dedicated-mysql",
        "DBMinorVersion=8.0.2",
        "essdpl1",
        "https://vpc.console.aliyun.com/vpc/",
        "DEDICATED_WORKER_NOT_RUNNING",
        "ALIYUN_ACCESS_NOT_CONFIGURED",
        "dedicated_pool_simulation_enabled",
        "NOT_STARTED",
        "PREWARMING",
        "PARTIALLY_READY",
        "CAPACITY_LIMITED",
        "routing_order",
        "primary",
        "fallback",
        "PURCHASE_PROFILE_UPGRADE_REQUIRED",
    }
    assert not [term for term in required if term not in text]
    assert (
        "Agent MySQL default permissions" in text
        or "为 Agent 分配的默认 MySQL 权限" in text
    )


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/reference/configuration-modules.md",
        "docs/zh-cn/reference/configuration-modules.md",
    ),
)
def test_configuration_guides_cover_dedicated_runtime_safety(path: str):
    text = _read(path)
    required = {
        "dedicated_pool_enabled",
        "dedicated_pool_simulation_enabled",
        "dedicated_worker_heartbeat_interval_seconds",
        "dedicated_worker_heartbeat_stale_after_seconds",
        "10",
        "30",
    }
    assert not [term for term in required if term not in text]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/knowledge/polarrag-onboarding.md",
        "docs/zh-cn/knowledge/polarrag-onboarding.md",
    ),
)
def test_polarrag_onboarding_covers_end_to_end_admin_delivery(path: str):
    text = _read(path)
    required = {
        "polarrag",
        "PUBLIC",
        "PERSONAL",
        "PAS_ENCRYPTION_KEY",
        "pas_user_agent_",
        "knowledge_resource_id",
        "100 MiB",
        "8 MiB",
        "24",
        "15",
        "feishu",
        "sharepoint",
        *POLARRAG_MCP_TOOLS,
    }
    assert not [term for term in required if term not in text]


def test_polarrag_onboarding_is_linked_from_public_indexes() -> None:
    expected = {
        "README.md": "docs/en/knowledge/polarrag-onboarding.md",
        "README_zh-CN.md": "docs/zh-cn/knowledge/polarrag-onboarding.md",
        "docs/en/README.md": "knowledge/polarrag-onboarding.md",
        "docs/zh-cn/README.md": "knowledge/polarrag-onboarding.md",
    }
    for path, link in expected.items():
        assert link in _read(path)


def test_polarrag_onboarding_directs_large_files_to_enterprise_knowledge_space() -> None:
    expected = {
        "docs/en/knowledge/polarrag-onboarding.md": "enterprise knowledge space",
        "docs/zh-cn/knowledge/polarrag-onboarding.md": "企业知识空间自动文档上传",
    }
    official_guide = (
        "https://help.aliyun.com/zh/polardb/polardb-for-mysql/"
        "create-and-use-an-enterprise-knowledge-space#upload-oss-section"
    )
    for path, phrase in expected.items():
        text = _read(path)
        assert phrase in text
        assert official_guide in text


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/knowledge/polarrag-onboarding.md",
        "docs/zh-cn/knowledge/polarrag-onboarding.md",
    ),
)
def test_polarrag_onboarding_declares_its_upload_contract_compatibility(path: str) -> None:
    text = _read(path)
    required = {
        "v0.0.7",
        "resume_document_upload",
        "abort_document_upload",
        "prepare_document_upload",
        "complete_document_upload",
        "upload_session_id",
        "parts",
    }
    assert not [term for term in required if term not in text]


@pytest.mark.parametrize(
    ("path", "required"),
    (
        (
            "docs/en/knowledge/polarrag-onboarding.md",
            {
                "`polarrag`",
                "`feishu`",
                "`sharepoint`",
                "Feishu-synchronized",
                "automatic identity synchronization",
            },
        ),
        (
            "docs/zh-cn/knowledge/polarrag-onboarding.md",
            {
                "`polarrag`",
                "`feishu`",
                "`sharepoint`",
                "飞书同步",
                "企业身份自动同步",
            },
        ),
    ),
)
def test_polarrag_onboarding_explains_identity_provider_selection(
    path: str, required: set[str]
) -> None:
    text = _read(path)
    assert not [term for term in required if term not in text]


@pytest.mark.parametrize(
    ("english_path", "chinese_path"),
    PAIRED_DOCS,
)
def test_bilingual_pages_have_reciprocal_language_links_near_top(
    english_path: str,
    chinese_path: str,
):
    english = _read(english_path)
    chinese = _read(chinese_path)

    expected_chinese_link = Path(
        Path(english_path).parent,
        Path(chinese_path).relative_to(Path(chinese_path).parent),
    )
    assert "简体中文" in "\n".join(english.splitlines()[:8])
    assert "English" in "\n".join(chinese.splitlines()[:8])
    assert Path(english_path).name in chinese
    assert Path(chinese_path).name in english
    assert expected_chinese_link.name in english


@pytest.mark.parametrize(
    ("english_path", "chinese_path"),
    PAIRED_DOCS,
)
def test_bilingual_pages_keep_structural_and_contract_parity(
    english_path: str,
    chinese_path: str,
):
    english = _read(english_path)
    chinese = _read(chinese_path)

    assert len(re.findall(r"^## ", english, flags=re.MULTILINE)) == len(
        re.findall(r"^## ", chinese, flags=re.MULTILINE)
    )
    assert english.count("```") == chinese.count("```")
    machine_terms = TOOLS | TOOL_ERRORS | {"expires_at"}
    assert {
        term for term in machine_terms if term in english
    } == {term for term in machine_terms if term in chinese}


@pytest.mark.parametrize(
    "path",
    (
        "README.md",
        "README_zh-CN.md",
        "docs/en/database-instances/access-and-provisioning.md",
        "docs/zh-cn/database-instances/access-and-provisioning.md",
    ),
)
def test_primary_public_docs_use_final_tool_contract(path: str):
    text = _read(path)

    assert TOOLS <= set(re.findall(r"`([a-z_]+)`", text))
    assert "client_token" in text
    assert "db_instance_id" in text
    assert "polardb_mysql" in text


@pytest.mark.parametrize(
    "path",
    (
        "README.md",
        "README_zh-CN.md",
        "docs/en/database-instances/access-and-provisioning.md",
        "docs/zh-cn/database-instances/access-and-provisioning.md",
    ),
)
def test_primary_public_docs_do_not_publish_removed_contracts(path: str):
    text = _read(path)

    forbidden = (
        "PAS_POLARDB_TENANT_PROVISIONING_" + "MULTITENANT_INSTANCE_ID",
        "task" + "_id",
        "/api/users/{user_id}/api-tokens",
        "active lease",
        "active leases",
        "活跃租约",
        "expired lease",
        "expired leases",
        "过期租约",
        "overlap period",
        "重叠时间",
    )
    assert not [term for term in forbidden if term in text]


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/database-instances/access-and-provisioning.md",
        "docs/zh-cn/database-instances/access-and-provisioning.md",
    ),
)
def test_operator_guides_cover_shipped_admin_and_security_workflows(path: str):
    text = _read(path)

    required_terms = {
        "single_tenant",
        "multitenant",
        "registered",
        "direct_access",
        "provisioning_admin",
        "readonly",
        "readwrite",
        "db_instance:list",
        "db_instance:describe",
        "db_instance:credentials:read",
        "CREATING",
        "READY",
        "DELETING",
        "DELETED",
        "enable_multi_tenant",
        "rds_kill_user_list",
        "MULTITENANT_DISABLED",
        "MULTITENANT_ADMIN_REQUIRED",
        "MULTITENANT_PREFLIGHT_FAILED",
    }
    assert not [term for term in required_terms if term not in text]
    assert OFFICIAL_MULTITENANT_URL in text
    assert re.search(r"\bone[- ]to[- ]one\b", text, re.IGNORECASE) or "一对一" in text
    assert re.search(r"\bmultiple\b", text, re.IGNORECASE) or "多个" in text
    assert TOOL_ERRORS <= set(re.findall(r"`([A-Z_]+)`", text))
    assert "expires_at" in text
    assert "all four" in text.lower() or "四个" in text
    assert (
        "capacity does not remove" in text.lower()
        or "容量不会让" in text
    )


def test_tool_visibility_is_not_documented_as_a_capacity_oracle():
    english = _read(
        "docs/en/database-instances/access-and-provisioning.md"
    )
    chinese = _read(
        "docs/zh-cn/database-instances/access-and-provisioning.md"
    )

    assert "capacity-eligible provisioning binding" not in english
    assert not re.search(r"容量可用.*显示", chinese)


def test_documentation_indexes_link_to_bilingual_operator_guides():
    for path in ("docs/en/README.md", "docs/zh-cn/README.md"):
        index = _read(path)
        assert "setup/initial-setup.md" in index
        assert "configuration/guided-configuration.md" in index
        assert (
            "database-instances/access-and-provisioning.md"
            in index
        )


def test_public_guide_graph_is_mirrored_and_reachable_from_indexes():
    english = {
        str(path.relative_to(ROOT / "docs/en"))
        for path in (ROOT / "docs/en").rglob("*.md")
        if path.name != "README.md"
    }
    chinese = {
        str(path.relative_to(ROOT / "docs/zh-cn"))
        for path in (ROOT / "docs/zh-cn").rglob("*.md")
        if path.name != "README.md"
    }

    assert REQUIRED_GUIDES <= english
    assert english == chinese
    for locale, guides in (("en", english), ("zh-cn", chinese)):
        index = _read(f"docs/{locale}/README.md")
        assert not [guide for guide in guides if f"({guide})" not in index]


def test_contributing_defines_translation_workflow():
    text = _read("CONTRIBUTING.md")

    required_terms = (
        "canonical",
        "same relative path",
        "language-switch",
        "fluent reviewer",
    )
    assert not [term for term in required_terms if term not in text.lower()]
    assert re.search(r"same\s+pull\s+request", text, re.IGNORECASE)


def test_public_docs_do_not_link_internal_design_material():
    assert not [
        path
        for path in PUBLIC_DOCS
        if "docs/superpowers" in _read(path)
    ]


def test_mcp_identity_and_authentication_contract_is_public_and_stable():
    for locale in ("en", "zh-cn"):
        authentication = _read(
            f"docs/{locale}/administration/authentication.md"
        )
        assert not [
            term
            for term in {
                "iss",
                "aud",
                "sub",
                "iat",
                "exp",
                "jti",
                "type=access",
                "PKCE",
                "nonce",
                "authorization_endpoint",
                "token_endpoint",
                "UserInfo",
                "Retry-After",
                "localhost",
                "127.0.0.1",
                "::1",
            }
            if term not in authentication
        ]

        agents = _read(
            f"docs/{locale}/administration/agents-and-tokens.md"
        )
        assert not [
            term
            for term in {
                "pas_user_agent_",
                "expires_at",
                "issued_at",
                "last_used_at",
                "PolarRAG",
                "Space",
            }
            if term not in agents
        ]
        assert "UTC" in agents
        assert (
            "timezone offset" in agents
            if locale == "en"
            else "时区偏移" in agents
        )

        client_guide = _read(
            f"docs/{locale}/agents/connect-mcp-client.md"
        )
        example = re.search(
            r"```json\n(?P<payload>.*?)```",
            client_guide,
            flags=re.DOTALL,
        )
        assert example is not None
        configuration = json.loads(example.group("payload"))
        server = next(iter(configuration["mcpServers"].values()))
        assert set(server) == {"url", "headers"}
        assert set(server["headers"]) == {"Authorization"}

        polarrag = _read(f"docs/{locale}/knowledge/polarrag-mcp.md")
        assert "acl_context" in polarrag
        assert (
            "Space enabled after the Agent binding" in polarrag
            if locale == "en"
            else "建立 Agent 绑定后才启用的 Space" in polarrag
        )


def test_public_docs_exclude_internal_and_placeholder_content():
    forbidden = (
        "docs/customer",
        "gitlab.alibaba-inc",
        "TODO",
        "TBD",
    )

    assert not [
        f"{path}:{term}"
        for path in PUBLIC_DOCS
        for term in forbidden
        if term in _read(path)
    ]


def test_aliyun_access_docs_cover_temporary_credential_operations():
    """Catch a release that exposes a credential mode without safe guidance."""
    required_by_guide = {
        "configuration/guided-configuration.md": {
            "direct_ak",
            "assume_role",
            "ecs_ram_role",
            "_from_env",
            "--dry-run",
            "confirmation",
        },
        "reference/configuration-modules.md": {
            "direct_ak",
            "assume_role",
            "ecs_ram_role",
            "IMDSv2",
            "OPENAPI_PERMISSION_DENIED",
            "temporary credentials",
        },
        "deployment/networking.md": {
            "sts:AssumeRole",
            "trust policy",
            "IMDSv2",
            "no HTTP proxy",
        },
        "deployment/upgrade-and-rollback.md": {
            "operator-enforced",
            "configuration writes",
            "version 2",
            "PAS_ENCRYPTION_KEY",
            "direct mode",
        },
        "operations/credential-and-key-rotation.md": {
            "Clear the previous credential",
            "Retain it, but keep it disabled",
            "Use retained credential",
            "Delete retained credential",
            "display mask",
        },
        "operations/troubleshooting.md": {
            "OPENAPI_STS_SOURCE_CREDENTIAL_INVALID",
            "OPENAPI_STS_ASSUME_ROLE_DENIED",
            "OPENAPI_STS_ROLE_TRUST_REJECTED",
            "OPENAPI_STS_EXTERNAL_ID_MISMATCH",
            "OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED",
            "OPENAPI_ECS_METADATA_DISABLED",
            "OPENAPI_ECS_IMDSV2_UNAVAILABLE",
            "OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED",
        },
    }

    for relative_path, terms in required_by_guide.items():
        english = re.sub(r"\s+", " ", _read(f"docs/en/{relative_path}"))
        chinese = re.sub(r"\s+", " ", _read(f"docs/zh-cn/{relative_path}"))
        assert not [term for term in terms if term not in english]
        assert not [term for term in terms if term not in chinese]


def test_aliyun_access_docs_keep_error_migration_and_endpoint_contracts_aligned():
    temporary_credential_codes = {
        "OPENAPI_STS_SOURCE_CREDENTIAL_INVALID",
        "OPENAPI_STS_ASSUME_ROLE_DENIED",
        "OPENAPI_STS_ROLE_TRUST_REJECTED",
        "OPENAPI_STS_EXTERNAL_ID_MISMATCH",
        "OPENAPI_ECS_RAM_ROLE_NOT_ATTACHED",
        "OPENAPI_ECS_METADATA_DISABLED",
        "OPENAPI_ECS_IMDSV2_UNAVAILABLE",
        "OPENAPI_TEMPORARY_CREDENTIAL_EXPIRED",
    }
    for locale in ("en", "zh-cn"):
        guided = _read(f"docs/{locale}/configuration/guided-configuration.md")
        troubleshooting = _read(f"docs/{locale}/operations/troubleshooting.md")
        upgrade = _read(f"docs/{locale}/deployment/upgrade-and-rollback.md")
        networking = _read(f"docs/{locale}/deployment/networking.md")

        assert temporary_credential_codes <= set(
            re.findall(r"`(OPENAPI_[A-Z0-9_]+)`", guided)
        )
        assert temporary_credential_codes <= set(
            re.findall(r"`(OPENAPI_[A-Z0-9_]+)`", troubleshooting)
        )
        first_write = (
            "first version 2 write"
            if locale == "en"
            else "首次 version 2 写入"
        )
        draft_save = "draft save" if locale == "en" else "草稿保存"
        activation_boundary = (
            "After activation of version 2"
            if locale == "en"
            else "version 2 激活后"
        )
        assert first_write in upgrade
        assert draft_save in upgrade
        assert activation_boundary not in upgrade
        assert "polardb.aliyuncs.com" in networking
        assert "polardb.<region>.aliyuncs.com" in networking
        assert "polardb-vpc.<region>.aliyuncs.com" in networking
        assert "sts.<region>.aliyuncs.com" in networking


def test_networking_docs_list_exactly_the_resolver_global_polardb_regions():
    resolver_global_regions = {
        region_id
        for region_id, (public_endpoint, _) in POLARDB_ENDPOINTS.items()
        if public_endpoint == "polardb.aliyuncs.com"
    }

    for locale in ("en", "zh-cn"):
        networking = _read(f"docs/{locale}/deployment/networking.md")
        global_region_clause = re.search(
            r"central regions \(([^)]*)\)"
            if locale == "en"
            else r"中央地域（([^）]*)）",
            networking,
            flags=re.DOTALL,
        )
        assert global_region_clause is not None
        documented_regions = set(
            re.findall(r"`(cn-[a-z0-9-]+)`", global_region_clause.group(1))
        )
        assert documented_regions == resolver_global_regions


def test_example_configuration_contains_only_bootstrap_settings():
    env_example = _read(".env.example")

    assert set(
        re.findall(r"^(PAS_[A-Z0-9_]+)=", env_example, re.MULTILINE)
    ) == {"PAS_DATABASE_URL", "PAS_ENCRYPTION_KEY"}
    assert "guided UI" in env_example
    assert "32-byte root key" in env_example
    assert "at least 32 bytes" not in env_example


@pytest.mark.asyncio
async def test_example_database_url_uses_an_installed_async_driver():
    env_example = _read(".env.example")
    database_url = re.search(
        r"^PAS_DATABASE_URL=(.+)$",
        env_example,
        re.MULTILINE,
    )
    assert database_url is not None

    engine = create_async_engine(database_url.group(1))
    await engine.dispose()


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/getting-started/deploy-compose.md",
        "docs/zh-cn/getting-started/deploy-compose.md",
    ),
)
def test_compose_getting_started_uses_safe_environment_generator(path: str):
    text = _read(path)

    required = {
        "scripts/deploy/create-external-mysql-env.sh",
        "SELECT 1",
        "host.docker.internal",
        "Use these settings? [Y/n]",
        "external-mysql-env-generator.png",
    }
    assert not [term for term in required if term not in text]
    assert "python3 - <<'PY'" not in text
    assert "cp .env.example .env" not in text
    assert "mysql+aiomysql://" not in text


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/deployment/docker-compose.md",
        "docs/zh-cn/deployment/docker-compose.md",
    ),
)
def test_compose_operations_guide_uses_external_mysql_generator(
    path: str,
) -> None:
    text = _read(path)
    required = {
        "scripts/deploy/create-external-mysql-env.sh",
        "Use host.docker.internal instead? [Y/n]",
        "SELECT 1",
        "--skip-connection-test",
        "--image",
        "mysql+asyncmy",
        "--env-file",
        "compose.external-mysql.yaml",
    }

    assert not [term for term in required if term not in text]
    assert "export PAS_DATABASE_URL='mysql+asyncmy://" not in text


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/getting-started/cloud-resources.md",
        "docs/zh-cn/getting-started/cloud-resources.md",
    ),
)
def test_cloud_resource_guide_defers_url_encoding_to_generator(
    path: str,
) -> None:
    text = _read(path)

    assert "scripts/deploy/create-external-mysql-env.sh" in text
    assert (
        "mysql+asyncmy://USER:PASSWORD@ENDPOINT:3306/DATABASE"
        not in text
    )


@pytest.mark.parametrize(
    "path",
    (
        "README.md",
        "README_zh-CN.md",
        "docs/en/setup/initial-setup.md",
        "docs/zh-cn/setup/initial-setup.md",
    ),
)
def test_initial_setup_workflow_is_public(path: str):
    text = _read(path)
    required = {
        "PAS_DATABASE_URL",
        "PAS_ENCRYPTION_KEY",
        "pas config init",
    }
    assert not [term for term in required if term not in text]
    assert "config.example.yaml" not in text
    assert "PAS_ADMIN_INITIAL_PASSWORD" not in text


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/setup/initial-setup.md",
        "docs/zh-cn/setup/initial-setup.md",
    ),
)
def test_initial_setup_covers_container_token_delivery(path: str):
    text = _read(path)
    required = {
        "docker logs",
        "kubectl exec",
        "--all-pods=true",
        "POD=<pod-name>",
        "bootstrap-token issue",
        "--bootstrap-token-file",
        "15 minutes",
        "15 分钟",
    }
    assert "15 minutes" in text or "15 分钟" in text
    assert not [
        term
        for term in required - {"15 minutes", "15 分钟"}
        if term not in text
    ]


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/configuration/guided-configuration.md",
        "docs/zh-cn/configuration/guided-configuration.md",
    ),
)
def test_guided_configuration_covers_modules_and_workflows(path: str):
    text = _read(path)
    required = {
        "pas config apply --file onboarding.yaml --dry-run",
        "pas config export",
        "core_admin",
        "agent_token_auth",
        "user_sso",
        "aliyun_access",
        "runtime_policy",
        "CreateDBCluster",
        "Dedicated",
        "SKIPPED",
        "external_base_url",
    }
    assert not [term for term in required if term not in text]
    assert "pas config export --module resource_pool" not in text
    assert "agentic_db_purchase" not in text


def test_public_docs_do_not_advertise_retired_agentic_purchase_module():
    public_paths = [Path("README.md")]
    public_paths.extend(Path("docs/en").rglob("*.md"))
    public_paths.extend(Path("docs/zh-cn").rglob("*.md"))

    references = [
        str(path)
        for path in public_paths
        if "agentic_db_purchase" in path.read_text(encoding="utf-8")
    ]
    assert references == []


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/database-instances/dedicated-hot-pools.md",
        "docs/zh-cn/database-instances/dedicated-hot-pools.md",
    ),
)
def test_dedicated_pool_guides_explain_unified_supply(path: str):
    text = _read(path)
    required = {
        "ALLOCATED_PREPARING",
        "NO_INSTANCE_ASSIGNED",
        "LEGACY_POOL_CONFIG_PRESENT",
        "LEGACY_POOL_INSTANCE_PRESENT",
        "multitenant",
        "single-tenant",
        "agentic-dedicated-mysql",
    }
    assert not [term for term in required if term not in text]
    assert (
        "Auto-provisioning pool" in text
        or "自动供给池" in text
    )


@pytest.mark.parametrize(
    "path",
    (
        "docs/en/reference/rest-api.md",
        "docs/zh-cn/reference/rest-api.md",
    ),
)
def test_rest_api_does_not_advertise_retired_pool_or_quota_routes(path: str):
    text = _read(path)
    assert "/api/pool" not in text
    assert "/api/quota" not in text


def test_legacy_resource_pool_guides_are_retired():
    assert not (ROOT / "docs/en/getting-started/resource-pool.md").exists()
    assert not (ROOT / "docs/zh-cn/getting-started/resource-pool.md").exists()


def test_relative_markdown_links_resolve():
    unresolved: list[str] = []
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")

    for doc_path in PUBLIC_DOCS:
        doc = ROOT / doc_path
        for raw_target in link_pattern.findall(_read(doc_path)):
            target = raw_target.split("#", 1)[0].strip()
            if (
                not target
                or "://" in target
                or target.startswith(("mailto:", "#"))
            ):
                continue
            if not (doc.parent / target).resolve().exists():
                unresolved.append(f"{doc_path}: {raw_target}")

    assert unresolved == []
