from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_selector():
    path = ROOT / "scripts/release/select-latest-release.py"
    specification = importlib.util.spec_from_file_location(
        "select_latest_release",
        path,
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_highest_version_uses_numeric_semver_ordering() -> None:
    selector = _load_selector()

    assert selector.highest_version(
        ["v0.0.2", "v0.0.10", "v0.1.0"]
    ) == "v0.1.0"


def test_candidate_must_be_highest_published_release() -> None:
    selector = _load_selector()
    releases = [
        {
            "tag_name": "v0.0.2",
            "draft": False,
            "published_at": "2026-07-28T01:00:00Z",
        },
        {
            "tag_name": "v0.0.10",
            "draft": False,
            "published_at": "2026-07-28T02:00:00Z",
        },
    ]

    with pytest.raises(
        ValueError,
        match="latest promotion would move backward",
    ):
        selector.validate_candidate("v0.0.2", releases)
    assert selector.validate_candidate("v0.0.10", releases) == "v0.0.10"


@pytest.mark.parametrize(
    ("candidate", "releases", "message"),
    [
        (
            "v0.0.2",
            [
                {
                    "tag_name": "v0.0.2",
                    "draft": True,
                    "published_at": None,
                }
            ],
            "not a published Release",
        ),
        (
            "v0.0.2",
            [
                {
                    "tag_name": "release-2",
                    "draft": False,
                    "published_at": "2026-07-28T01:00:00Z",
                }
            ],
            "malformed release tag",
        ),
        (
            "v0.0.2",
            [
                {
                    "tag_name": "v0.0.2",
                    "draft": False,
                    "published_at": "2026-07-28T01:00:00Z",
                },
                {
                    "tag_name": "v0.0.2",
                    "draft": False,
                    "published_at": "2026-07-28T02:00:00Z",
                },
            ],
            "duplicate semantic version",
        ),
    ],
)
def test_latest_selector_rejects_ambiguous_release_state(
    candidate: str,
    releases: list[dict[str, object]],
    message: str,
) -> None:
    selector = _load_selector()

    with pytest.raises(ValueError, match=message):
        selector.validate_candidate(candidate, releases)
