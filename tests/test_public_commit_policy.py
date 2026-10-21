from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/release/validate-public-commit.py"
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
VALID_BODY = (
    "Release-Version: v0.0.3\n"
    f"Source-Develop: {SOURCE_SHA}\n"
)


def _load_validator():
    specification = importlib.util.spec_from_file_location(
        "validate_public_commit",
        SCRIPT,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_descriptive_public_commit_is_accepted() -> None:
    validator = _load_validator()

    validator.validate_message(
        "fix: harden resource pool networking and endpoint selection",
        VALID_BODY,
    )


@pytest.mark.parametrize(
    ("subject", "message"),
    [
        ("chore: publish v0.0.3", "allowed Conventional Commit type"),
        ("release: v0.0.3", "behavior description"),
        (
            "chore: port develop updates for open source release",
            "allowed Conventional Commit type",
        ),
        (
            "fix: port develop networking changes",
            "generic public release subject",
        ),
        (
            "docs: document the open source release",
            "generic public release subject",
        ),
        ("Merge branch 'develop'", "merge subjects are not allowed"),
        ("fix: " + "x" * 68, "72 characters"),
        ("fix:", "behavior description"),
    ],
)
def test_public_commit_subject_rejects_generic_or_invalid_messages(
    subject: str,
    message: str,
) -> None:
    validator = _load_validator()

    with pytest.raises(ValueError, match=message):
        validator.validate_message(subject, VALID_BODY)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            f"Source-Develop: {SOURCE_SHA}\n",
            "Release-Version trailer",
        ),
        (
            "Release-Version: v0.0.3\n",
            "Source-Develop trailer",
        ),
        (
            (
                "Release-Version: v0.0.3\n"
                "Source-Develop: 0123456\n"
            ),
            "full lowercase SHA",
        ),
        (
            (
                "Release-Version: 0.0.3\n"
                f"Source-Develop: {SOURCE_SHA}\n"
            ),
            "vMAJOR.MINOR.PATCH",
        ),
        (
            (
                "Release-Version: v0.0.3\n"
                "Release-Version: v0.0.3\n"
                f"Source-Develop: {SOURCE_SHA}\n"
            ),
            "exactly one Release-Version",
        ),
    ],
)
def test_public_commit_requires_exact_release_trailers(
    body: str,
    message: str,
) -> None:
    validator = _load_validator()

    with pytest.raises(ValueError, match=message) as error:
        validator.validate_message("fix: harden networking", body)

    assert body not in str(error.value)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(
    repository: Path,
    filename: str,
    subject: str = "fix: validate public snapshot integration",
    body: str = VALID_BODY,
) -> str:
    (repository / filename).write_text(filename, encoding="utf-8")
    _git(repository, "add", filename)
    _git(repository, "commit", "--quiet", "-m", subject, "-m", body)
    return _git(repository, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "--quiet", "--initial-branch=main")
    _git(tmp_path, "config", "user.name", "Test Maintainer")
    _git(tmp_path, "config", "user.email", "maintainer@example.com")
    _git(tmp_path, "config", "core.hooksPath", "/dev/null")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _commit(tmp_path, "base.txt", "Historical public base", "")
    return tmp_path


def _snapshot(repository: Path, **message: str) -> str:
    _git(repository, "switch", "--quiet", "-c", "snapshot")
    return _commit(repository, "snapshot.txt", **message)


def _merge(
    repository: Path,
    message: str = "Merge pull request #1 from contributor/snapshot",
) -> str:
    _git(repository, "switch", "--quiet", "main")
    _git(repository, "merge", "--quiet", "--no-ff", "snapshot", "-m", message)
    return _git(repository, "rev-parse", "HEAD")


def _validate(repository: Path, revision: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), revision],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_accepts_a_direct_snapshot(repository: Path) -> None:
    snapshot = _snapshot(repository)

    result = _validate(repository, snapshot)

    assert result.returncode == 0, result.stderr


