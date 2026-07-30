from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine


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
    "operations/health-and-readiness.md",
    "operations/logs-and-observability.md",
    "operations/backup-and-restore.md",
    "operations/credential-and-key-rotation.md",
    "operations/troubleshooting.md",
    "reference/configuration-modules.md",
    "reference/rest-api.md",
    "reference/compatibility.md",
}


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


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
def test_compose_guide_uses_safe_environment_generator(path: str):
    text = _read(path)

    required = {
        "scripts/deploy/create-external-mysql-env.sh",
        "--skip-connection-test",
        "--image",
        "SELECT 1",
        "mysql+asyncmy",
        "host.docker.internal",
        "Use these settings? [Y/n]",
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
        "agentic_db_purchase",
        "resource_pool",
        "SKIPPED",
        "external_base_url",
    }
    assert not [term for term in required if term not in text]


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
