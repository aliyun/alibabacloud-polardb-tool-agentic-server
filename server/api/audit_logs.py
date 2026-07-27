from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.db.engine import get_session
from server.models import AuditLog, AuditStatus, User

router = APIRouter(prefix="/audit-logs", tags=["audit"])


class AuditLogResponse(BaseModel):
    id: str
    user_id: str | None
    agent_id: str | None = None
    instance_id: str | None
    action: str
    sql_text: str | None = None
    sql_type: str | None = None
    status: str
    error_message: str | None
    duration_ms: int | None
    row_count: int | None
    client_info: str | None
    user_name: str | None = None
    instance_name: str | None = None
    db_name: str | None = None
    created_at: str


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int


@router.get("", response_model=AuditLogListResponse)
async def list_audit_logs(
    user_id: str | None = None,
    instance_id: str | None = None,
    action: str | None = None,
    status: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    query = select(AuditLog)
    count_query = select(func.count()).select_from(AuditLog)

    if user_id:
        query = query.where(AuditLog.actor_user_id == user_id)
        count_query = count_query.where(AuditLog.actor_user_id == user_id)
    if instance_id:
        query = query.where(AuditLog.instance_id == instance_id)
        count_query = count_query.where(AuditLog.instance_id == instance_id)
    if action:
        query = query.where(AuditLog.action == action)
        count_query = count_query.where(AuditLog.action == action)
    if status:
        try:
            status_enum = AuditStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}. Must be one of: {', '.join(s.value for s in AuditStatus)}")
        query = query.where(AuditLog.status == status_enum)
        count_query = count_query.where(AuditLog.status == status_enum)

    total = (await session.execute(count_query)).scalar() or 0
    result = await session.execute(
        query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
    )
    logs = result.scalars().all()

    items: list[AuditLogResponse] = []
    for log in logs:
        try:
            metadata = json.loads(log.metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        items.append(
            AuditLogResponse(
                id=log.id,
                user_id=log.actor_user_id,
                agent_id=log.actor_agent_id,
                instance_id=log.instance_id,
                action=log.action,
                sql_text=metadata.get("sql_text"),
                sql_type=metadata.get("sql_type"),
                status=log.status.value,
                error_message=metadata.get("error_message"),
                duration_ms=log.duration_ms,
                row_count=metadata.get("row_count"),
                client_info=metadata.get("client_info"),
                user_name=metadata.get("user_name"),
                instance_name=metadata.get("instance_name"),
                db_name=metadata.get("db_name"),
                created_at=log.created_at.isoformat() if log.created_at else "",
            )
        )
    return AuditLogListResponse(items=items, total=total)
