from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
MYSQL_IMAGE = (
    "mysql:8.0.44@"
    "sha256:9c3380eac945af0736031b200027f581925927c81e010056214a4bd6b6693714"
)
DEFAULT_PAS_IMAGE = (
    "ghcr.io/aliyun/"
    f"alibabacloud-polardb-tool-agentic-server:{CURRENT_VERSION}"
)


def _load(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text())


def test_compose_owns_mysql_migration_and_server_order() -> None:
    compose = _load("compose.yaml")
    services = compose["services"]

    assert set(services) == {"mysql", "migrate", "server"}
    assert services["mysql"]["image"] == (
        "${MYSQL_IMAGE:-" + MYSQL_IMAGE + "}"
    )
    assert "ports" not in services["mysql"]
    assert services["mysql"]["volumes"] == [
        "mysql-data:/var/lib/mysql"
    ]
    assert (
        services["migrate"]["depends_on"]["mysql"]["condition"]
        == "service_healthy"
    )
    assert services["migrate"]["command"] == ["database", "migrate"]
    assert services["migrate"]["restart"] == "no"
    assert (
        services["server"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert services["server"]["restart"] == "unless-stopped"


def test_pas_services_share_image_and_only_bootstrap_settings() -> None:
    services = _load("compose.yaml")["services"]

    for name in ("migrate", "server"):
        assert services[name]["image"] == (
            "${PAS_IMAGE:-" + DEFAULT_PAS_IMAGE + "}"
        )
        assert set(services[name]["environment"]) == {
            "PAS_DATABASE_URL",
            "PAS_ENCRYPTION_KEY",
        }
        assert services[name]["read_only"] is True
        assert "/tmp" in services[name]["tmpfs"]
    assert services["server"]["ports"] == [
        "${PAS_PORT:-18760}:18760"
    ]


@pytest.mark.parametrize(
    "path",
    [
        "deploy/compose/compose.external-mysql.yaml",
        "deploy/compose/compose.external-postgres.yaml",
    ],
)
def test_external_database_compose_has_no_embedded_database(
    path: str,
) -> None:
    services = _load(path)["services"]

    assert set(services) == {"migrate", "server"}
    for name in ("migrate", "server"):
        assert services[name]["image"] == (
            "${PAS_IMAGE:-" + DEFAULT_PAS_IMAGE + "}"
        )
        assert "PYTHONPATH" not in services[name]["environment"]
    assert services["migrate"]["command"] == ["database", "migrate"]
    assert (
        services["server"]["depends_on"]["migrate"]["condition"]
        == "service_completed_successfully"
    )


def test_compose_env_example_uses_current_image_version() -> None:
    content = (ROOT / ".env.compose.example").read_text(encoding="utf-8")

    assert f"PAS_IMAGE={DEFAULT_PAS_IMAGE}\n" in content


def test_docker_compose_config_is_valid_when_cli_is_available() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is not installed")
    environment = {
        **os.environ,
        "PAS_ENCRYPTION_KEY": "test-only-not-a-production-root-key",
        "MYSQL_ROOT_PASSWORD": "test-root-password",
        "MYSQL_PASSWORD": "test-pas-password",
    }

    result = subprocess.run(
        ["docker", "compose", "-f", "compose.yaml", "config", "--quiet"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
