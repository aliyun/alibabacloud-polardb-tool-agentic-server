from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CURRENT_VERSION = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
major, minor, patch = (int(part) for part in CURRENT_VERSION.split("."))
NEXT_VERSION = f"{major}.{minor}.{patch + 1}"
CURRENT_RELEASE_REFERENCE_PATHS = (
    ".agents/skills/deploy-polardb-agentic-server/SKILL.md",
    ".claude/skills/deploy-polardb-agentic-server/SKILL.md",
    "README.md",
    "README_zh-CN.md",
    "docs/en/README.md",
    "docs/zh-cn/README.md",
    "docs/en/deployment/agent-assisted-deployment.md",
    "docs/zh-cn/deployment/agent-assisted-deployment.md",
    "docs/en/getting-started/deploy-compose.md",
    "docs/zh-cn/getting-started/deploy-compose.md",
)
VERSION_PATHS = (
    ".agents/skills/deploy-polardb-agentic-server/scripts/deploy-docker.sh",
    ".agents/skills/deploy-polardb-agentic-server/scripts/deploy-source.sh",
    ".claude/skills/deploy-polardb-agentic-server/scripts/deploy-docker.sh",
    ".claude/skills/deploy-polardb-agentic-server/scripts/deploy-source.sh",
    ".agents/skills/deploy-polardb-agentic-server/SKILL.md",
    ".claude/skills/deploy-polardb-agentic-server/SKILL.md",
    "README.md",
    "README_zh-CN.md",
    "docs/en/README.md",
    "docs/zh-cn/README.md",
    ".env.compose.example",
    "Dockerfile",
    "pyproject.toml",
    "server/version.py",
    "uv.lock",
    "web/package.json",
    "web/package-lock.json",
    "compose.yaml",
    "deploy/compose/compose.external-mysql.yaml",
    "deploy/compose/compose.external-postgres.yaml",
    "deploy/helm/polardb-agentic-server/Chart.yaml",
    "deploy/helm/polardb-agentic-server/values.yaml",
    "docs/en/deployment/agent-assisted-deployment.md",
    "scripts/deploy/create-external-mysql-env.sh",
    "scripts/public-release/rehearse.sh",
    "docs/en/deployment/kubernetes-helm.md",
    "docs/zh-cn/deployment/kubernetes-helm.md",
    "docs/zh-cn/deployment/agent-assisted-deployment.md",
    "docs/en/deployment/offline-installation.md",
    "docs/zh-cn/deployment/offline-installation.md",
    "docs/en/deployment/upgrade-and-rollback.md",
    "docs/zh-cn/deployment/upgrade-and-rollback.md",
    "docs/en/getting-started/deploy-compose.md",
    "docs/zh-cn/getting-started/deploy-compose.md",
    "docs/en/deployment/prerequisites.md",
    "docs/zh-cn/deployment/prerequisites.md",
    "tests/test_release_assets.py",
)


