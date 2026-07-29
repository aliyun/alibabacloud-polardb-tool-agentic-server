from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / "scripts/public-release/export.sh"
REHEARSE = ROOT / "scripts/public-release/rehearse.sh"
AUDIT = ROOT / "scripts/public-release/audit-refs.sh"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fixture_repo(tmp_path: Path, *, secret: bool = False) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _write(source / ".public-release-allowlist", "README.md\nserver/\nscripts/\n")
    _write(source / "README.md", "# Public\n")
    _write(source / "server/app.py", "TOKEN = 'placeholder'\n")
    _write(source / "scripts/run.sh", "#!/bin/sh\nexit 0\n")
    _write(
        source / "pyproject.toml",
        '[project]\nname = "fixture"\nversion = "1.2.3"\n',
    )
    _write(
        source / "web/package.json",
        '{"name":"fixture","version":"1.2.3"}\n',
    )
    _write(
        source / "web/package-lock.json",
        '{"name":"fixture","version":"1.2.3","packages":{"":{"version":"1.2.3"}}}\n',
    )
    _write(
        source / "deploy/helm/polardb-agentic-server/Chart.yaml",
        'apiVersion: v2\nname: fixture\nversion: 1.2.3\nappVersion: "1.2.3"\n',
    )
    _write(source / ("docs/" + "superpowers/spec.md"), "internal\n")
    _write(source / "benign-unlisted.txt", "not selected\n")
    if secret:
        _write(source / "server/secret.py", "KEY = '" + "LTAI" + "1234567890ABCDEF'\n")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    return source


