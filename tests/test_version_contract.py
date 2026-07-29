from __future__ import annotations

import json
import tomllib
from pathlib import Path

from server.app import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_all_runtime_version_sources_match_current_public_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    web = json.loads((ROOT / "web/package.json").read_text())
    web_lock = json.loads((ROOT / "web/package-lock.json").read_text())

    current = project["project"]["version"]

    assert web["version"] == current
    assert web_lock["version"] == current
    assert web_lock["packages"][""]["version"] == current
    assert create_app().version == current
