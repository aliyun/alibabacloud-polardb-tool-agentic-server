from __future__ import annotations

import errno
import os
import pty
import re
import select
import stat
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[2]
ENABLED = (
    os.environ.get("PAS_RUN_ENV_GENERATOR_MYSQL_TEST") == "1"
    and bool(os.environ.get("PAS_TEST_IMAGE"))
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not ENABLED,
        reason=(
            "set PAS_RUN_ENV_GENERATOR_MYSQL_TEST=1 and PAS_TEST_IMAGE "
            "to run the environment generator MySQL test"
        ),
    ),
]


def _pinned_mysql_image() -> str:
    match = re.search(
        r"(?m)^\s+image: \$\{MYSQL_IMAGE:-(mysql:[^}]+)\}$",
        (ROOT / "compose.yaml").read_text(encoding="utf-8"),
    )
    assert match is not None
    return match.group(1)


def _scrub(value: str, password: str) -> str:
    return value.replace(password, "<redacted>")


def _run_interactive_generator(
    command: list[str],
    responses: list[tuple[bytes, str]],
    password: str,
) -> tuple[int, str]:
    master, slave = pty.openpty()
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    output = bytearray()
    response_index = 0
    deadline = time.monotonic() + 60
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                raise AssertionError(
                    "environment generator exceeded its 60-second deadline"
                )
            readable, _, _ = select.select(
                [master],
                [],
                [],
                min(remaining, 1),
            )
            if readable:
                try:
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.extend(chunk)
                if response_index < len(responses):
                    prompt, response = responses[response_index]
                    if prompt in output:
                        os.write(master, response.encode() + b"\n")
                        response_index += 1
            elif process.poll() is not None:
                break
    except BaseException:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        raise
    finally:
        os.close(master)
    returncode = process.wait(timeout=5)
    rendered = output.decode(errors="replace")
    if password in rendered:
        pytest.fail(
            "the interactive terminal echoed the database password",
            pytrace=False,
        )
    assert response_index == len(responses), _scrub(rendered, password)
    return returncode, _scrub(rendered, password)


@pytest.mark.integration
def test_candidate_image_generates_verified_mysql_environment(
    tmp_path: Path,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    network = f"pas-env-network-{suffix}"
    mysql_container = f"pas-env-mysql-{suffix}"
    pas_container = f"pas-env-generator-{suffix}"
    password = f"test-$@:/?#% space-{suffix}"
    image = os.environ["PAS_TEST_IMAGE"]
    output = tmp_path / "generated.env"
    created_network = False
    created_container = False

    try:
        network_result = subprocess.run(
            ["docker", "network", "create", network],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert network_result.returncode == 0, network_result.stderr
        created_network = True

        mysql_result = subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                mysql_container,
                "--network",
                network,
                "--env",
                f"MYSQL_ROOT_PASSWORD={password}",
                "--env",
                "MYSQL_DATABASE=pas",
                "--env",
                "MYSQL_USER=pas",
                "--env",
                f"MYSQL_PASSWORD={password}",
                _pinned_mysql_image(),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if mysql_result.returncode != 0:
            pytest.fail(
                _scrub(mysql_result.stderr, password),
                pytrace=False,
            )
        created_container = True

        deadline = time.monotonic() + 60
        last_ping_error = ""
        while time.monotonic() < deadline:
            ping = subprocess.run(
                [
                    "docker",
                    "exec",
                    mysql_container,
                    "mysqladmin",
                    "ping",
                    "--host=127.0.0.1",
                    "--silent",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            if ping.returncode == 0:
                break
            last_ping_error = ping.stderr
            time.sleep(1)
        else:
            raise AssertionError(
                "MySQL did not become ready within 60 seconds: "
                + _scrub(last_ping_error, password)
            )

        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            pas_container,
            "--interactive",
            "--tty",
            "--network",
            network,
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--mount",
            f"type=bind,src={tmp_path},dst=/output",
            "--entrypoint",
            "pas",
            image,
            "database",
            "create-env",
            "--output",
            "/output/generated.env",
        ]
        returncode, generator_output = _run_interactive_generator(
            command,
            [
                (b"Metadata database endpoint: ", mysql_container),
                (b"Metadata database port [3306]: ", ""),
                (b"Metadata database name: ", "pas"),
                (b"Metadata database username: ", "pas"),
                (b"Use these settings? [Y/n]: ", "y"),
                (b"Metadata database password: ", password),
            ],
            password,
        )

        assert returncode == 0, generator_output
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        values = dict(
            line.split("=", 1)
            for line in output.read_text(encoding="utf-8").splitlines()
        )
        database_url = values["PAS_DATABASE_URL"].strip("'")
        parsed = make_url(database_url)
        assert parsed.drivername == "mysql+asyncmy"
        if parsed.password != password:
            pytest.fail(
                "the database password did not round-trip through the URL",
                pytrace=False,
            )
    finally:
        subprocess.run(
            ["docker", "rm", "--force", pas_container],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
        if created_container:
            subprocess.run(
                ["docker", "rm", "--force", mysql_container],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
        if created_network:
            subprocess.run(
                ["docker", "network", "rm", network],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
