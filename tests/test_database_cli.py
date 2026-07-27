from __future__ import annotations

from server.cli import build_parser, main


def test_parser_exposes_database_lifecycle_commands() -> None:
    parser = build_parser()

    check = parser.parse_args(["database", "check"])
    migrate = parser.parse_args(["database", "migrate"])

    assert check.handler
    assert migrate.handler


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
