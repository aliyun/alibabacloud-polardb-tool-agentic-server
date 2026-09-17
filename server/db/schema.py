from __future__ import annotations

from pathlib import Path

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
from server.db import schema_contract
from server.db.legacy_f6_schema_repair import (
    LegacyF6SchemaState,
    inspect_legacy_managed_f6_schema,
)

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
            def inspect_schema(sync_connection):
                current_heads = MigrationContext.configure(
                    sync_connection
                ).get_current_heads()
                physical_state = inspect_legacy_managed_f6_schema(
                    sync_connection
                )
                record = schema_contract.read_record(sync_connection, schema_contract.CONTRACT_KEY)
                errors = (
                    schema_contract.physical_errors(sync_connection)
                    if current_heads == (required,) or record is not None
                    else []
                )
                return current_heads, physical_state, record, errors

            current_heads, physical_state, record, errors = await connection.run_sync(
                inspect_schema
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
    if physical_state == LegacyF6SchemaState.REPAIR_REQUIRED:
        raise DatabaseSchemaError(
            "DATABASE_SCHEMA_REPAIR_REQUIRED",
            "The metadata database has a known legacy revision whose "
            "physical schema requires repair. Run 'pas database migrate'.",
        )
    if physical_state == LegacyF6SchemaState.PARTIAL:
        raise DatabaseSchemaError(
            "DATABASE_SCHEMA_REPAIR_PARTIAL",
            "The metadata database contains a partially applied "
            "schema repair. Stop the rollout and inspect the "
            "migration state.",
        )
    if physical_state == LegacyF6SchemaState.UNKNOWN:
        raise DatabaseSchemaError(
            "DATABASE_SCHEMA_PHYSICAL_STATE_UNKNOWN",
            "The metadata database physical schema is not recognized "
            "for its reported revision.",
        )
    if record is not None and not schema_contract.accepts_record(record, current):
        raise DatabaseSchemaError("DATABASE_SCHEMA_CONTRACT_INVALID", "The stored schema compatibility contract is not supported or does not match its revision.")
    if errors:
        raise DatabaseSchemaError("DATABASE_SCHEMA_PHYSICAL_STATE_UNKNOWN", "The metadata database does not satisfy this application's physical schema requirements.")
    if current == required or record is not None:
        return current
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


async def inspect_database(database_url: str | None = None) -> dict:
    """Read-only, credential-free output for deployment preflight."""
    url = database_url or load_bootstrap_settings().database_url
    value = schema_contract.manifest()
    result = {
        "protocol_version": 1,
        "target_head": value["head"],
        "generation": value["generation"],
        "release_classification": value["release_classification"],
        "manifest_digest": schema_contract.digest(value),
        "compatible": False,
        "current_head": None,
        "error_code": None,
        "current_heads": [],
        "pending_revisions": [],
    }
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            heads = await connection.run_sync(lambda sync: MigrationContext.configure(sync).get_current_heads())
            result["current_heads"] = list(heads)
            result["current_head"] = heads[0] if len(heads) == 1 else None
            script = ScriptDirectory.from_config(_alembic_config(url))
            known = {revision.revision for revision in script.walk_revisions()}
            if not heads or len(heads) == 1 and heads[0] in known:
                result["pending_revisions"] = [revision.revision for revision in reversed(list(script.iterate_revisions(value["head"], heads[0] if heads else "base")))]
    except Exception:
        result["error_code"] = "DATABASE_UNAVAILABLE"
        return result
    finally:
        await engine.dispose()
    try:
        result["current_head"] = await check_database_schema(url)
        result["compatible"] = True
    except DatabaseSchemaError as error:
        result["error_code"] = error.code
    return result


def migrate_database(database_url: str | None = None, *, operation_id: str | None = None) -> None:
    from server.db.migration_runner import migrate
    url = database_url or load_bootstrap_settings().database_url
    migrate(url, operation_id)
