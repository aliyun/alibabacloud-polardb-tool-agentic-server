from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from server.cli import build_parser, main
from server.deployment.env_generator import EnvironmentGenerationError


def test_parser_exposes_database_lifecycle_commands() -> None:
    parser = build_parser()

    check = parser.parse_args(["database", "check"])
    migrate = parser.parse_args(["database", "migrate"])
    create_env = parser.parse_args(
        [
            "database",
            "create-env",
            "--output",
            "/output/generated.env",
            "--skip-connection-test",
            "--image",
            "registry.example/pas:test",
        ]
    )

    assert check.handler
    assert migrate.handler
    assert create_env.handler


def test_database_check_handler_prints_current_revision(
    monkeypatch, capsys
) -> None:
    async def current() -> str:
        return "abc123"

    monkeypatch.setattr(
        "server.db.schema.check_database_schema",
        current,
    )

    assert main(["database", "check"]) == 0
    assert capsys.readouterr().out == (
        "Database schema is current: abc123\n"
    )


def test_database_migrate_handler_runs_upgrade(
    monkeypatch, capsys
) -> None:
    called = False

    def migrate() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("server.db.schema.migrate_database", migrate)

    assert main(["database", "migrate"]) == 0
    assert called is True
    assert capsys.readouterr().out == "Database migration completed.\n"


def test_database_create_env_handler_passes_safe_sources(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    create = AsyncMock()
    monkeypatch.setattr(
        "server.deployment.env_generator.create_environment_file",
        create,
    )

    assert main(
        [
            "database",
            "create-env",
            "--output",
            "/output/generated.env",
            "--skip-connection-test",
            "--image",
            "registry.example/pas:test",
        ]
    ) == 0

    assert create.await_args.kwargs["output"] == Path(
        "/output/generated.env"
    )
    assert create.await_args.kwargs["skip_connection_test"] is True
    assert (
        create.await_args.kwargs["image"]
        == "registry.example/pas:test"
    )
    assert create.await_args.kwargs["input_stream"]
    assert create.await_args.kwargs["output_stream"]
    assert create.await_args.kwargs["secret_reader"]
    assert (
        create.await_args.kwargs["secret_reader"].__name__
        == "read_masked_secret"
    )
    assert capsys.readouterr().out == (
        "Environment file created at /output/generated.env\n"
    )


def test_database_create_env_handler_sanitizes_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    password = "must-not-be-rendered"
    create = AsyncMock(
        side_effect=EnvironmentGenerationError(
            "DATABASE_CONNECTION_FAILED",
            "Unable to connect to the metadata database.",
        )
    )
    monkeypatch.setattr(
        "server.deployment.env_generator.create_environment_file",
        create,
    )

    with pytest.raises(SystemExit):
        main(
            [
                "database",
                "create-env",
                "--output",
                "/output/generated.env",
            ]
        )

    captured = capsys.readouterr()
    assert "DATABASE_CONNECTION_FAILED" in captured.err
    assert password not in captured.out
    assert password not in captured.err
