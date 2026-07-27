from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.integration
def test_kind_release_lifecycle() -> None:
    if os.environ.get("PAS_RUN_KUBERNETES_INTEGRATION") != "1":
        pytest.skip(
            "set PAS_RUN_KUBERNETES_INTEGRATION=1 to run the kind release test"
        )
    image = os.environ["PAS_TEST_IMAGE"]
    mysql_image = os.environ["PAS_TEST_MYSQL_IMAGE"]
    kind_image = os.environ.get("PAS_TEST_KIND_IMAGE")

    command = [
        str(ROOT / "scripts/deploy/smoke-helm.sh"),
        "--image",
        image,
        "--mysql-image",
        mysql_image,
    ]
    if kind_image:
        command.extend(["--kind-image", kind_image])

    result = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        check=False,
    )

    assert result.returncode == 0
