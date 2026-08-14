from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, BeforeValidator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.dependencies import require_admin
from server.db.engine import get_session
from server.models import (
    Agent,
    AuditLog,
    AuditStatus,
    KnowledgeResource,
    PolarRAGInstance,
    PolarRAGSpace,
    User,
)

router = APIRouter(prefix="/audit-logs", tags=["audit"])

SQL_ACTIONS = ("run_sql", "run_sql_transaction")
_FORM_DECODED_OFFSET = re.compile(
    r"^(.*T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s(\d{2}:\d{2})$"
)


def _normalize_created_from(value: object) -> object:
    if isinstance(value, str):
        return _FORM_DECODED_OFFSET.sub(r"\1+\2", value.strip())
    return value


CreatedFrom = Annotated[datetime | None, BeforeValidator(_normalize_created_from)]


class PolarRAGAuditContext(BaseModel):
    instance_ids: list[str]
    instance_names: list[str]
    space_ids: list[str]
    space_names: list[str]
    kb_ids: list[str]
    kb_names: list[str]
    knowledge_resource_ids: list[str]
    knowledge_resource_names: list[str]
    hit_count: int | None = None
    successful_searches: int | None = None
    failed_searches: int | None = None
    partial_failure_count: int = 0
    polarrag_status: str | None = None


class AuditLogResponse(BaseModel):
    id: str
    user_id: str | None
    agent_id: str | None = None
    category: Literal["sql", "polarrag", "other"]
    instance_id: str | None
    action: str
    sql_text: str | None = None
    sql_type: str | None = None
    status: str
    error_message: str | None
    error_code: str | None = None
    duration_ms: int | None
    row_count: int | None
    client_info: str | None
    user_name: str | None = None
    agent_name: str | None = None
    instance_name: str | None = None
    db_name: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    request_id: str | None = None
    polarrag: PolarRAGAuditContext | None = None
    created_at: str


class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int


