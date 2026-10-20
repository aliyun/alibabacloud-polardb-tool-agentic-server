"""Use the real Compose parser without requiring a running Docker engine."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def compose_command() -> list[str]:
    explicit = os.environ.get("PAS_TEST_COMPOSE_BINARY")
    if explicit:
        path = Path(explicit)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            pytest.fail("PAS_TEST_COMPOSE_BINARY must name an absolute executable file")
        candidates = [[str(path)]]
    else:
        docker, standalone = shutil.which("docker"), shutil.which("docker-compose")
        candidates = ([[docker, "compose"]] if docker else []) + ([[standalone]] if standalone else [])
    for command in candidates:
        if subprocess.run([*command, "version"], capture_output=True, check=False).returncode == 0:
            return command
    if explicit:
        pytest.fail("The explicit Compose parser could not run")
    pytest.skip("Docker Compose plugin or standalone parser is unavailable")
