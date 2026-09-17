"""Versioned schema compatibility, independent of the application's latest head."""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import sqlalchemy as sa

CONTRACT_KEY = "schema.compatibility"
MIGRATION_PREFIX = "schema.migration."


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("schema_manifest.json").read_text())


def read_record(connection, key: str) -> dict | None:
    if not sa.inspect(connection).has_table("system_config"):
        return None
    raw = connection.execute(
        sa.text("SELECT config_value FROM system_config WHERE config_key=:key"), {"key": key}
    ).scalar_one_or_none()
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {"invalid": True}
    return value if isinstance(value, dict) else {"invalid": True}


def write_record(connection, key: str, value: dict) -> None:
    from datetime import datetime, timezone
    from server.models.system_config import SystemConfig

    table = SystemConfig.__table__
    old = connection.execute(sa.select(table.c.config_version).where(table.c.config_key == key)).scalar_one_or_none()
    values = {"config_value": json.dumps(value, sort_keys=True), "updated_at": datetime.now(timezone.utc)}
    if old is None:
        connection.execute(
            sa.insert(table).values(config_key=key, config_version=1, created_at=datetime.now(timezone.utc), **values)
        )
    else:
        result = connection.execute(
            sa.update(table)
            .where(table.c.config_key == key, table.c.config_version == old)
            .values(config_version=old + 1, **values)
        )
        if result.rowcount != 1:
            raise RuntimeError("schema record changed concurrently")


def column_type(value) -> tuple[str, int | None]:
    if isinstance(value, sa.Boolean):
        return "boolean", None
    if isinstance(value, sa.BigInteger):
        return "bigint", None
    if isinstance(value, sa.Integer):
        return "integer", None
    if isinstance(value, sa.DateTime):
        return "datetime", None
    if isinstance(value, sa.Text) or getattr(value, "__visit_name__", "").upper() in {
        "LONGTEXT",
        "MEDIUMTEXT",
        "TINYTEXT",
    }:
        return "text", None
    if isinstance(value, sa.String):
        return "string", value.length
    return str(value).lower(), None


def normalize_default(value) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    while result.startswith("(") and result.endswith(")"):
        result = result[1:-1].strip()
    # PostgreSQL reflection includes scalar text casts in server defaults.
    cast = re.fullmatch(r"('(?:[^']|'')*')::(?:character varying|text|bpchar)", result, flags=re.IGNORECASE)
    if cast:
        result = cast.group(1)
    if result.startswith("'") and result.endswith("'"):
        return result[1:-1].replace("''", "'")
    expression = result.lower()
    if expression in {"true", "1"}:
        return "1"
    if expression in {"false", "0"}:
        return "0"
    if expression in {"current_timestamp", "current_timestamp()"}:
        return "current_timestamp"
    return result


def physical_errors(connection) -> list[str]:
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    errors = []
    for name, required in manifest()["tables"].items():
        if name not in tables:
            errors.append(f"missing table: {name}")
            continue
        actual = {c["name"]: c for c in inspector.get_columns(name)}
        for key, requirement in required.items():
            column = actual.get(key)
            if column is None:
                errors.append(f"missing column: {name}.{key}")
                continue
            kind, length = column_type(column["type"])
            allowed = {requirement["type"]}
            if requirement["type"] == "boolean":
                allowed.update({"integer", "tinyint"})
            if kind not in allowed or length != requirement["length"]:
                errors.append(f"column type: {name}.{key}")
            if bool(column["nullable"]) != requirement["nullable"]:
                errors.append(f"column nullability: {name}.{key}")
            if "default" in requirement and normalize_default(column["default"]) != requirement["default"]:
                errors.append(f"column default: {name}.{key}")
    return errors


def accepts_record(record: dict, current: str) -> bool:
    required = manifest()
    return (
        type(record.get("protocol_version")) is int
        and type(record.get("generation")) is int
        and record.get("protocol_version") == required["protocol_version"]
        and record.get("generation") == required["generation"]
        and record.get("baseline_head") == required["baseline_head"]
        and record.get("completed_head") == current
        and type(record.get("completed_sequence")) is int
        and record["completed_sequence"] >= required["required_sequence"]
        and isinstance(record.get("compatible_manifests"), list)
        and digest(required) in record["compatible_manifests"]
    )


def completed_record() -> dict:
    required = manifest()
    compatible = required.get("compatible_manifest_digests", [])
    if not isinstance(compatible, list) or not all(isinstance(value, str) for value in compatible):
        raise ValueError("compatible_manifest_digests must be a list of strings")
    return {
        "protocol_version": required["protocol_version"],
        "generation": required["generation"],
        "baseline_head": required["baseline_head"],
        "completed_head": required["head"],
        "completed_sequence": required["required_sequence"],
        "compatible_manifests": list(dict.fromkeys([*compatible, digest(required)])),
    }


def physical_fingerprint(connection) -> str:
    inspector = sa.inspect(connection)
    snapshot = {}
    for table in sorted(inspector.get_table_names()):
        snapshot[table] = {
            "columns": [{**c, "type": str(c["type"])} for c in inspector.get_columns(table)],
            "indexes": inspector.get_indexes(table),
            "foreign_keys": inspector.get_foreign_keys(table),
            "unique": inspector.get_unique_constraints(table),
            "checks": inspector.get_check_constraints(table),
        }
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()
