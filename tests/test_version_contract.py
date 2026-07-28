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

    assert project["project"]["version"] == "0.0.2"
    assert web["version"] == "0.0.2"
    assert web_lock["version"] == "0.0.2"
    assert web_lock["packages"][""]["version"] == "0.0.2"
    assert create_app().version == "0.0.2"
