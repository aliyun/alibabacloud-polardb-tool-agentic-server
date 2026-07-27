from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from server.bootstrap import load_bootstrap_settings
from server.db.engine import enable_sqlite_foreign_keys

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class DatabaseSchemaError(RuntimeError):
    """A sanitized database schema compatibility failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _alembic_config(database_url: str | None = None) -> Config:
    config_path = _ROOT / "alembic.ini"
    config = Config(str(config_path)) if config_path.is_file() else Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    if database_url is not None:
        config.attributes["database_url"] = database_url
    return config


def required_schema_head() -> str:
    heads = ScriptDirectory.from_config(_alembic_config()).get_heads()
    if len(heads) != 1:
        raise DatabaseSchemaError(
            "DATABASE_MIGRATION_HEAD_INVALID",
            "The application migration graph must contain one head.",
        )
    return heads[0]


async def check_database_schema(
    database_url: str | None = None,
) -> str:
    url = database_url or load_bootstrap_settings().database_url
    required = required_schema_head()
    script = ScriptDirectory.from_config(_alembic_config())
    known_revisions = {
        revision.revision for revision in script.walk_revisions()
    }
    engine = create_async_engine(url, poolclass=NullPool)
    enable_sqlite_foreign_keys(engine)
    try:
        async with engine.connect() as connection:
            current_heads = await connection.run_sync(
                lambda sync_connection: MigrationContext.configure(
                    sync_connection
                ).get_current_heads()
            )
    except Exception:
        raise DatabaseSchemaError(
            "DATABASE_UNAVAILABLE",
            "The metadata database is unavailable.",
        ) from None
    finally:
        await engine.dispose()

    if not current_heads:
        raise DatabaseSchemaError(
            "DATABASE_SCHEMA_NOT_INITIALIZED",
            "The metadata database is not initialized. "
            "Run 'pas database migrate'.",
        )
    if len(current_heads) != 1:
        raise DatabaseSchemaError(
            "DATABASE_MIGRATION_HEAD_INVALID",
            "The metadata database contains multiple migration heads.",
        )
    current = current_heads[0]
    if current == required:
        return required
    if current not in known_revisions:
        raise DatabaseSchemaError(
            "DATABASE_SCHEMA_TOO_NEW",
            "The metadata database revision is not supported by this "
            "application version.",
        )
    raise DatabaseSchemaError(
        "DATABASE_SCHEMA_OUTDATED",
        "The metadata database is behind the required revision. "
        "Run 'pas database migrate'.",
    )


def migrate_database(database_url: str | None = None) -> None:
    url = database_url or load_bootstrap_settings().database_url
    command.upgrade(_alembic_config(url), "head")
