"""Released migration bytes and narrowly scoped deployment compatibility."""

from __future__ import annotations

import hashlib
import json
from importlib import import_module
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext

ROOT = Path(__file__).resolve().parents[1]
RELEASED = json.loads(
    (ROOT / "tests/fixtures/released-migrations-v0.0.12.json").read_text()
)


@pytest.mark.parametrize("relative,expected", RELEASED["sha256"].items())
def test_released_migrations_are_immutable(relative: str, expected: str) -> None:
    assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, (
        f"Released migration changed since {RELEASED['release_tag']}: {relative}"
    )


def _predecessor(tmp_path: Path) -> tuple[Config, sa.Engine]:
    database = tmp_path / "legacy.db"
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = f"sqlite+aiosqlite:///{database}"
    command.upgrade(config, "c3d4e5f6a7b8")
    return config, sa.create_engine(f"sqlite:///{database}")


@pytest.mark.parametrize("ddl", [
    "ALTER TABLE agents ADD COLUMN oauth_redirect_uri VARCHAR(128)",
    "ALTER TABLE agents ADD COLUMN oauth_redirect_uri TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE agents ADD COLUMN oauth_redirect_uri TEXT DEFAULT ''",
    "CREATE INDEX ix_knowledge_resources_catalog_sync_token ON knowledge_resources(id)",
    "CREATE UNIQUE INDEX ix_knowledge_resources_catalog_sync_token ON knowledge_resources(id)",
])
def test_legacy_preflight_rejects_drift_before_adding_columns(tmp_path: Path, ddl: str) -> None:
    config, engine = _predecessor(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(ddl)
        with pytest.raises(RuntimeError, match="Incompatible existing"):
            command.upgrade(config, "fb1c2d3e4f5a")
        with engine.connect() as connection:
            assert "catalog_sync_token" not in {
                column["name"] for column in sa.inspect(connection).get_columns("knowledge_resources")
            }
            assert connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "c3d4e5f6a7b8"
    finally:
        engine.dispose()


def test_manual_operations_do_not_silently_reuse_existing_columns(tmp_path: Path) -> None:
    _, engine = _predecessor(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE agents ADD COLUMN oauth_redirect_uri TEXT")
            operations = Operations(MigrationContext.configure(connection))
            with pytest.raises(sa.exc.OperationalError, match="duplicate column"):
                operations.add_column("agents", sa.Column("oauth_redirect_uri", sa.Text(), nullable=True))
    finally:
        engine.dispose()


def test_compatibility_does_not_skip_duplicates_at_other_revisions(tmp_path: Path) -> None:
    config, engine = _predecessor(tmp_path)
    try:
        command.upgrade(config, "fb1c2d3e4f5a")

        def upgrade(*_args):
            return []

        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection, opts={"fn": upgrade}))
            with pytest.raises(sa.exc.OperationalError, match="duplicate column"):
                operations.add_column("agents", sa.Column("oauth_redirect_uri", sa.Text(), nullable=True))
    finally:
        engine.dispose()


def test_partial_catalog_index_is_not_reused(tmp_path: Path) -> None:
    config, engine = _predecessor(tmp_path)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE knowledge_resources ADD COLUMN catalog_sync_token VARCHAR(36)")
            connection.exec_driver_sql(
                "CREATE INDEX ix_knowledge_resources_catalog_sync_token "
                "ON knowledge_resources(catalog_sync_token) WHERE catalog_sync_token IS NOT NULL"
            )
        with pytest.raises(RuntimeError, match="Incompatible existing index"):
            command.upgrade(config, "fb1c2d3e4f5a")
        with engine.connect() as connection:
            assert "oauth_redirect_uri" not in {
                column["name"] for column in sa.inspect(connection).get_columns("agents")
            }
    finally:
        engine.dispose()


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_offline_sql_keeps_original_column_and_index_ddl(dialect: str) -> None:
    import_module("server.db.legacy_scalability_compat")
    output = StringIO()

    def upgrade(*_args):
        return []

    operations = Operations(MigrationContext.configure(
        dialect_name=dialect,
        opts={"as_sql": True, "output_buffer": output, "fn": upgrade},
    ))
    operations.add_column("agents", sa.Column("oauth_redirect_uri", sa.Text(), nullable=True))
    operations.create_index(
        "ix_knowledge_resources_catalog_sync_token", "knowledge_resources", ["catalog_sync_token"], unique=False,
    )
    sql = output.getvalue().upper()
    assert "ALTER TABLE AGENTS ADD COLUMN OAUTH_REDIRECT_URI TEXT" in sql
    assert "CREATE INDEX IX_KNOWLEDGE_RESOURCES_CATALOG_SYNC_TOKEN ON KNOWLEDGE_RESOURCES" in sql
