from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from cryptography.exceptions import InvalidTag
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from server.bootstrap import load_bootstrap_settings
from server.configuration.module_migrations import ModuleMigrationError
from server.configuration.repository import ConfigRepository
from server.configuration.runtime import project_app_config
from server.core.config_crypto import ConfigCrypto
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


def _caused_by_invalid_tag(error: BaseException) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        if isinstance(current, InvalidTag):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


async def check_database_compatibility(
    database_url: str | None = None,
    encryption_key: bytes | None = None,
) -> str:
    """Validate schema and the root key against persisted configuration."""
    settings = None
    if database_url is None or encryption_key is None:
        settings = load_bootstrap_settings()
    if database_url is None:
        assert settings is not None
        database_url = settings.database_url
    if encryption_key is None:
        assert settings is not None
        encryption_key = settings.encryption_key

    revision = await check_database_schema(database_url)
    engine = create_async_engine(database_url, poolclass=NullPool)
    enable_sqlite_foreign_keys(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        documents = await ConfigRepository(factory).list_modules()
        if documents:
            project_app_config(documents, ConfigCrypto(encryption_key))
    except Exception as error:
        if _caused_by_invalid_tag(error):
            raise DatabaseSchemaError(
                "DATABASE_ENCRYPTION_KEY_MISMATCH",
                "The configured PAS_ENCRYPTION_KEY cannot decrypt the "
                "metadata database. Restore the original root key paired "
                "with this database.",
            ) from None
        if isinstance(error, ModuleMigrationError):
            raise DatabaseSchemaError(
                "DATABASE_CONFIGURATION_INCOMPATIBLE",
                "The persisted configuration is incompatible with this "
                "application version.",
            ) from None
        raise DatabaseSchemaError(
            "DATABASE_CONFIGURATION_INCOMPATIBLE",
            "The persisted configuration cannot be loaded safely.",
        ) from None
    finally:
        await engine.dispose()
    return revision


def migrate_database(database_url: str | None = None) -> None:
    url = database_url or load_bootstrap_settings().database_url
    command.upgrade(_alembic_config(url), "head")
