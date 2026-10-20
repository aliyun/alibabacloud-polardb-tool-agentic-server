"""Native CLI companion to candidate-image acceptance; no container claim is made."""
import os
import secrets
import stat
import sys

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.test_env_generator_mysql import _run_interactive_generator
from scripts.dev.run_local_regression import owned_database_name


@pytest.mark.integration
async def test_native_cli_generates_verified_mysql_environment_without_echoing_password(tmp_path):
    url = os.environ.get("PAS_TEST_MYSQL_URL", "")
    if not url or os.environ.get("PAS_TEST_MYSQL_DATABASE_OK") != "1":
        pytest.skip("A disposable local MySQL database is required")
    parsed = make_url(url)
    if parsed.host != "127.0.0.1" or not (parsed.database or "").startswith("pas_local_reg_"):
        pytest.skip("Native CLI acceptance requires the private local regression runner")
    owned_database_name(parsed.database)
    username = "pas_env_" + secrets.token_hex(8)
    password = "test-$@:/?#% space-" + secrets.token_hex(8)
    engine = create_async_engine(url)
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE USER :name@'localhost' IDENTIFIED BY :password"), {"name": username, "password": password})
            created = True
            await connection.execute(text(f"GRANT ALL PRIVILEGES ON `{parsed.database}`.* TO :name@'localhost'"), {"name": username})
        output = tmp_path / "generated.env"
        returncode, terminal = _run_interactive_generator(
            [sys.executable, "-m", "server.cli", "database", "create-env", "--output", str(output)],
            [(b"Metadata database endpoint: ", "127.0.0.1"),
             (b"Metadata database port [3306]: ", str(parsed.port)),
             (b"Metadata database name: ", parsed.database), (b"Metadata database username: ", username),
             (b"Use host.docker.internal instead? [Y/n]: ", "n"),
             (b"Use these settings? [Y/n]: ", "y"), (b"Metadata database password: ", password)], password)
        assert returncode == 0, terminal
        assert "connection test skipped" not in terminal.lower()
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        generated = make_url(values["PAS_DATABASE_URL"].strip("'"))
        assert generated.password == password
        assert generated.host == "127.0.0.1" and generated.database == parsed.database
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(text("DROP USER :name@'localhost'"), {"name": username})
        finally:
            await engine.dispose()
