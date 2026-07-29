from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
VALID_BODY = (
    "Release-Version: v0.0.3\n"
    f"Source-Develop: {SOURCE_SHA}\n"
)


def _load_validator():
    path = ROOT / "scripts/release/validate-public-commit.py"
    specification = importlib.util.spec_from_file_location(
        "validate_public_commit",
        path,
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
