from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mysql

from server.models import SystemConfig


HEAD_REVISION = "d4e5f6a7b8c9"
KEY = base64.b64encode(b"01234567890123456789012345678901").decode()


def _config() -> AlembicConfig:
    return AlembicConfig(str(Path(__file__).parents[1] / "alembic.ini"))


def test_breaking_migration_replaces_system_settings(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "configuration.db"
    monkeypatch.setenv(
        "PAS_DATABASE_URL", f"sqlite+aiosqlite:///{database}"
    )
    monkeypatch.setenv("PAS_ENCRYPTION_KEY", KEY)

    command.upgrade(_config(), "head")

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "system_settings" not in tables
        assert {
            "system_config",
            "config_bootstrap_claim",
            "config_operation_receipts",
        } <= tables
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        assert revision == (HEAD_REVISION,)
    assert ScriptDirectory.from_config(_config()).get_heads() == [HEAD_REVISION]


def test_migration_model_compiles_mysql_config_as_longtext() -> None:
    column_type = SystemConfig.__table__.c.config_value.type

    assert isinstance(
        column_type.dialect_impl(mysql.dialect()), mysql.LONGTEXT
    )
