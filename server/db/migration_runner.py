"""Single-executor migration commands; application startup never calls this module."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from server.db import schema_contract as contract

MigrationReporter = Callable[[str], None]


def _report_migration(
    reporter: MigrationReporter | None,
    *,
    stage: str,
    status: str,
    **details: object,
) -> None:
    if reporter is None:
        return
    try:
        fields = {"stage": stage, "status": status, **details}
        rendered = " ".join(
            f"{name}={json.dumps(value, separators=(',', ':'))}"
            for name, value in fields.items()
        )
        reporter(f"PAS database migration: {rendered}")
    except Exception:
        # Observability must not change migration behavior.
        pass


def _next_action(error_code: str) -> str:
    if error_code == "DATABASE_MIGRATION_MANIFEST_INVALID":
        return (
            "Rebuild PAS from a source revision whose schema manifest "
            "matches the bundled Alembic migration graph."
        )
    if error_code == "DATABASE_MIGRATION_READ_ONLY":
        return "Use the metadata database writer endpoint."
    if error_code == "DATABASE_MIGRATION_BUSY":
        return (
            "Wait for the active migration executor to finish, then replay "
            "the same operation."
        )
    if error_code == "DATABASE_SCHEMA_TOO_NEW":
        return "Use a PAS image that supports the metadata database revision."
    if error_code in {
        "DATABASE_MIGRATION_REPAIR_REQUIRED",
        "DATABASE_SCHEMA_PHYSICAL_STATE_UNKNOWN",
    }:
        return (
            "Preserve the failed Pod and inspect the physical schema before "
            "retrying."
        )
    return (
        "Preserve the migration logs and inspect the database state before "
        "retrying."
    )


def _valid_release_manifest(manifest: dict, required_head: str) -> bool:
    classification = manifest.get("release_classification")
    baseline_head = manifest.get("baseline_head")
    required_sequence = manifest.get("required_sequence")
    compatible = manifest.get("compatible_manifest_digests", [])
    if (
        manifest.get("head") != required_head
        or classification not in {"NONE", "EXPAND"}
        or type(required_sequence) is not int
        or required_sequence < 0
        or not isinstance(compatible, list)
        or not all(isinstance(value, str) for value in compatible)
    ):
        return False
    if classification == "NONE":
        return required_head == baseline_head and required_sequence == 0
    return required_head != baseline_head and required_sequence > 0


@asynccontextmanager
async def migration_lock(connection, url: str):
    from server.db.schema import DatabaseSchemaError

    dialect = connection.dialect.name
    name = "pas-schema-" + hashlib.sha256((make_url(url).database or "").encode()).hexdigest()[:40]
    lock_file = None
    acquired = False
    try:
        if dialect == "mysql":
            if await connection.scalar(sa.text("SELECT @@read_only")):
                raise DatabaseSchemaError(
                    "DATABASE_MIGRATION_READ_ONLY", "Migration requires the metadata writer endpoint."
                )
            acquired = await connection.scalar(sa.text("SELECT GET_LOCK(:name, 0)"), {"name": name}) == 1
        elif dialect == "postgresql":
            key = int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], signed=True)
            acquired = await connection.scalar(sa.text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        elif dialect == "sqlite":
            database = make_url(url).database
            if not database or database == ":memory:":
                raise DatabaseSchemaError(
                    "DATABASE_MIGRATION_UNSUPPORTED", "Use a file-backed SQLite database for migrations."
                )
            path = Path(database).resolve().with_suffix(Path(database).suffix + ".migration.lock")
            lock_file = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        if not acquired:
            raise DatabaseSchemaError("DATABASE_MIGRATION_BUSY", "Another metadata migration owns the database lock.")
        await connection.commit()
        yield
    finally:
        if acquired:
            if dialect == "mysql" and not connection.invalidated:
                await connection.execute(sa.text("SELECT RELEASE_LOCK(:name)"), {"name": name})
            elif dialect == "postgresql" and not connection.invalidated:
                await connection.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        if lock_file is not None:
            os.close(lock_file)


async def run_migration(
    url: str,
    operation_id: str | None = None,
    reporter: MigrationReporter | None = None,
) -> None:
    from server.db.schema import DatabaseSchemaError, _alembic_config, required_schema_head

    try:
        manifest = contract.manifest()
        required_head = required_schema_head()
    except DatabaseSchemaError as error:
        _report_migration(
            reporter,
            stage="manifest-validation",
            status="failed",
            error_code=error.code,
            database_connected=False,
            schema_changes_attempted=False,
            next_action=_next_action(error.code),
        )
        raise
    except Exception:
        error_code = "DATABASE_MIGRATION_MANIFEST_INVALID"
        _report_migration(
            reporter,
            stage="manifest-validation",
            status="failed",
            error_code=error_code,
            database_connected=False,
            schema_changes_attempted=False,
            next_action=_next_action(error_code),
        )
        raise DatabaseSchemaError(
            error_code,
            "The release manifest or bundled Alembic migration graph could "
            "not be read. No database connection was opened and no schema "
            f"changes were attempted. {_next_action(error_code)}",
        ) from None
    manifest_details = {
        "required_head": required_head,
        "manifest_head": manifest.get("head"),
        "baseline_head": manifest.get("baseline_head"),
        "release_classification": manifest.get("release_classification"),
        "required_sequence": manifest.get("required_sequence"),
        "database_connected": False,
        "schema_changes_attempted": False,
    }
    _report_migration(
        reporter,
        stage="manifest-validation",
        status="started",
        **manifest_details,
    )
    if not _valid_release_manifest(manifest, required_head):
        error_code = "DATABASE_MIGRATION_MANIFEST_INVALID"
        _report_migration(
            reporter,
            stage="manifest-validation",
            status="failed",
            error_code=error_code,
            **manifest_details,
            next_action=_next_action(error_code),
        )
        raise DatabaseSchemaError(
            error_code,
            "The release manifest does not match the bundled Alembic migration "
            f"graph (required_head={required_head}, "
            f"manifest_head={manifest.get('head')}, "
            f"release_classification={manifest.get('release_classification')}, "
            f"required_sequence={manifest.get('required_sequence')}). "
            "No database connection was opened and no schema changes were "
            f"attempted. {_next_action(error_code)}",
        )
    _report_migration(
        reporter,
        stage="manifest-validation",
        status="succeeded",
        **manifest_details,
    )
    stage = "database-connect"
    database_connected = False
    schema_changes_attempted = False
    result = "target-schema-verified"
    engine = None
    try:
        identity = contract.digest(
            {
                "database": make_url(url).database,
                "manifest": contract.digest(manifest),
            }
        )
        key = contract.MIGRATION_PREFIX + identity
        _report_migration(
            reporter,
            stage=stage,
            status="started",
            database_connected=False,
            schema_changes_attempted=False,
        )
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.connect() as connection:
            database_connected = True
            _report_migration(
                reporter,
                stage=stage,
                status="succeeded",
                dialect=connection.dialect.name,
                database_connected=True,
                schema_changes_attempted=False,
            )
            stage = "migration-lock"
            _report_migration(
                reporter,
                stage=stage,
                status="started",
                database_connected=True,
                schema_changes_attempted=False,
            )
            async with migration_lock(connection, url):
                _report_migration(
                    reporter,
                    stage=stage,
                    status="succeeded",
                    database_connected=True,
                    schema_changes_attempted=False,
                )

                def execute(sync):
                    nonlocal result, schema_changes_attempted, stage

                    stage = "schema-inspection"
                    _report_migration(
                        reporter,
                        stage=stage,
                        status="started",
                        database_connected=True,
                        schema_changes_attempted=False,
                    )
                    fingerprint = contract.physical_fingerprint(sync)
                    previous = contract.read_record(sync, key)
                    heads = MigrationContext.configure(sync).get_current_heads()
                    published = contract.read_record(sync, contract.CONTRACT_KEY)
                    if (
                        len(heads) == 1
                        and heads[0] != manifest["head"]
                        and published is not None
                        and contract.accepts_record(published, heads[0])
                        and not contract.physical_errors(sync)
                    ):
                        # A compatible source image may be replayed during rollback.
                        # Leave the newer schema and its published contract intact.
                        sync.rollback()
                        result = "compatible-newer-schema-preserved"
                        return
                    if previous and previous.get("status") in {"RUNNING", "FAILED"}:
                        already_complete = heads == (manifest["head"],) and not contract.physical_errors(sync)
                        if not already_complete and previous.get("source_fingerprint") != fingerprint:
                            raise DatabaseSchemaError(
                                "DATABASE_MIGRATION_REPAIR_REQUIRED",
                                "An interrupted migration changed the physical schema. Inspect and repair it before replaying this operation.",
                            )
                    if len(heads) == 1 and heads[0] != manifest["head"]:
                        known = {r.revision for r in ScriptDirectory.from_config(_alembic_config(url)).walk_revisions()}
                        if heads[0] not in known:
                            raise DatabaseSchemaError(
                                "DATABASE_SCHEMA_TOO_NEW", "Migration cannot downgrade an expanded schema."
                            )
                    record = {
                        "status": "RUNNING",
                        "manifest_digest": contract.digest(manifest),
                        "source_heads": list(heads),
                        "source_fingerprint": fingerprint,
                        "operation_id": operation_id,
                        "error_code": None,
                    }
                    has_store = sa.inspect(sync).has_table("system_config")
                    if has_store:
                        contract.write_record(sync, key, record)
                    sync.commit()
                    try:
                        stage = "schema-upgrade"
                        schema_changes_attempted = True
                        _report_migration(
                            reporter,
                            stage=stage,
                            status="started",
                            source_heads=list(heads),
                            target_head=manifest["head"],
                            database_connected=True,
                            schema_changes_attempted=True,
                        )
                        config = _alembic_config(url)
                        config.attributes["connection"] = sync
                        command.upgrade(config, "head")
                        stage = "schema-validation"
                        _report_migration(
                            reporter,
                            stage=stage,
                            status="started",
                            target_head=manifest["head"],
                            database_connected=True,
                            schema_changes_attempted=True,
                        )
                        errors = contract.physical_errors(sync)
                        if errors:
                            raise DatabaseSchemaError(
                                "DATABASE_SCHEMA_PHYSICAL_STATE_UNKNOWN",
                                "The migrated metadata schema does not match the release contract.",
                            )
                        contract.write_record(sync, contract.CONTRACT_KEY, contract.completed_record())
                        contract.write_record(sync, key, {**record, "status": "SUCCEEDED"})
                        sync.commit()
                    except Exception as error:
                        sync.rollback()
                        if sa.inspect(sync).has_table("system_config"):
                            contract.write_record(
                                sync,
                                key,
                                {
                                    **record,
                                    "status": "FAILED",
                                    "error_code": getattr(error, "code", "DATABASE_MIGRATION_FAILED"),
                                },
                            )
                            sync.commit()
                        raise

                await connection.run_sync(execute)
        _report_migration(
            reporter,
            stage="complete",
            status="succeeded",
            result=result,
            target_head=manifest["head"],
            database_connected=database_connected,
            schema_changes_attempted=schema_changes_attempted,
        )
    except DatabaseSchemaError as error:
        _report_migration(
            reporter,
            stage=stage,
            status="failed",
            error_code=error.code,
            database_connected=database_connected,
            schema_changes_attempted=schema_changes_attempted,
            next_action=_next_action(error.code),
        )
        raise
    except Exception:
        error_code = "DATABASE_MIGRATION_FAILED"
        _report_migration(
            reporter,
            stage=stage,
            status="failed",
            error_code=error_code,
            database_connected=database_connected,
            schema_changes_attempted=schema_changes_attempted,
            next_action=_next_action(error_code),
        )
        raise DatabaseSchemaError(
            error_code,
            "Metadata migration failed. Preserve the migration logs and inspect its physical state before retrying.",
        ) from None
    finally:
        if engine is not None:
            await engine.dispose()


def migrate(
    url: str,
    operation_id: str | None = None,
    reporter: MigrationReporter | None = None,
) -> None:
    asyncio.run(run_migration(url, operation_id, reporter))
