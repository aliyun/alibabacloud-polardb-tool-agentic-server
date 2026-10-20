"""The real-service browser runner must never reach cloud DNS or TCP."""

import runpy
import subprocess
import sys
from pathlib import Path

import pytest


RUNNER = Path(__file__).resolve().parents[1] / "web/e2e/pas_runner.py"


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_browser_runner_allows_loopback(host):
    runpy.run_path(str(RUNNER))["require_loopback"](host)


@pytest.mark.parametrize(
    "operation",
    [
        "socket.getaddrinfo('polardb-vpc.cn-shenzhen.aliyuncs.com', 443)",
        "socket.gethostbyname('polardb.aliyuncs.com')",
        "socket.socket().connect(('100.100.100.200', 80))",
        "socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'', ('8.8.8.8', 53))",
    ],
)
def test_browser_runner_blocks_external_network_before_io(operation):
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import runpy, socket, sys; "
            "sys.addaudithook(runpy.run_path(sys.argv[1])['network_guard']); "
            + operation,
            str(RUNNER),
        ],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode != 0
    assert "ConnectionRefusedError: Browser E2E denies non-loopback networking" in result.stderr


@pytest.mark.parametrize("url", ["mysql+asyncmy://127.0.0.1/existing", "sqlite+aiosqlite:////tmp/existing.db"])
def test_browser_seed_rejects_existing_or_remote_database(tmp_path, url):
    seed = runpy.run_path(str(RUNNER.with_name("seed_layout.py")))
    with pytest.raises(ValueError, match="owned"):
        seed["owned_database_url"]({"PAS_DATABASE_URL": url, "PAS_E2E_OWNED_DIRECTORY": str(tmp_path / "pas-browser-e2e-private")})
