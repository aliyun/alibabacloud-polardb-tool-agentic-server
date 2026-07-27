from __future__ import annotations

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_config
from server.core.crypto import encrypt
from server.core.sql_classifier import classify_sql
from server.logging import normalize_request_id, trace_id_var
from server.models import AuditLog, AuditStatus

logger = logging.getLogger(__name__)


async def log_audit(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    agent_id: str | None = None,
    instance_id: str | None = None,
    action: str,
    sql_text: str | None = None,
    status: AuditStatus,
    error_message: str | None = None,
    duration_ms: int | None = None,
    row_count: int | None = None,
    client_info: str | None = None,
    encryption_key: bytes | None = None,
    user_name: str | None = None,
    instance_name: str | None = None,
    db_name: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    request_id: str | None = None,
    error_code: str | None = None,
    required: bool = False,
    commit: bool = True,
) -> AuditLog | None:
    """Record an audit log entry."""
    config = get_config().sql_security.audit

    if not config.enabled and not required:
        return None

    # Optionally encrypt SQL text
    stored_sql = sql_text
    if sql_text and config.encrypt_sql_text:
        try:
            stored_sql = encrypt(sql_text, key=encryption_key)
        except Exception:
            stored_sql = "[encryption failed]"

    metadata = {
        "sql_text": stored_sql,
        "error_message": error_message,
        "row_count": row_count,
        "client_info": client_info,
        "sql_type": classify_sql(sql_text),
        "user_name": user_name,
        "instance_name": instance_name,
        "db_name": db_name,
    }
    request_id_candidate = request_id or trace_id_var.get("") or None
    normalized_request_id = (
        normalize_request_id(request_id_candidate)
        if request_id_candidate is not None
        else None
    )
    entry = AuditLog(
        actor_user_id=user_id,
        actor_agent_id=agent_id,
        instance_id=instance_id,
        action=action,
        target_type=target_type or ("instance" if instance_id is not None else None),
        target_id=target_id or instance_id,
        status=status,
        request_id=normalized_request_id,
        error_code=error_code,
        duration_ms=duration_ms,
        metadata_json=json.dumps(metadata, separators=(",", ":")),
    )
    session.add(entry)
    if commit:
        await session.commit()
    else:
        await session.flush()
    return entry
