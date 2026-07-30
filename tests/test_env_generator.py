from __future__ import annotations

import base64
import io
import os
import pty
import select
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.engine import make_url

from server.deployment import env_generator
from server.deployment.env_generator import (
    EnvironmentGenerationError,
    MySQLConnectionInput,
    build_mysql_url,
    check_mysql_connection,
    create_environment_file,
)


ROOT = Path(__file__).resolve().parents[1]


def _connection_input(**overrides: object) -> MySQLConnectionInput:
    values: dict[str, object] = {
        "endpoint": "db.example.test",
        "port": 3306,
        "database": "pas",
        "username": "pas@example",
        "password": "$@:/?#% space'\"\\密码",
    }
    values.update(overrides)
    return MySQLConnectionInput(**values)  # type: ignore[arg-type]


def test_mysql_url_round_trips_special_password_characters() -> None:
    value = _connection_input()

    url = build_mysql_url(value)
    rendered = url.render_as_string(hide_password=False)
    parsed = make_url(rendered)

    assert url.drivername == "mysql+asyncmy"
    assert parsed.username == value.username
    assert parsed.password == value.password
    assert rendered != value.password


@pytest.mark.parametrize(
    ("overrides", "code"),
    (
        ({"endpoint": ""}, "DATABASE_ENDPOINT_INVALID"),
        ({"endpoint": "host\nname"}, "DATABASE_ENDPOINT_INVALID"),
        ({"endpoint": "host\0name"}, "DATABASE_ENDPOINT_INVALID"),
        ({"port": 0}, "DATABASE_PORT_INVALID"),
        ({"port": 65536}, "DATABASE_PORT_INVALID"),
        ({"database": ""}, "DATABASE_NAME_INVALID"),
        ({"database": "pas\rdb"}, "DATABASE_NAME_INVALID"),
        ({"username": ""}, "DATABASE_USERNAME_INVALID"),
        ({"username": "pas\nuser"}, "DATABASE_USERNAME_INVALID"),
        ({"password": ""}, "DATABASE_PASSWORD_INVALID"),
    ),
)
def test_mysql_url_rejects_invalid_non_secret_fields(
    overrides: dict[str, object],
    code: str,
) -> None:
    sentinel = "must-not-appear"
    overrides = {"password": sentinel, **overrides}

    with pytest.raises(EnvironmentGenerationError) as captured:
        build_mysql_url(_connection_input(**overrides))

    assert captured.value.code == code
    assert sentinel not in str(captured.value)
    assert captured.value.__cause__ is None


