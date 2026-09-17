from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from server.version import __version__


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "alibabacloud-polardb-tool-agentic-server"
INTERNAL_IMAGE = f"reg.docker.alibaba-inc.com/apsaradb/{PACKAGE_NAME}"
pytestmark = pytest.mark.skipif(
    not (ROOT / ".fw.yml").is_file(),
    reason="internal container build assets are not part of the public export",
)


def test_fw_pipeline_declares_the_internal_image_artifact() -> None:
    pipeline = yaml.safe_load((ROOT / ".fw.yml").read_text(encoding="utf-8"))

    assert pipeline["version"] == 1.0
    assert pipeline["variables"]["PACKAGE_NAME"] == PACKAGE_NAME
    assert pipeline["variables"]["IMAGE_TAG"] == __version__
    assert pipeline["variables"]["DOCKERFILE_PATH"] == (f"docker/Dockerfile-{PACKAGE_NAME}")
    assert pipeline["machine"]["standard"]["7u2_dockerce_23"] == ["default"]
    assert pipeline["scripts"] == [
        "sudo bash docker/build.sh build {{PACKAGE_NAME}} "
        "{{IMAGE_TAG}}-{{FW_BUILD_TIME}} {{FW_REPO_URL}} "
        "{{FW_BRANCH_NAME}} {{FW_REPO_VERSION}}"
    ]
    artifact = pipeline["artifacts"]["images"][0]
    rendered_artifact = artifact.replace(
        "{{PACKAGE_NAME}}",
        pipeline["variables"]["PACKAGE_NAME"],
    ).replace("{{IMAGE_TAG}}", pipeline["variables"]["IMAGE_TAG"])
    assert rendered_artifact == (
        f"{INTERNAL_IMAGE}:{__version__}-{{{{FW_BUILD_TIME}}}}"
    )


def test_internal_build_uses_aone_metadata_and_internal_sources(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nprintf \'CALL\\n\' >>"$DOCKER_LOG"\nprintf \'%s\\n\' "$@" >>"$DOCKER_LOG"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_LOG": str(docker_log),
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "docker/build.sh"),
            "build",
            PACKAGE_NAME,
            "0.0.5-20260803120000",
            "ssh://git.example/pas.git",
            "feature/managed-pas-core",
            "35875689d6acb553e871c3ce7c3044c63421b058",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert calls[:2] == ["CALL", "build"]
    build_arguments = calls[2:]
    assert build_arguments[-1] == "."
    platform_index = build_arguments.index("--platform")
    assert build_arguments[platform_index + 1] == "linux/amd64"
    internal_dockerfile = f"docker/Dockerfile-{PACKAGE_NAME}"
    assert ["-f", internal_dockerfile] == build_arguments[build_arguments.index("-f") : build_arguments.index("-f") + 2]
    assert f"{INTERNAL_IMAGE}:0.0.5-20260803120000" in build_arguments
    assert "VERSION=0.0.5-20260803120000" in build_arguments
    assert "REVISION=35875689d6acb553e871c3ce7c3044c63421b058" in build_arguments
    assert "GIT_REPO=ssh://git.example/pas.git" in build_arguments
    assert "GIT_BRANCH=feature/managed-pas-core" in build_arguments
    assert "NPM_REGISTRY=https://registry.npmmirror.com" in build_arguments
    assert "PYPI_INDEX_URL=http://yum.tbsite.net/aliyun-pypi/simple/" in build_arguments
    assert "PYPI_EXTRA_INDEX_URL=http://yum.tbsite.net/pypi/simple/" in build_arguments
    assert "PYPI_TRUSTED_HOST=yum.tbsite.net" in build_arguments
    assert "BUILDKIT_INLINE_CACHE=1" in build_arguments


def test_internal_build_uses_only_an_explicit_cache_image(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        '#!/bin/sh\nprintf \'CALL\\n\' >>"$DOCKER_LOG"\nprintf \'%s\\n\' "$@" >>"$DOCKER_LOG"\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    cache_image = f"{INTERNAL_IMAGE}:0.0.5-20260803120000"
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "DOCKER_LOG": str(docker_log),
        "CACHE_FROM_IMAGE": cache_image,
    }

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "docker/build.sh"),
            "build",
            PACKAGE_NAME,
            "0.0.5-20260803130000",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8").splitlines()
    assert calls[:3] == ["CALL", "pull", cache_image]
    assert calls[3:5] == ["CALL", "build"]
    build_arguments = calls[5:]
    cache_index = build_arguments.index("--cache-from")
    assert build_arguments[cache_index + 1] == cache_image


