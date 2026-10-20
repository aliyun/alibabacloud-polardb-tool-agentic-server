"""Independent, bounded ETL reads; no directory lifecycle writes."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.crypto import decrypt
from server.db.engine import get_session_factory
from server.enterprise_identity.acl_snapshot import FeishuAclMembershipSnapshotClient, acl_snapshot_client_for
from server.enterprise_identity.providers import acl_membership_snapshot_config
from server.enterprise_identity.service import import_external_knowledge_scopes
from server.models import EnterpriseIdentitySource, IdentitySourceProvider, SystemConfig

TIMEOUT_SECONDS = 120
LEASE_SECONDS = 300


def key(source_id: str) -> str:
    return f"etl_permissions.{source_id}"


def config_digest(source: EnterpriseIdentitySource) -> str:
    return hashlib.sha256((source.config_ciphertext or "").encode()).hexdigest()


def running(value: dict) -> bool:
    until = value.get("lease_until")
    return (
        value.get("state") == "RUNNING" and isinstance(until, str) and datetime.fromisoformat(until) > datetime.now(UTC)
    )


async def status(session: AsyncSession, source_id: str) -> dict:
    row = await session.get(SystemConfig, key(source_id), populate_existing=True)
    value = json.loads(row.config_value) if row else {"connection": {"state": "UNVERIFIED"}, "state": "NEVER"}
    if value.get("state") == "RUNNING" and not running(value):
        value = {**value, "state": "FAILED", "error": "ETL_JOB_EXPIRED"}
        if value.get("operation") == "sync":
            value["sync"] = {**value.get("sync", {}), "state": "FAILED", "error": "ETL_JOB_EXPIRED"}
    return {k: v for k, v in value.items() if k not in {"owner", "config_digest", "lease_until"}}


async def ensure_row(session: AsyncSession, source_id: str) -> SystemConfig:
    """Insert once, without rolling back caller changes on a concurrent insert."""
    values = {
        "config_key": key(source_id),
        "config_value": json.dumps({"state": "NEVER", "connection": {"state": "UNVERIFIED"}}),
        "config_version": 1,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        stmt = insert(SystemConfig).values(**values).on_conflict_do_nothing(index_elements=["config_key"])
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert

        stmt = insert(SystemConfig).values(**values).on_conflict_do_nothing(index_elements=["config_key"])
    else:
        from sqlalchemy.dialects.mysql import insert

        stmt = insert(SystemConfig).values(**values).on_duplicate_key_update(config_key=key(source_id))
    await session.execute(stmt)
    return await session.get(SystemConfig, key(source_id), populate_existing=True)


async def write(session: AsyncSession, row: SystemConfig, value: dict) -> None:
    result = await session.execute(
        update(SystemConfig)
        .where(SystemConfig.config_key == row.config_key, SystemConfig.config_version == row.config_version)
        .values(config_value=json.dumps(value), config_version=row.config_version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(status_code=409, detail="Knowledge permission operation changed concurrently; retry")
    await session.refresh(row)


async def configuration_saved(session: AsyncSession, source: EnterpriseIdentitySource) -> None:
    row = await ensure_row(session, source.id)
    value = json.loads(row.config_value)
    if running(value):
        raise HTTPException(status_code=409, detail="A knowledge permission operation is running")
    await write(
        session,
        row,
        {**value, "connection": {"state": "UNVERIFIED"}, "configuration_updated_at": datetime.now(UTC).isoformat()},
    )


async def claim(session: AsyncSession, source: EnterpriseIdentitySource, operation: str) -> str:
    if source.provider not in {IdentitySourceProvider.FEISHU, IdentitySourceProvider.DINGTALK} or not source.tenant_id or not source.config_ciphertext:
        raise HTTPException(
            status_code=409, detail="A verified Feishu identity source and permission data source are required"
        )
    config = json.loads(decrypt(source.config_ciphertext))
    if not acl_membership_snapshot_config(config):
        raise HTTPException(status_code=409, detail="Knowledge permission data source is not configured")
    row = await ensure_row(session, source.id)
    value = json.loads(row.config_value)
    if running(value):
        raise HTTPException(status_code=409, detail="A knowledge permission operation is running")
    owner = str(uuid.uuid4())
    now = datetime.now(UTC)
    await write(
        session,
        row,
        {
            **value,
            "operation": operation,
            "owner": owner,
            "config_digest": config_digest(source),
            "state": "RUNNING",
            "started_at": now.isoformat(),
            "finished_at": None,
            "error": None,
            "lease_until": (now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
        },
    )
    return owner


async def run(source_id: str, owner: str, *, session_factory=None, client_factory=None) -> None:
    factory = session_factory or get_session_factory()
    client_type = client_factory or FeishuAclMembershipSnapshotClient
    connection_ok = False
    error = "ETL_PERMISSION_SYNC_FAILED"
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            async with factory() as session:
                source = await session.get(EnterpriseIdentitySource, source_id)
                row = await session.get(SystemConfig, key(source_id))
                if source is None or row is None:
                    return
                value = json.loads(row.config_value)
                if value.get("owner") != owner or not running(value):
                    return
                digest = value["config_digest"]
                operation = value["operation"]
                config = acl_membership_snapshot_config(json.loads(decrypt(source.config_ciphertext)))
                tenant_id = (source.tenant_id or "").strip()
                if client_factory is None and source.provider == IdentitySourceProvider.DINGTALK:
                    client_type = acl_snapshot_client_for(source.provider)
                # Release the PAS read transaction during network I/O.
                await session.rollback()
                client = client_type(**config)
                await client.verify_connection(tenant_id=tenant_id)
                connection_ok = True
                scopes = client.iter_knowledge_scopes(tenant_key=tenant_id) if operation == "sync" else None
                source = await session.get(EnterpriseIdentitySource, source_id, populate_existing=True)
                row = await session.get(SystemConfig, key(source_id), populate_existing=True)
                if source is None or row is None:
                    return
                value = json.loads(row.config_value)
                if value.get("owner") != owner or not running(value):
                    return
                if config_digest(source) != digest:
                    error = "ETL_CONFIGURATION_CHANGED"
                    raise ValueError(error)
                # Fence publication and start a physical write transaction before
                # nested scope savepoints (including SQLite legacy transaction mode).
                await write(session, row, value)
                result = None
                if scopes is not None:
                    result = await import_external_knowledge_scopes(
                        session, source, scopes, record_identity_warning=False
                    )
                now = datetime.now(UTC).isoformat()
                await write(
                    session,
                    row,
                    {
                        **value,
                        "state": "PARTIAL" if result and result["snapshots_skipped"] else "SUCCEEDED",
                        "finished_at": now,
                        "lease_until": None,
                        "connection": {"state": "SUCCEEDED", "checked_at": now},
                        **(
                            {
                                "sync": {
                                    "state": "PARTIAL" if result["snapshots_skipped"] else "SUCCEEDED",
                                    "synced_at": now,
                                    **result,
                                }
                            }
                            if result is not None
                            else {}
                        ),
                    },
                )
                await session.commit()
    except Exception:  # noqa: BLE001 - persist a sanitized terminal job result
        # Never persist driver errors: they may contain credentials/connection URLs.
        async with factory() as session:
            row = await session.get(SystemConfig, key(source_id))
            if row is None:
                return
            value = json.loads(row.config_value)
            if value.get("owner") != owner:
                return
            now = datetime.now(UTC).isoformat()
            try:
                await write(
                    session,
                    row,
                    {
                        **value,
                        "state": "FAILED",
                        "finished_at": now,
                        "lease_until": None,
                        "error": error,
                        **(
                            {
                                "sync": {
                                    **value.get("sync", {}),
                                    "state": "FAILED",
                                    "last_attempt_at": now,
                                    "error": error,
                                }
                            }
                            if value.get("operation") == "sync"
                            else {}
                        ),
                        "connection": {"state": "SUCCEEDED" if connection_ok else "FAILED", "checked_at": now},
                    },
                )
                await session.commit()
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                # A newer state/version won the race; never overwrite it.
                await session.rollback()