class _ConnectionContext:
    def __init__(self, connection: object) -> None:
        self.connection = connection

    async def __aenter__(self) -> object:
        return self.connection

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_check_mysql_connection_executes_select_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SimpleNamespace(scalar_one=lambda: 1)
    connection = SimpleNamespace(
        execute=AsyncMock(return_value=result),
    )
    engine = SimpleNamespace(
        connect=lambda: _ConnectionContext(connection),
        dispose=AsyncMock(),
    )
    monkeypatch.setattr(
        "server.deployment.env_generator.create_async_engine",
        lambda *_args, **_kwargs: engine,
    )
    url = build_mysql_url(_connection_input())

    await check_mysql_connection(url)

    statement = connection.execute.await_args.args[0]
    assert statement.text == "SELECT 1"
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage",
    ("create", "connect", "execute", "scalar"),
)
async def test_check_mysql_connection_sanitizes_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure_stage: str,
) -> None:
    password = "unique-secret-password-$@/"
    url = build_mysql_url(_connection_input(password=password))
    dispose = AsyncMock()

    if failure_stage == "create":
        def create_engine(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError(f"driver leaked {password}")
    else:
        result = SimpleNamespace(
            scalar_one=(
                (lambda: 2)
                if failure_stage == "scalar"
                else (lambda: 1)
            )
        )
        execute = AsyncMock(return_value=result)
        if failure_stage == "execute":
            execute.side_effect = RuntimeError(
                f"query leaked {password}"
            )
        connection = SimpleNamespace(execute=execute)

        if failure_stage == "connect":
            class FailingContext:
                async def __aenter__(self) -> object:
                    raise RuntimeError(f"connect leaked {password}")

                async def __aexit__(self, *_args: object) -> None:
                    return None

            def connect() -> FailingContext:
                return FailingContext()
        else:
            def connect() -> _ConnectionContext:
                return _ConnectionContext(connection)

        engine = SimpleNamespace(connect=connect, dispose=dispose)

        def create_engine(
            *_args: object,
            **_kwargs: object,
        ) -> object:
            return engine

    monkeypatch.setattr(
        "server.deployment.env_generator.create_async_engine",
        create_engine,
    )

    expected_code = (
        "DATABASE_CONNECTION_CHECK_FAILED"
        if failure_stage == "scalar"
        else "DATABASE_CONNECTION_FAILED"
    )
    with pytest.raises(
        EnvironmentGenerationError,
        match=expected_code,
    ) as captured:
        await check_mysql_connection(url)

    output = capsys.readouterr()
    assert captured.value.code == expected_code
    assert captured.value.__cause__ is None
    assert password not in str(captured.value)
    assert password not in output.out
    assert password not in output.err
    if failure_stage != "create":
        dispose.assert_awaited_once()


class _TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


def _prompt_input() -> _TTYStringIO:
    return _TTYStringIO(
        "db.example.test\n"
        "\n"
        "pas\n"
        "pas@example\n"
        "\n"
    )


def _read_generated_env(path: os.PathLike[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as stream:
        for line in stream.read().splitlines():
            key, raw = line.split("=", 1)
            assert raw.startswith("'") and raw.endswith("'")
            values[key] = raw[1:-1]
    return values


def _read_pty_until(
    descriptor: int,
    marker: bytes,
    *,
    timeout: float = 5,
) -> bytes:
    output = bytearray()
    deadline = time.monotonic() + timeout
    while marker not in output:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"PTY output did not contain {marker!r}"
        readable, _, _ = select.select(
            [descriptor],
            [],
            [],
            remaining,
        )
        assert readable, f"PTY output did not contain {marker!r}"
        output.extend(os.read(descriptor, 64))
    return bytes(output)


@pytest.mark.asyncio
async def test_create_environment_file_protects_and_round_trips_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    password = "$@:/?#% space'\"\\密码"
    output = tmp_path / ".env"
    terminal = _TTYStringIO()
    check = AsyncMock()
    monkeypatch.setattr(
        "server.deployment.env_generator.check_mysql_connection",
        check,
    )

    await create_environment_file(
        output.resolve(),
        skip_connection_test=False,
        image="registry.example/pas:test",
        input_stream=_prompt_input(),
        output_stream=terminal,
        secret_reader=lambda _prompt: password,
    )

    values = _read_generated_env(output)
    parsed_url = make_url(values["PAS_DATABASE_URL"])
    assert parsed_url.drivername == "mysql+asyncmy"
    assert parsed_url.password == password
    assert len(
        base64.b64decode(
            values["PAS_ENCRYPTION_KEY"],
            validate=True,
        )
    ) == 32
    assert values["PAS_IMAGE"] == "registry.example/pas:test"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    checked_url = check.await_args.args[0]
    assert checked_url.password == password
    visible = terminal.getvalue()
    assert password not in visible
    assert values["PAS_DATABASE_URL"] not in visible
    assert values["PAS_ENCRYPTION_KEY"] not in visible
    assert "Use these settings? [Y/n]:" in visible
    assert "Action: connect and execute SELECT 1" in visible
    assert "Connection test succeeded" in visible


@pytest.mark.asyncio
async def test_create_environment_file_can_reenter_connection_settings(
    tmp_path: Path,
) -> None:
    output = tmp_path / ".env"
    terminal = _TTYStringIO()
    input_stream = _TTYStringIO(
        "db.example.test\n3306\nwrong\npas\nn\n"
        "db.example.test\n3306\npas\npas\ny\n"
    )

    await create_environment_file(
        output.resolve(),
        skip_connection_test=True,
        image=None,
        input_stream=input_stream,
        output_stream=terminal,
        secret_reader=lambda _prompt: "password",
    )

    parsed_url = make_url(
        _read_generated_env(output)["PAS_DATABASE_URL"]
    )
    assert parsed_url.database == "pas"
    assert terminal.getvalue().count(
        "Review metadata database settings:"
    ) == 2


def test_masked_secret_reader_displays_asterisks_and_supports_backspace() -> None:
    reader = getattr(env_generator, "read_masked_secret", None)
    assert callable(reader)
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "from server.deployment.env_generator import "
                "read_masked_secret; "
                "value = read_masked_secret('Password: '); "
                "raise SystemExit(0 if value == 'ac' else 7)"
            ),
        ],
        cwd=ROOT,
        stdin=slave,
        stdout=slave,
        stderr=slave,
    )
    os.close(slave)
    try:
        prompt = _read_pty_until(master, b"Password: ")
        os.write(master, b"ab\x7fc\r")
        visible = prompt + _read_pty_until(master, b"\n")
        returncode = process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        os.close(master)

    assert returncode == 0
    assert visible.count(b"*") == 3
    assert b"ac" not in visible


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "reason"),
    (
        (1045, "Authentication failed"),
        (1049, "selected database does not exist"),
        (2003, "refused the connection or could not be reached"),
        (2005, "could not be resolved"),
    ),
)
async def test_check_mysql_connection_reports_sanitized_mysql_reason(
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
    reason: str,
) -> None:
    password = "reason-test-secret"

    def create_engine(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(error_code, f"driver leaked {password}")

    monkeypatch.setattr(
        "server.deployment.env_generator.create_async_engine",
        create_engine,
    )

    with pytest.raises(EnvironmentGenerationError) as captured:
        await check_mysql_connection(
            build_mysql_url(_connection_input(password=password))
        )

    assert reason in str(captured.value)
    assert password not in str(captured.value)


@pytest.mark.asyncio
async def test_create_environment_file_replaces_container_loopback(
    tmp_path: Path,
) -> None:
    terminal = _TTYStringIO()
    output = tmp_path / ".env"

    await create_environment_file(
        output.resolve(),
        skip_connection_test=True,
        image=None,
        input_stream=_TTYStringIO(
            "127.0.0.1\n3306\npas\nroot\n\n\n"
        ),
        output_stream=terminal,
        secret_reader=lambda _prompt: "password",
    )

    parsed_url = make_url(
        _read_generated_env(output)["PAS_DATABASE_URL"]
    )
    assert parsed_url.host == "host.docker.internal"
    assert (
        "Use host.docker.internal instead? [Y/n]:"
        in terminal.getvalue()
    )
    assert "Endpoint: host.docker.internal" in terminal.getvalue()
    assert "Endpoint: 127.0.0.1\n" not in terminal.getvalue()


@pytest.mark.asyncio
async def test_create_environment_file_can_keep_container_loopback(
    tmp_path: Path,
) -> None:
    terminal = _TTYStringIO()
    output = tmp_path / ".env"

    await create_environment_file(
        output.resolve(),
        skip_connection_test=True,
        image=None,
        input_stream=_TTYStringIO(
            "127.0.0.1\n3306\npas\nroot\nn\n\n"
        ),
        output_stream=terminal,
        secret_reader=lambda _prompt: "password",
    )

    parsed_url = make_url(
        _read_generated_env(output)["PAS_DATABASE_URL"]
    )
    assert parsed_url.host == "127.0.0.1"
    assert "Keeping loopback endpoint 127.0.0.1" in (
        terminal.getvalue()
    )
    assert "host.docker.internal" in terminal.getvalue()


@pytest.mark.asyncio
async def test_generated_env_survives_compose_interpolation(
    tmp_path: Path,
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is unavailable")
    compose = subprocess.run(
        [docker, "compose", "version"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if compose.returncode != 0:
        pytest.skip("Docker Compose is unavailable")

    password = "$@:/?#% space"
    output = tmp_path / ".env"
    await create_environment_file(
        output.resolve(),
        skip_connection_test=True,
        image=None,
        input_stream=_prompt_input(),
        output_stream=_TTYStringIO(),
        secret_reader=lambda _prompt: password,
    )
    assert make_url(
        _read_generated_env(output)["PAS_DATABASE_URL"]
    ).password == password

    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(output),
            "-f",
            str(
                ROOT
                / "deploy/compose/compose.external-mysql.yaml"
            ),
            "config",
            "--quiet",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, (
        "Docker Compose rejected the generated environment file"
    )


@pytest.mark.asyncio
async def test_create_environment_file_refuses_existing_target_before_prompt(
    tmp_path: Path,
) -> None:
    output = tmp_path / ".env"
    output.write_text("existing", encoding="utf-8")
    prompt = _TTYStringIO()

    with pytest.raises(
        EnvironmentGenerationError,
        match="ENV_FILE_EXISTS",
    ):
        await create_environment_file(
            output.resolve(),
            skip_connection_test=True,
            image=None,
            input_stream=prompt,
            output_stream=_TTYStringIO(),
            secret_reader=lambda _prompt: "unused",
        )

    assert prompt.tell() == 0
    assert output.read_text(encoding="utf-8") == "existing"


@pytest.mark.asyncio
async def test_create_environment_file_refuses_symlink_target(
    tmp_path: Path,
) -> None:
    referenced = tmp_path / "referenced"
    referenced.write_text("do-not-change", encoding="utf-8")
    output = tmp_path / ".env"
    output.symlink_to(referenced)

    with pytest.raises(
        EnvironmentGenerationError,
        match="ENV_FILE_EXISTS",
    ):
        await create_environment_file(
            output,
            skip_connection_test=True,
            image=None,
            input_stream=_prompt_input(),
            output_stream=_TTYStringIO(),
            secret_reader=lambda _prompt: "unused",
        )

    assert referenced.read_text(encoding="utf-8") == "do-not-change"


@pytest.mark.asyncio
async def test_create_environment_file_requires_interactive_terminal(
    tmp_path: Path,
) -> None:
    with pytest.raises(
        EnvironmentGenerationError,
        match="INTERACTIVE_TERMINAL_REQUIRED",
    ):
        await create_environment_file(
            (tmp_path / ".env").resolve(),
            skip_connection_test=True,
            image=None,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
            secret_reader=lambda _prompt: "unused",
        )


@pytest.mark.asyncio
async def test_create_environment_file_fails_closed_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / ".env"
    password = "do-not-leak-this-password"
    check = AsyncMock(
        side_effect=EnvironmentGenerationError(
            "DATABASE_CONNECTION_FAILED",
            "Unable to connect to the metadata database.",
        )
    )
    monkeypatch.setattr(
        "server.deployment.env_generator.check_mysql_connection",
        check,
    )
    token = SimpleNamespace(called=False)

    def generate_key(_length: int) -> bytes:
        token.called = True
        return b"x" * 32

    monkeypatch.setattr(
        "server.deployment.env_generator.secrets.token_bytes",
        generate_key,
    )

    with pytest.raises(
        EnvironmentGenerationError,
        match="DATABASE_CONNECTION_FAILED",
    ) as captured:
        await create_environment_file(
            output.resolve(),
            skip_connection_test=False,
            image=None,
            input_stream=_prompt_input(),
            output_stream=_TTYStringIO(),
            secret_reader=lambda _prompt: password,
        )

    assert not output.exists()
    assert token.called is False
    assert password not in str(captured.value)


@pytest.mark.asyncio
async def test_create_environment_file_can_explicitly_skip_connection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / ".env"
    terminal = _TTYStringIO()
    check = AsyncMock()
    monkeypatch.setattr(
        "server.deployment.env_generator.check_mysql_connection",
        check,
    )

    await create_environment_file(
        output.resolve(),
        skip_connection_test=True,
        image=None,
        input_stream=_prompt_input(),
        output_stream=terminal,
        secret_reader=lambda _prompt: "password",
    )

    check.assert_not_awaited()
    assert "WARNING" in terminal.getvalue()
    assert set(_read_generated_env(output)) == {
        "PAS_DATABASE_URL",
        "PAS_ENCRYPTION_KEY",
    }


@pytest.mark.asyncio
async def test_create_environment_file_removes_partial_output_on_fsync_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / ".env"
    monkeypatch.setattr(
        "server.deployment.env_generator.os.fsync",
        lambda _descriptor: (_ for _ in ()).throw(
            OSError("test-only failure")
        ),
    )

    with pytest.raises(
        EnvironmentGenerationError,
        match="ENV_FILE_WRITE_FAILED",
    ):
        await create_environment_file(
            output.resolve(),
            skip_connection_test=True,
            image=None,
            input_stream=_prompt_input(),
            output_stream=_TTYStringIO(),
            secret_reader=lambda _prompt: "password",
        )

    assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "image",
    ("", "registry.example/pas:test\nother", "registry.example/'bad"),
)
async def test_create_environment_file_rejects_invalid_image(
    tmp_path: Path,
    image: str,
) -> None:
    with pytest.raises(
        EnvironmentGenerationError,
        match="PAS_IMAGE_INVALID",
    ):
        await create_environment_file(
            (tmp_path / ".env").resolve(),
            skip_connection_test=True,
            image=image,
            input_stream=_prompt_input(),
            output_stream=_TTYStringIO(),
            secret_reader=lambda _prompt: "password",
        )