def test_internal_build_rejects_latest_as_cache_image(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/bin/sh\nprintf 'called\\n' >>\"$DOCKER_LOG\"\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "docker/build.sh"),
            "build",
            PACKAGE_NAME,
            "0.0.5-20260803130000",
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DOCKER_LOG": str(docker_log),
            "CACHE_FROM_IMAGE": f"{INTERNAL_IMAGE}:latest",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "cache image must not use the latest tag" in result.stderr
    assert not docker_log.exists()


def test_internal_dockerfile_has_no_docker_hub_dependency() -> None:
    dockerfile = (ROOT / f"docker/Dockerfile-{PACKAGE_NAME}").read_text(encoding="utf-8")
    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]

    assert not dockerfile.startswith("# syntax=")
    assert from_lines == [
        "FROM mirror.aone.alibaba-inc.com/mirrors/library/node:22.22.0-bookworm-slim AS web-builder",
        "FROM mirror.aone.alibaba-inc.com/mirrors/library/python:3.11.14-slim-bookworm AS python-builder",
        "FROM mirror.aone.alibaba-inc.com/mirrors/library/python:3.11.14-slim-bookworm AS runtime",
    ]
    assert "docker.io" not in dockerfile
    assert "--mount=" not in dockerfile


def test_internal_requirements_are_locked_and_public_url_free(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "requirements-internal.txt"
    result = subprocess.run(
        [
            "uv",
            "export",
            "--format",
            "requirements.txt",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--no-header",
            "--output-file",
            str(generated),
        ],
        cwd=ROOT,
        env={**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    committed = (ROOT / "docker/requirements-internal.txt").read_text(encoding="utf-8")
    assert committed == generated.read_text(encoding="utf-8")
    assert "--hash=sha256:" in committed
    assert "pypi.org" not in committed
    assert "pythonhosted.org" not in committed


def test_internal_dockerfile_installs_locked_dependencies_from_internal_indexes() -> None:
    dockerfile = (ROOT / f"docker/Dockerfile-{PACKAGE_NAME}").read_text(encoding="utf-8")

    assert "PIP_DISABLE_PIP_VERSION_CHECK=1" in dockerfile
    assert "UV_CONCURRENT_DOWNLOADS=1" in dockerfile
    requirements_copy = dockerfile.index("COPY docker/requirements-internal.txt ./requirements-internal.txt")
    dependency_sync = dockerfile.index("uv pip sync")
    assert "--require-hashes" in dockerfile
    assert "uv sync" not in dockerfile
    server_copy = dockerfile.index("COPY server ./server")
    project_install = dockerfile.index("uv pip install")
    assert "--no-deps" in dockerfile
    assert "--reinstall" in dockerfile
    assert requirements_copy < dependency_sync < server_copy < project_install


def test_internal_runtime_exposes_truthful_debian_release_to_aone() -> None:
    dockerfile = (ROOT / f"docker/Dockerfile-{PACKAGE_NAME}").read_text(encoding="utf-8")

    assert ". /etc/os-release" in dockerfile
    assert '"${NAME}" "${VERSION_ID}"' in dockerfile
    assert "> /etc/redhat-release" in dockerfile


def test_internal_runtime_includes_basic_diagnostic_tools() -> None:
    dockerfile = (ROOT / f"docker/Dockerfile-{PACKAGE_NAME}").read_text(
        encoding="utf-8"
    )
    runtime = dockerfile.split(" AS runtime", maxsplit=1)[1]

    assert "apt-get install --yes --no-install-recommends" in runtime
    for package in (
        "ca-certificates",
        "curl",
        "inetutils-telnet",
        "iproute2",
        "iputils-ping",
        "netcat-openbsd",
        "procps",
        "vim-tiny",
    ):
        assert package in runtime
    assert "rm -rf /var/lib/apt/lists/*" in runtime
    assert "build-essential" not in runtime


def test_internal_build_rejects_an_unsafe_image_tag(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "bash",
            str(ROOT / "docker/build.sh"),
            "build",
            PACKAGE_NAME,
            "0.0.5;echo-injected",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid image tag" in result.stderr
