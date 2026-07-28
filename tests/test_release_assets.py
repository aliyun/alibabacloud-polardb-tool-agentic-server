from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ASSETS = {
    "polardb-agentic-server-0.0.2-chart.tgz",
    "polardb-agentic-server-0.0.2-deploy.tar.gz",
    "polardb-agentic-server-0.0.2-image-linux-amd64.tar.gz",
    "polardb-agentic-server-0.0.2-image-linux-arm64.tar.gz",
    "polardb-agentic-server-0.0.2.spdx.json",
    "SHA256SUMS",
}


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_release_version_matches_all_versioned_components() -> None:
    result = _run("scripts/release/verify-version.py", "v0.0.2")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.0.2"


def test_release_version_rejects_invalid_or_mismatched_tag() -> None:
    invalid = _run("scripts/release/verify-version.py", "0.0.2")
    mismatch = _run("scripts/release/verify-version.py", "v0.0.1")

    assert invalid.returncode != 0
    assert mismatch.returncode != 0


def test_expected_release_asset_names_are_exact() -> None:
    result = _run("scripts/release/check-assets.py", "names", "--version", "0.0.2")

    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) == EXPECTED_ASSETS


def test_deployment_bundle_is_deterministic_and_allowlisted(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for output in (first, second):
        result = subprocess.run(
            ["scripts/release/build-deployment-bundle.sh", "0.0.2", str(output)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    for name in (
        "polardb-agentic-server-0.0.2-chart.tgz",
        "polardb-agentic-server-0.0.2-deploy.tar.gz",
    ):
        assert hashlib.sha256((first / name).read_bytes()).digest() == hashlib.sha256(
            (second / name).read_bytes()
        ).digest()

    bundle = first / "polardb-agentic-server-0.0.2-deploy.tar.gz"
    with tarfile.open(bundle, "r:gz") as archive:
        members = archive.getmembers()
    assert members
    assert all(not member.name.startswith("/") and ".." not in Path(member.name).parts for member in members)
    assert all(member.mtime == 0 for member in members)
    assert {Path(member.name).parts[0] for member in members} == {
        "polardb-agentic-server-0.0.2"
    }
    assert not any("secret" in member.name.lower() for member in members)


def test_image_archive_builder_rejects_mutable_reference(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            "scripts/release/save-image-archive.sh",
            "--image",
            "ghcr.io/aliyun/example:0.0.1",
            "--platform",
            "linux/amd64",
            "--version",
            "0.0.1",
            "--revision",
            "abc123",
            "--output",
            str(tmp_path / "image.tar.gz"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "digest" in result.stderr.lower()
