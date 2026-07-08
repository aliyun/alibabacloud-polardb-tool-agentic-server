from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_config
from server.core.crypto import encrypt
from server.core.sql_classifier import classify_sql
from server.models import AuditLog, AuditStatus

logger = logging.getLogger(__name__)


async def log_audit(
    session: AsyncSession,
    *,
    user_id: str,
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
) -> AuditLog | None:
    """Record an audit log entry."""
    config = get_config().sql_security.audit

    if not config.enabled:
        return None

    # Optionally encrypt SQL text
    stored_sql = sql_text
    if sql_text and config.encrypt_sql_text:
        try:
            stored_sql = encrypt(sql_text, key=encryption_key)
        except Exception:
            stored_sql = "[encryption failed]"

    entry = AuditLog(
        user_id=user_id,
        instance_id=instance_id,
        action=action,
        sql_text=stored_sql,
        status=status,
        error_message=error_message,
        duration_ms=duration_ms,
        row_count=row_count,
        client_info=client_info,
        sql_type=classify_sql(sql_text),
        user_name=user_name,
        instance_name=instance_name,
        db_name=db_name,
    )
    session.add(entry)
    await session.commit()
    return entry