def test_github_merge_validates_the_preserved_snapshot(repository: Path) -> None:
    snapshot = _snapshot(repository)
    merge = _merge(repository)

    result = _validate(repository, merge)

    assert result.returncode == 0, result.stderr
    assert snapshot in result.stdout


@pytest.mark.parametrize(
    ("subject", "body", "error"),
    [
        ("Update dependencies", VALID_BODY, "Conventional Commit"),
        ("fix: validate public snapshots", "", "Release-Version trailer"),
        (
            "fix: validate public snapshots",
            "Release-Version: v0.0.3\n",
            "Source-Develop trailer",
        ),
    ],
)
def test_valid_merge_message_cannot_hide_an_invalid_snapshot(
    repository: Path, subject: str, body: str, error: str,
) -> None:
    _snapshot(repository, subject=subject, body=body)
    merge = _merge(repository, f"fix: integrate public snapshot\n\n{VALID_BODY}")

    result = _validate(repository, merge)

    assert result.returncode == 1
    assert error in result.stderr


def test_merge_does_not_search_older_commits_for_valid_metadata(repository: Path) -> None:
    _snapshot(repository)
    _commit(repository, "later.txt", body="")
    merge = _merge(repository)

    result = _validate(repository, merge)

    assert result.returncode == 1
    assert "Release-Version trailer" in result.stderr


def test_merge_rejects_changes_added_outside_the_snapshot(repository: Path) -> None:
    _snapshot(repository)
    _git(repository, "switch", "--quiet", "main")
    _git(repository, "merge", "--quiet", "--no-ff", "--no-commit", "snapshot")
    merge = _commit(repository, "unreviewed.txt")

    result = _validate(repository, merge)

    assert result.returncode == 1
    assert "merge tree differs from the snapshot" in result.stderr


def test_merge_requires_the_snapshot_to_include_current_main(repository: Path) -> None:
    _snapshot(repository)
    _git(repository, "switch", "--quiet", "main")
    _commit(repository, "concurrent-main.txt")
    merge = _merge(repository, f"fix: integrate public snapshot\n\n{VALID_BODY}")

    result = _validate(repository, merge)

    assert result.returncode == 1
    assert "snapshot must include the first-parent main commit" in result.stderr


def test_octopus_merge_is_not_a_single_snapshot(repository: Path) -> None:
    _snapshot(repository)
    _git(repository, "switch", "--quiet", "main")
    _git(repository, "switch", "--quiet", "-c", "another")
    _commit(repository, "another.txt")
    _git(repository, "switch", "--quiet", "main")
    _git(
        repository, "merge", "--quiet", "--no-ff", "snapshot", "another",
        "-m", f"fix: integrate public snapshots\n\n{VALID_BODY}",
    )

    result = _validate(repository, "HEAD")

    assert result.returncode == 1
    assert "exactly two parents" in result.stderr


def test_nested_merge_is_not_used_as_snapshot_metadata(repository: Path) -> None:
    _snapshot(repository)
    _git(repository, "switch", "--quiet", "-c", "side", "main")
    _commit(repository, "side.txt")
    _git(repository, "switch", "--quiet", "snapshot")
    _git(
        repository, "merge", "--quiet", "--no-ff", "side",
        "-m", f"fix: merge branch content\n\n{VALID_BODY}",
    )
    merge = _merge(repository, f"fix: integrate public snapshot\n\n{VALID_BODY}")

    result = _validate(repository, merge)

    assert result.returncode == 1
    assert "snapshot must not itself be a merge" in result.stderr


def test_missing_merge_history_fails_closed(repository: Path, tmp_path: Path) -> None:
    _snapshot(repository)
    _merge(repository)
    shallow = tmp_path / "shallow"
    _git(
        repository, "clone", "--quiet", "--depth=1", "--no-local",
        repository.as_uri(), str(shallow),
    )

    result = _validate(shallow, "HEAD")

    assert result.returncode == 1
    assert "full history" in result.stderr