def _copy_version_tree(tmp_path: Path) -> Path:
    destination = tmp_path / "source"
    for relative in VERSION_PATHS:
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (destination / "release-notes.md").write_text(
        "Historical releases v0.0.1 and v0.0.2 remain immutable.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=destination, check=True)
    subprocess.run(["git", "add", "."], cwd=destination, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=destination,
        check=True,
    )
    return destination


def _run(root: Path, script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / script), *arguments, "--root", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_verify_version_rejects_compose_drift(tmp_path: Path) -> None:
    source = _copy_version_tree(tmp_path)
    compose = source / "compose.yaml"
    compose.write_text(
        compose.read_text(encoding="utf-8").replace(
            f":{CURRENT_VERSION}}}",
            ":9.9.9}",
        ),
        encoding="utf-8",
    )

    result = _run(
        source,
        "scripts/release/verify-version.py",
        f"v{CURRENT_VERSION}",
    )

    assert result.returncode != 0
    assert "Compose" in result.stderr


def test_verify_version_rejects_python_lock_drift(tmp_path: Path) -> None:
    source = _copy_version_tree(tmp_path)
    lock = source / "uv.lock"
    marker = (
        'name = "alibabacloud-polardb-tool-agentic-server"\n'
        f'version = "{CURRENT_VERSION}"'
    )
    lock.write_text(
        lock.read_text(encoding="utf-8").replace(
            marker,
            (
                'name = "alibabacloud-polardb-tool-agentic-server"\n'
                'version = "9.9.9"'
            ),
        ),
        encoding="utf-8",
    )

    result = _run(
        source,
        "scripts/release/verify-version.py",
        f"v{CURRENT_VERSION}",
    )

    assert result.returncode != 0
    assert "Python lock" in result.stderr


def test_verify_version_rejects_environment_generator_drift(
    tmp_path: Path,
) -> None:
    source = _copy_version_tree(tmp_path)
    launcher = source / "scripts/deploy/create-external-mysql-env.sh"
    launcher.write_text(
        launcher.read_text(encoding="utf-8").replace(
            f":{CURRENT_VERSION}\n",
            ":9.9.9\n",
        ),
        encoding="utf-8",
    )

    result = _run(
        source,
        "scripts/release/verify-version.py",
        f"v{CURRENT_VERSION}",
    )

    assert result.returncode != 0
    assert "Compose environment generator" in result.stderr


def test_bump_version_updates_release_locations_only(tmp_path: Path) -> None:
    source = _copy_version_tree(tmp_path)

    result = _run(
        source,
        "scripts/release/bump-version.py",
        NEXT_VERSION,
    )

    assert result.returncode == 0, result.stderr
    verified = _run(
        source,
        "scripts/release/verify-version.py",
        f"v{NEXT_VERSION}",
    )
    assert verified.returncode == 0, verified.stderr
    assert (
        source / "scripts/deploy/create-external-mysql-env.sh"
    ).read_text(encoding="utf-8").count(
        "DEFAULT_PAS_IMAGE=ghcr.io/aliyun/"
        f"alibabacloud-polardb-tool-agentic-server:{NEXT_VERSION}"
    ) == 1
    for relative in VERSION_PATHS[:4]:
        assert (
            source / relative
        ).read_text(encoding="utf-8").count(
            f'PAS_VERSION="${{PAS_VERSION:-{NEXT_VERSION}}}"'
        ) == 1
    assert (source / "release-notes.md").read_text(encoding="utf-8") == (
        "Historical releases v0.0.1 and v0.0.2 remain immutable.\n"
    )
    expected_occurrences = {
        "docs/en/deployment/agent-assisted-deployment.md": 1,
        "docs/en/deployment/kubernetes-helm.md": 2,
        "docs/zh-cn/deployment/kubernetes-helm.md": 2,
        "docs/en/deployment/offline-installation.md": 1,
        "docs/zh-cn/deployment/offline-installation.md": 1,
        "docs/en/deployment/upgrade-and-rollback.md": 1,
        "docs/zh-cn/deployment/upgrade-and-rollback.md": 1,
        "docs/zh-cn/deployment/agent-assisted-deployment.md": 1,
    }
    for relative, count in expected_occurrences.items():
        content = (source / relative).read_text(encoding="utf-8")
        assert content.count(f"PAS_VERSION={NEXT_VERSION}") == count


def test_bump_version_updates_current_release_prose(tmp_path: Path) -> None:
    source = _copy_version_tree(tmp_path)

    result = _run(
        source,
        "scripts/release/bump-version.py",
        NEXT_VERSION,
    )

    assert result.returncode == 0, result.stderr
    for relative in CURRENT_RELEASE_REFERENCE_PATHS:
        content = (source / relative).read_text(encoding="utf-8")
        assert NEXT_VERSION in content
        assert CURRENT_VERSION not in content


def test_bump_version_rejects_dirty_tracked_worktree(
    tmp_path: Path,
) -> None:
    source = _copy_version_tree(tmp_path)
    (source / "release-notes.md").write_text(
        "dirty\n",
        encoding="utf-8",
    )

    result = _run(
        source,
        "scripts/release/bump-version.py",
        NEXT_VERSION,
    )

    assert result.returncode != 0
    assert "worktree must be clean" in result.stderr