def _run_export(source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(EXPORT), "--source", str(source), "--output", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_allowlist_export_excludes_unlisted_and_internal_files(tmp_path: Path) -> None:
    source = _fixture_repo(tmp_path)
    destination = tmp_path / "public"

    result = _run_export(source, destination)

    assert result.returncode == 0, result.stderr
    assert (destination / "server/app.py").exists()
    assert (destination / "README.md").exists()
    assert not (destination / ("docs/" + "superpowers/spec.md")).exists()
    assert not (destination / "benign-unlisted.txt").exists()
    assert not (destination / ".git").exists()
    assert (destination / "scripts/run.sh").stat().st_mode & 0o777 == 0o755
    assert (destination / "server/app.py").stat().st_mode & 0o777 == 0o644


def test_export_manifest_is_sorted_and_matches_files(tmp_path: Path) -> None:
    source = _fixture_repo(tmp_path)
    destination = tmp_path / "public"
    result = _run_export(source, destination)
    assert result.returncode == 0, result.stderr

    lines = (destination / ".public-release-manifest.sha256").read_text(encoding="utf-8").splitlines()
    assert lines == sorted(lines, key=lambda line: line.split("  ", maxsplit=1)[1])
    for line in lines:
        digest, relative = line.split("  ", maxsplit=1)
        assert hashlib.sha256((destination / relative).read_bytes()).hexdigest() == digest


def test_export_rejects_secret_bearing_allowlisted_content(tmp_path: Path) -> None:
    source = _fixture_repo(tmp_path, secret=True)
    destination = tmp_path / "public"

    result = _run_export(source, destination)

    assert result.returncode != 0
    assert "SECRET_ALIBABA_ACCESS_KEY" in result.stderr
    assert ("LTAI" + "1234567890ABCDEF") not in result.stderr


def test_export_rejects_relative_or_existing_output(tmp_path: Path) -> None:
    source = _fixture_repo(tmp_path)
    existing = tmp_path / "existing"
    existing.mkdir()

    relative = subprocess.run(
        [str(EXPORT), "--source", str(source), "--output", "relative"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    existing_result = _run_export(source, existing)

    assert relative.returncode != 0
    assert existing_result.returncode != 0


def test_export_rejects_symlink_that_escapes_source(tmp_path: Path) -> None:
    source = _fixture_repo(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("safe-looking but outside\n", encoding="utf-8")
    (source / "server/outside.py").symlink_to(outside)
    subprocess.run(["git", "add", "server/outside.py"], cwd=source, check=True)

    result = _run_export(source, tmp_path / "public")

    assert result.returncode != 0
    assert "symlink escapes source" in result.stderr


def test_rehearsal_creates_one_root_commit_without_mutating_source_refs(tmp_path: Path) -> None:
    source = _fixture_repo(tmp_path)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
        cwd=source,
        check=True,
    )
    before = subprocess.run(["git", "show-ref"], cwd=source, capture_output=True, text=True, check=True).stdout

    result = subprocess.run(
        [
            str(REHEARSE),
            "--source",
            str(source),
            "--version",
            "1.2.3",
            "--report",
            str(tmp_path / "audit.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ["PATH"],
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )
    after = subprocess.run(["git", "show-ref"], cwd=source, capture_output=True, text=True, check=True).stdout

    assert result.returncode == 0, result.stderr
    assert before == after
    assert "one-root-commit: ok" in result.stdout
    report = (tmp_path / "audit.json").read_text(encoding="utf-8")
    assert '"tag": "v1.2.3"' in report


def test_rehearsal_rejects_malformed_version(tmp_path: Path) -> None:
    source = _fixture_repo(tmp_path)

    result = subprocess.run(
        [
            str(REHEARSE),
            "--source",
            str(source),
            "--version",
            "1..2",
            "--report",
            str(tmp_path / "audit.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--version must use MAJOR.MINOR.PATCH" in result.stderr


def _audit_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "candidate"
    repository.mkdir(parents=True)
    _write(repository / "README.md", "# Public candidate\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "snapshot"],
        cwd=repository,
        check=True,
    )
    subprocess.run(["git", "tag", "v0.0.1"], cwd=repository, check=True)
    return repository


def _run_audit(repository: Path, report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            str(AUDIT),
            "--repo",
            str(repository),
            "--tag",
            "v0.0.1",
            "--report",
            str(report),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_ref_audit_accepts_one_clean_orphan_snapshot(tmp_path: Path) -> None:
    repository = _audit_repo(tmp_path)

    result = _run_audit(repository, tmp_path / "audit.json")

    assert result.returncode == 0, result.stderr
    assert '"status": "passed"' in (tmp_path / "audit.json").read_text(encoding="utf-8")


def test_ref_audit_rejects_extra_branch_or_tag(tmp_path: Path) -> None:
    repository = _audit_repo(tmp_path)
    subprocess.run(["git", "branch", "develop"], cwd=repository, check=True)
    branch_result = _run_audit(repository, tmp_path / "branch.json")
    subprocess.run(["git", "branch", "-D", "develop"], cwd=repository, check=True)
    subprocess.run(["git", "tag", "internal-test"], cwd=repository, check=True)
    tag_result = _run_audit(repository, tmp_path / "tag.json")

    assert branch_result.returncode != 0
    assert tag_result.returncode != 0


def test_ref_audit_rejects_parent_history_internal_file_and_secret(tmp_path: Path) -> None:
    history_repo = _audit_repo(tmp_path / "history")
    _write(history_repo / "CHANGELOG.md", "second commit\n")
    subprocess.run(["git", "add", "."], cwd=history_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "second"],
        cwd=history_repo,
        check=True,
    )
    subprocess.run(["git", "tag", "-f", "v0.0.1"], cwd=history_repo, check=True)

    internal_repo = _audit_repo(tmp_path / "internal")
    _write(internal_repo / "PRODUCT.md", "internal\n")
    subprocess.run(["git", "add", "."], cwd=internal_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--amend", "--no-edit", "-q"],
        cwd=internal_repo,
        check=True,
    )
    subprocess.run(["git", "tag", "-f", "v0.0.1"], cwd=internal_repo, check=True)

    secret_repo = _audit_repo(tmp_path / "secret")
    _write(secret_repo / "config.txt", "key=" + "LTAI" + "1234567890ABCDEF\n")
    subprocess.run(["git", "add", "."], cwd=secret_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--amend", "--no-edit", "-q"],
        cwd=secret_repo,
        check=True,
    )
    subprocess.run(["git", "tag", "-f", "v0.0.1"], cwd=secret_repo, check=True)

    assert _run_audit(history_repo, tmp_path / "history.json").returncode != 0
    assert _run_audit(internal_repo, tmp_path / "internal.json").returncode != 0
    secret_result = _run_audit(secret_repo, tmp_path / "secret.json")
    assert secret_result.returncode != 0
    assert ("LTAI" + "1234567890ABCDEF") not in secret_result.stderr
