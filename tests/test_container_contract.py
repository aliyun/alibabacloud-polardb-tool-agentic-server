from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_production_dockerfile_contract() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    from_lines = [
        line for line in dockerfile.splitlines() if line.startswith("FROM ")
    ]

    assert len(from_lines) == 3
    assert all("@sha256:" in line for line in from_lines)
    assert {line.rsplit(" AS ", 1)[-1] for line in from_lines} == {
        "web-builder",
        "python-builder",
        "runtime",
    }
    assert "USER 10001:10001" in dockerfile
    assert 'ENTRYPOINT ["pas"]' in dockerfile
    assert 'CMD ["serve"]' in dockerfile
    assert "EXPOSE 18760" in dockerfile
    assert "org.opencontainers.image.source" in dockerfile
    assert "org.opencontainers.image.licenses" in dockerfile
    assert "COPY pyproject.toml uv.lock alembic.ini README.md LICENSE NOTICE ./" in dockerfile
    assert "COPY --from=python-builder /src/alembic.ini /app/alembic.ini" in dockerfile


def test_container_build_context_excludes_non_runtime_material() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    dockerignore = (ROOT / ".dockerignore").read_text().splitlines()

    assert not re.search(r"^COPY\\s+\\.\\s", dockerfile, re.MULTILINE)
    assert "COPY .env" not in dockerfile
    assert ".env*" in dockerignore
    assert ".git" in dockerignore
    assert "tests" in dockerignore
    assert "docs/superpowers" in dockerignore


def test_runtime_declares_only_expected_writable_paths() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "/tmp" in dockerfile
    assert "/app/log" in dockerfile
    assert "/var/run/pas" in dockerfile
    assert "node_modules" not in dockerfile


def test_arm64_source_dependencies_build_only_in_builder_stage() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    python_builder = dockerfile.split(
        " AS python-builder", maxsplit=1
    )[1].split("FROM ", maxsplit=1)[0]
    runtime = dockerfile.split(" AS runtime", maxsplit=1)[1]

    assert "apt-get update" in python_builder
    assert "build-essential" in python_builder
    assert "ARG DEBIAN_MIRROR=http://deb.debian.org/debian" in python_builder
    assert (
        "ARG DEBIAN_SECURITY_MIRROR=http://deb.debian.org/debian-security"
        in python_builder
    )
    assert "${DEBIAN_MIRROR}" in python_builder
    assert "${DEBIAN_SECURITY_MIRROR}" in python_builder
    assert "ARG PYPI_INDEX_URL=https://pypi.org/simple" in python_builder
    assert "PIP_INDEX_URL=${PYPI_INDEX_URL}" in python_builder
    assert "UV_DEFAULT_INDEX=${PYPI_INDEX_URL}" in python_builder
    assert "--mount=type=cache,target=/root/.cache/uv" in python_builder
    assert "UV_PROJECT_ENVIRONMENT=/app/.venv" in python_builder
    assert "COPY --from=python-builder /app/.venv /app/.venv" in dockerfile
    assert "build-essential" not in runtime


def test_cached_build_reinstalls_the_current_project_wheel() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert (
        "--reinstall-package "
        "alibabacloud-polardb-tool-agentic-server"
    ) in dockerfile
