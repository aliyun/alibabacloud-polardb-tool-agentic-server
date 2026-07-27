from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dependency_license_report_is_current_and_allowlisted() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/security/generate-license-report.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    report = json.loads((ROOT / "THIRD_PARTY_LICENSES.json").read_text(encoding="utf-8"))
    allowed = {
        line.strip()
        for line in (ROOT / ".github/allowed-licenses.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }

    assert report["schema_version"] == 1
    assert report["dependencies"] == sorted(
        report["dependencies"],
        key=lambda dependency: (
            dependency["ecosystem"],
            dependency["name"].lower(),
            dependency["version"],
        ),
    )
    assert {dependency["ecosystem"] for dependency in report["dependencies"]} == {
        "npm",
        "python",
    }
    assert all(dependency["license"] in allowed for dependency in report["dependencies"])
    assert not any(dependency["license"] in {"", "UNKNOWN", "UNLICENSED"} for dependency in report["dependencies"])
