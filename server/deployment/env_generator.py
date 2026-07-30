from __future__ import annotations

import base64
import codecs
import os
import secrets
import sys
import termios
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TextIO

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool


class EnvironmentGenerationError(ValueError):
    """A sanitized environment generation failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class MySQLConnectionInput:
    endpoint: str
    port: int
    database: str
    username: str
    password: str


def _has_control_characters(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _validate_connection_input(value: MySQLConnectionInput) -> None:
    _validate_connection_target(
        value.endpoint,
        value.port,
        value.database,
        value.username,
    )
    if not value.password:
        raise EnvironmentGenerationError(
            "DATABASE_PASSWORD_INVALID",
            "The metadata database password is required.",
        )


def _validate_connection_target(
    endpoint: str,
    port: int,
    database: str,
    username: str,
) -> None:
    if (
        not endpoint
        or _has_control_characters(endpoint)
        or any(character.isspace() for character in endpoint)
    ):
        raise EnvironmentGenerationError(
            "DATABASE_ENDPOINT_INVALID",
            "The metadata database endpoint is invalid.",
        )
    if not 1 <= port <= 65535:
        raise EnvironmentGenerationError(
            "DATABASE_PORT_INVALID",
            "The metadata database port must be between 1 and 65535.",
        )
    if not database or _has_control_characters(database):
        raise EnvironmentGenerationError(
            "DATABASE_NAME_INVALID",
            "The metadata database name is invalid.",
        )
    if not username or _has_control_characters(username):
        raise EnvironmentGenerationError(
            "DATABASE_USERNAME_INVALID",
            "The metadata database username is invalid.",
        )
def build_mysql_url(value: MySQLConnectionInput) -> URL:
    _validate_connection_input(value)
    return URL.create(
        "mysql+asyncmy",
        username=value.username,
        password=value.password,
        host=value.endpoint,
        port=value.port,
        database=value.database,
    )


async def check_mysql_connection(url: URL) -> None:
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            if result.scalar_one() != 1:
                raise EnvironmentGenerationError(
                    "DATABASE_CONNECTION_CHECK_FAILED",
                    "Connected, but SELECT 1 did not return 1.",
                )
    except EnvironmentGenerationError:
        raise
    except Exception as exc:
        raise EnvironmentGenerationError(
            "DATABASE_CONNECTION_FAILED",
            _connection_failure_reason(exc),
        ) from None
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                pass


def _exception_chain(error: BaseException) -> list[BaseException]:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    result: list[BaseException] = []
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        for related in (
            getattr(current, "orig", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(related, BaseException):
                pending.append(related)
    return result


def _connection_failure_reason(error: BaseException) -> str:
    chain = _exception_chain(error)
    error_code: int | None = None
    for current in chain:
        if current.args and isinstance(current.args[0], int):
            error_code = current.args[0]
            break

    reasons = {
        1044: (
            "The supplied user is not allowed to access the selected "
            "database (MySQL error 1044)."
        ),
        1045: (
            "Authentication failed for the supplied username and password "
            "(MySQL error 1045)."
        ),
        1049: (
            "The selected database does not exist (MySQL error 1049)."
        ),
        2002: (
            "The database endpoint refused the connection or could not be "
            "reached (MySQL error 2002)."
        ),
        2003: (
            "The database endpoint refused the connection or could not be "
            "reached (MySQL error 2003)."
        ),
        2005: (
            "The database endpoint name could not be resolved "
            "(MySQL error 2005)."
        ),
        2013: (
            "The database connection was lost during SELECT 1 "
            "(MySQL error 2013)."
        ),
    }
    if error_code in reasons:
        return reasons[error_code]
    if any(isinstance(current, TimeoutError) for current in chain):
        return "The database connection attempt timed out."
    return (
        "Unable to connect. Check the endpoint, port, database, username, "
        "password, network access, and database grants."
    )


def _prompt(
    label: str,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> str:
    output_stream.write(label)
    output_stream.flush()
    value = input_stream.readline()
    if value == "":
        raise EnvironmentGenerationError(
            "INTERACTIVE_INPUT_FAILED",
            "Interactive input ended before configuration was complete.",
        )
    return value.rstrip("\r\n")


def read_masked_secret(
    prompt: str,
    *,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> str:
    try:
        descriptor = input_stream.fileno()
        original = termios.tcgetattr(descriptor)
    except (AttributeError, OSError, termios.error):
        raise EnvironmentGenerationError(
            "INTERACTIVE_TERMINAL_REQUIRED",
            "Masked password input requires an interactive terminal.",
        ) from None

    settings = original.copy()
    settings[6] = original[6].copy()
    settings[3] &= ~(termios.ECHO | termios.ICANON)
    settings[6][termios.VMIN] = 1
    settings[6][termios.VTIME] = 0
    characters: list[str] = []
    decoder = codecs.getincrementaldecoder(
        input_stream.encoding or "utf-8"
    )(errors="strict")
    try:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, settings)
        output_stream.write(prompt)
        output_stream.flush()
        while True:
            raw_character = os.read(descriptor, 1)
            if raw_character == b"":
                raise EnvironmentGenerationError(
                    "INTERACTIVE_INPUT_FAILED",
                    "Unable to read the metadata database password.",
                )
            decoded = decoder.decode(raw_character)
            for character in decoded:
                if character in {"\r", "\n"}:
                    return "".join(characters)
                if character in {"\b", "\x7f"}:
                    if characters:
                        characters.pop()
                        output_stream.write("\b \b")
                        output_stream.flush()
                    continue
                if character == "\x15":
                    while characters:
                        characters.pop()
                        output_stream.write("\b \b")
                    output_stream.flush()
                    continue
                if ord(character) < 32:
                    continue
                characters.append(character)
                output_stream.write("*")
                output_stream.flush()
    except UnicodeError:
        raise EnvironmentGenerationError(
            "INTERACTIVE_INPUT_FAILED",
            "Unable to decode the metadata database password.",
        ) from None
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
        output_stream.write("\n")
        output_stream.flush()


def _write_connection_review(
    *,
    endpoint: str,
    port: int,
    database: str,
    username: str,
    output_stream: TextIO,
) -> None:
    output_stream.write(
        "Review metadata database settings:\n"
        f"  Endpoint: {endpoint}\n"
        f"  Port: {port}\n"
        f"  Database: {database}\n"
        f"  Username: {username}\n"
    )
    output_stream.flush()


def _resolve_loopback_endpoint(
    endpoint: str,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> str:
    if endpoint not in {"127.0.0.1", "localhost", "::1"}:
        return endpoint

    output_stream.write(
        f"Loopback endpoint detected: {endpoint} refers to the generator "
        "container when this command runs through Docker.\n"
    )
    output_stream.flush()
    while True:
        replace = _prompt(
            "Use host.docker.internal instead? [Y/n]: ",
            input_stream=input_stream,
            output_stream=output_stream,
        ).strip().lower()
        if replace in {"", "y", "yes"}:
            output_stream.write(
                f"Using host.docker.internal instead of {endpoint}.\n"
            )
            output_stream.flush()
            return "host.docker.internal"
        if replace in {"n", "no"}:
            output_stream.write(
                f"Keeping loopback endpoint {endpoint}. It refers to the "
                "generator container; answer n at the settings review if "
                "you need to re-enter it.\n"
            )
            output_stream.flush()
            return endpoint
        output_stream.write(
            "Please answer y or n.\n"
        )
        output_stream.flush()


def _prompt_connection(
    *,
    input_stream: TextIO,
    output_stream: TextIO,
    secret_reader: Callable[[str], str],
) -> MySQLConnectionInput:
    while True:
        endpoint = _prompt(
            "Metadata database endpoint: ",
            input_stream=input_stream,
            output_stream=output_stream,
        ).strip()
        raw_port = _prompt(
            "Metadata database port [3306]: ",
            input_stream=input_stream,
            output_stream=output_stream,
        ).strip()
        try:
            port = int(raw_port or "3306")
        except ValueError:
            raise EnvironmentGenerationError(
                "DATABASE_PORT_INVALID",
                "The metadata database port must be an integer.",
            ) from None
        database = _prompt(
            "Metadata database name: ",
            input_stream=input_stream,
            output_stream=output_stream,
        ).strip()
        username = _prompt(
            "Metadata database username: ",
            input_stream=input_stream,
            output_stream=output_stream,
        ).strip()
        _validate_connection_target(
            endpoint,
            port,
            database,
            username,
        )
        endpoint = _resolve_loopback_endpoint(
            endpoint,
            input_stream=input_stream,
            output_stream=output_stream,
        )
        _write_connection_review(
            endpoint=endpoint,
            port=port,
            database=database,
            username=username,
            output_stream=output_stream,
        )
        confirmation = _prompt(
            "Use these settings? [Y/n]: ",
            input_stream=input_stream,
            output_stream=output_stream,
        ).strip().lower()
        if confirmation in {"", "y", "yes"}:
            break
        if confirmation in {"n", "no"}:
            output_stream.write("Re-entering connection settings.\n")
            output_stream.flush()
            continue
        output_stream.write("Please answer y or n.\n")
        output_stream.flush()

    try:
        password = secret_reader("Metadata database password: ")
    except (EOFError, OSError):
        raise EnvironmentGenerationError(
            "INTERACTIVE_INPUT_FAILED",
            "Unable to read the metadata database password.",
        ) from None
    return MySQLConnectionInput(
        endpoint=endpoint,
        port=port,
        database=database,
        username=username,
        password=password,
    )


def _literal_assignment(name: str, value: str) -> str:
    if (
        not value
        or "'" in value
        or _has_control_characters(value)
    ):
        raise EnvironmentGenerationError(
            f"{name}_INVALID",
            f"{name} cannot be represented safely.",
        )
    return f"{name}='{value}'\n"


def _render_environment(
    url: URL,
    encryption_key: bytes,
    image: str | None,
) -> str:
    rendered_url = url.render_as_string(hide_password=False)
    encoded_key = base64.b64encode(encryption_key).decode("ascii")
    content = (
        _literal_assignment("PAS_DATABASE_URL", rendered_url)
        + _literal_assignment("PAS_ENCRYPTION_KEY", encoded_key)
    )
    if image is not None:
        content += _literal_assignment("PAS_IMAGE", image)
    return content


def _write_exclusive(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    created = False
    succeeded = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        succeeded = True
    except FileExistsError:
        raise EnvironmentGenerationError(
            "ENV_FILE_EXISTS",
            "The environment file already exists.",
        ) from None
    except OSError:
        raise EnvironmentGenerationError(
            "ENV_FILE_WRITE_FAILED",
            "Unable to write the environment file.",
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if created and not succeeded:
            path.unlink(missing_ok=True)


async def create_environment_file(
    output: Path,
    *,
    skip_connection_test: bool,
    image: str | None,
    input_stream: TextIO,
    output_stream: TextIO,
    secret_reader: Callable[[str], str],
) -> None:
    if not output.is_absolute():
        raise EnvironmentGenerationError(
            "ENV_FILE_PATH_INVALID",
            "The environment file path must be absolute.",
        )
    if output.exists() or output.is_symlink():
        raise EnvironmentGenerationError(
            "ENV_FILE_EXISTS",
            "The environment file already exists.",
        )
    if image is not None:
        _literal_assignment("PAS_IMAGE", image)
    if not input_stream.isatty() or not output_stream.isatty():
        raise EnvironmentGenerationError(
            "INTERACTIVE_TERMINAL_REQUIRED",
            "Environment generation requires an interactive terminal.",
        )

    connection_input = _prompt_connection(
        input_stream=input_stream,
        output_stream=output_stream,
        secret_reader=secret_reader,
    )
    url = build_mysql_url(connection_input)
    if skip_connection_test:
        output_stream.write(
            "WARNING: Metadata database connection test skipped.\n"
        )
        output_stream.flush()
    else:
        output_stream.write(
            "Testing metadata database connection:\n"
            f"  Endpoint: {connection_input.endpoint}:"
            f"{connection_input.port}\n"
            f"  Database: {connection_input.database}\n"
            f"  Username: {connection_input.username}\n"
            "  Action: connect and execute SELECT 1\n"
        )
        output_stream.flush()
        await check_mysql_connection(url)
        output_stream.write(
            "Connection test succeeded: SELECT 1 returned 1.\n"
        )
        output_stream.flush()

    content = _render_environment(
        url,
        secrets.token_bytes(32),
        image,
    )
    _write_exclusive(output, content)