def _category(action: str) -> Literal["sql", "polarrag", "other"]:
    if action.startswith("polarrag."):
        return "polarrag"
    if action in SQL_ACTIONS:
        return "sql"
    return "other"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str)))


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _parse_client_info(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def _resolve_polarrag_contexts(
    session: AsyncSession,
    client_infos: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    resource_ids = {
        resource_id
        for info in client_infos
        for resource_id in _string_list(info.get("knowledge_resource_ids"))
    }
    if not resource_ids:
        return {}, {}, {}, {}

    rows = (
        await session.execute(
            select(KnowledgeResource, PolarRAGSpace, PolarRAGInstance)
            .join(
                PolarRAGSpace,
                PolarRAGSpace.knowledge_space_id
                == KnowledgeResource.knowledge_space_id,
            )
            .join(
                PolarRAGInstance,
                PolarRAGInstance.id == KnowledgeResource.polarrag_instance_id,
            )
            .where(KnowledgeResource.id.in_(resource_ids))
        )
    ).all()
    resource_names: dict[str, str] = {}
    instance_names: dict[str, str] = {}
    space_names: dict[str, str] = {}
    kb_names: dict[str, str] = {}
    for resource, space, instance in rows:
        resource_names[resource.id] = resource.name
        instance_names[instance.id] = instance.name
        space_names[space.space_id] = space.name
        kb_names[resource.kb_id] = resource.name
    return resource_names, instance_names, space_names, kb_names


def _polarrag_context(
    info: dict[str, Any],
    resource_names: dict[str, str],
    instance_names: dict[str, str],
    space_names: dict[str, str],
    kb_names: dict[str, str],
) -> PolarRAGAuditContext:
    resources = _string_list(info.get("knowledge_resource_ids"))
    instances = _string_list(info.get("polarrag_instance_ids"))
    spaces = _string_list(info.get("space_ids"))
    kbs = _string_list(info.get("kb_ids"))
    return PolarRAGAuditContext(
        instance_ids=instances,
        instance_names=[instance_names.get(item, item) for item in instances],
        space_ids=spaces,
        space_names=[space_names.get(item, item) for item in spaces],
        kb_ids=kbs,
        kb_names=[kb_names.get(item, item) for item in kbs],
        knowledge_resource_ids=resources,
        knowledge_resource_names=[
            resource_names.get(item, item) for item in resources
        ],
        hit_count=_integer(info.get("hit_count")),
        successful_searches=_integer(info.get("successful_searches")),
        failed_searches=_integer(info.get("failed_searches")),
        partial_failure_count=_integer(info.get("partial_failure_count")) or 0,
        polarrag_status=(
            info.get("polarrag_status")
            if isinstance(info.get("polarrag_status"), str)
            else None
        ),
    )


@router.get("", response_model=AuditLogListResponse)
async def list_audit_logs(
    user_id: str | None = None,
    instance_id: str | None = None,
    action: str | None = None,
    status: str | None = None,
    category: Literal["sql", "polarrag"] | None = None,
    created_from: CreatedFrom = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    query = (
        select(AuditLog, User.display_name, Agent.name)
        .outerjoin(User, User.id == AuditLog.actor_user_id)
        .outerjoin(Agent, Agent.id == AuditLog.actor_agent_id)
    )
    count_query = select(func.count()).select_from(AuditLog)

    filters = []
    if user_id:
        filters.append(AuditLog.actor_user_id == user_id)
    if instance_id:
        filters.append(AuditLog.instance_id == instance_id)
    if action:
        filters.append(AuditLog.action == action)
    if category == "polarrag":
        filters.append(AuditLog.action.like("polarrag.%"))
    elif category == "sql":
        filters.append(AuditLog.action.in_(SQL_ACTIONS))
    if created_from:
        filters.append(AuditLog.created_at >= created_from)
    if status:
        try:
            status_enum = AuditStatus(status)
        except ValueError:
            allowed = ", ".join(item.value for item in AuditStatus)
            raise HTTPException(
                status_code=400,
                detail=f"Invalid status: {status}. Must be one of: {allowed}",
            )
        filters.append(AuditLog.status == status_enum)
    if filters:
        query = query.where(*filters)
        count_query = count_query.where(*filters)

    total = (await session.execute(count_query)).scalar() or 0
    rows = (
        await session.execute(
            query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
        )
    ).all()

    parsed: list[tuple[AuditLog, str | None, str | None, dict[str, Any], dict[str, Any]]] = []
    for log, user_name, agent_name in rows:
        try:
            metadata = json.loads(log.metadata_json or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        client_info = _parse_client_info(metadata.get("client_info"))
        parsed.append((log, user_name, agent_name, metadata, client_info))

    resource_names, rag_instance_names, space_names, kb_names = (
        await _resolve_polarrag_contexts(
            session,
            [item[4] for item in parsed if _category(item[0].action) == "polarrag"],
        )
    )

    items: list[AuditLogResponse] = []
    for log, user_name, agent_name, metadata, client_info in parsed:
        item_category = _category(log.action)
        items.append(
            AuditLogResponse(
                id=log.id,
                user_id=log.actor_user_id,
                agent_id=log.actor_agent_id,
                category=item_category,
                instance_id=log.instance_id,
                action=log.action,
                sql_text=metadata.get("sql_text"),
                sql_type=metadata.get("sql_type"),
                status=log.status.value,
                error_message=metadata.get("error_message"),
                error_code=log.error_code,
                duration_ms=log.duration_ms,
                row_count=metadata.get("row_count"),
                client_info=metadata.get("client_info"),
                user_name=user_name or metadata.get("user_name"),
                agent_name=agent_name,
                instance_name=metadata.get("instance_name"),
                db_name=metadata.get("db_name"),
                target_type=log.target_type,
                target_id=log.target_id,
                request_id=log.request_id,
                polarrag=(
                    _polarrag_context(
                        client_info,
                        resource_names,
                        rag_instance_names,
                        space_names,
                        kb_names,
                    )
                    if item_category == "polarrag"
                    else None
                ),
                created_at=log.created_at.isoformat() if log.created_at else "",
            )
        )
    return AuditLogListResponse(items=items, total=total)
