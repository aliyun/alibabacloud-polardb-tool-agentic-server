"""Account/resource projections over existing identities and binding tables.

These are administration views, not an alternative authorization engine.
Execution always resolves current effective access through access_control.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict
from server.models import BindingCapability, Permission, UserInstanceBindingCapability, AgentInstanceBindingCapability
from server.core import admin_binding_service

from sqlalchemy import case, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from server.auth.dependencies import require_admin
from server.core.audit_logger import log_audit
from server.db.engine import get_session
from server.models import (
    Agent,
    AgentInstanceBinding,
    AuditStatus,
    BindingOrigin,
    Department,
    DepartmentInstanceBinding,
    Instance,
    User,
    UserDepartment,
    UserInstanceBinding,
)

router = APIRouter(prefix="/access", tags=["access-console"])


async def page(session, query, offset, limit):
    rows = query.subquery()
    total = await session.scalar(select(func.count()).select_from(rows))
    items = (
        (await session.execute(select(rows).order_by(rows.c.name, rows.c.id).offset(offset).limit(limit)))
        .mappings()
        .all()
    )
    return {"items": [dict(row) for row in items], "total": total, "offset": offset, "limit": limit}


@router.get("/accounts")
async def accounts(
    kind: Literal["personal", "service"] = "personal",
    search: str = Query("", max_length=255),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if kind == "personal":
        query = select(
            User.id,
            User.display_name.label("name"),
            User.external_id.label("identity"),
            User.status,
            User.auth_provider.label("authentication"),
            User.role,
        )
        if search:
            query = query.where(
                User.display_name.contains(search, autoescape=True) | User.external_id.contains(search, autoescape=True)
            )
    else:
        query = select(
            Agent.id,
            Agent.name,
            Agent.description.label("identity"),
            Agent.status,
            literal("token").label("authentication"),
            literal("service").label("role"),
        )
        if search:
            query = query.where(Agent.name.contains(search, autoescape=True))
    return await page(session, query, offset, limit)


@router.get("/resources")
async def resources(
    kind: Literal["database", "knowledge"] = "database",
    search: str = Query("", max_length=255),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if kind == "database":
        query = select(
            Instance.id,
            Instance.name,
            literal("database").label("kind"),
            Instance.status,
            Instance.engine.label("type"),
            Instance.usage,
            Instance.allocation_mode.label("source"),
        )
        if search:
            query = query.where(Instance.name.contains(search, autoescape=True))
        return await page(session, query, offset, limit)
    from server.features.knowledge import runtime, KnowledgeUnavailable

    feature = runtime()
    if feature is not None and not await feature.available():
        return {"items": [], "total": 0, "offset": offset, "limit": limit}
    from server.models.polarrag import KnowledgeResource

    k = KnowledgeResource
    query = select(
        k.id,
        k.name,
        literal("knowledge").label("kind"),
        k.sync_status.label("status"),
        k.kb_type.label("type"),
        k.usage,
        k.identity_domain.label("source"),
        k.enabled,
    )
    if search:
        query = query.where(k.name.contains(search, autoescape=True))
    if feature is None:
        return await page(session, query, offset, limit)
    try:
        async with feature.operation("admin_resource_catalog"):
            return await page(session, query, offset, limit)
    except KnowledgeUnavailable:
        return {"items": [], "total": 0, "offset": offset, "limit": limit}


@router.get("/grants")
async def grants(
    resource_id: str | None = None,
    account_kind: Literal["personal", "service"] | None = None,
    account_id: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    if (account_id is None) != (account_kind is None):
        raise HTTPException(422, "Account kind and id must be supplied together")
    u, a, dep = UserInstanceBinding, AgentInstanceBinding, DepartmentInstanceBinding

    def columns(model, kind, owner, name, source, enabled, permission, credential):
        return (
            model.id,
            literal(kind).label("account_kind"),
            owner.label("account_id"),
            name.label("name"),
            model.instance_id.label("resource_id"),
            Instance.name.label("resource_name"),
            source.label("source"),
            enabled.label("enabled"),
            permission.label("permission"),
            credential.label("credential_id"),
        )

    user_query = (
        select(
            *columns(
                u,
                "personal",
                u.user_id,
                User.display_name,
                case((u.origin == BindingOrigin.SYSTEM, "system"), else_="admin"),
                u.enabled,
                u.permission,
                u.credential_id,
            )
        )
        .join(User, User.id == u.user_id)
        .join(Instance, Instance.id == u.instance_id)
    )
    agent_query = (
        select(
            *columns(a, "service", a.agent_id, Agent.name, literal("admin"), a.enabled, a.permission, a.credential_id)
        )
        .join(Agent, Agent.id == a.agent_id)
        .join(Instance, Instance.id == a.instance_id)
    )
    department_query = (
        select(
            *columns(
                dep,
                "group",
                dep.department_id,
                Department.name,
                literal("department"),
                literal(True),
                dep.default_permission,
                literal(None),
            )
        )
        .join(Department, Department.id == dep.department_id)
        .join(Instance, Instance.id == dep.instance_id)
    )
    if account_kind == "personal":
        user_query = user_query.where(u.user_id == account_id)
        department_query = department_query.where(
            dep.department_id.in_(select(UserDepartment.department_id).where(UserDepartment.user_id == account_id))
        )
        queries = [user_query, department_query]
    elif account_kind == "service":
        queries = [agent_query.where(a.agent_id == account_id)]
    else:
        queries = [user_query, agent_query, department_query]
    if resource_id:
        queries = [query.where(Instance.id == resource_id) for query in queries]
    result = await page(session, union_all(*queries), offset, limit)
    for account_type, capability_model in (
        ("personal", UserInstanceBindingCapability),
        ("service", AgentInstanceBindingCapability),
    ):
        direct = [row for row in result["items"] if row["account_kind"] == account_type and row["source"] == "admin"]
        ids = [row["id"] for row in direct]
        capabilities = {}
        if ids:
            for binding_id, capability in await session.execute(
                select(capability_model.binding_id, capability_model.capability).where(
                    capability_model.binding_id.in_(ids)
                )
            ):
                capabilities.setdefault(binding_id, set()).add(capability)
        for row in direct:
            granted = capabilities.get(row["id"], set())
            if BindingCapability.SQL_READ not in granted and BindingCapability.SQL_WRITE not in granted:
                row["permission"] = None
            elif BindingCapability.SQL_WRITE not in granted:
                row["permission"] = Permission.READONLY
    return result


@router.post("/accounts/personal/{user_id}/resources/{instance_id}/disable", status_code=204)
async def disable_personal_access(
    user_id: str,
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    # Keep a deny row: deleting it could unexpectedly restore inherited access.
    row = await session.scalar(
        select(UserInstanceBinding)
        .where(
            UserInstanceBinding.user_id == user_id,
            UserInstanceBinding.instance_id == instance_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Direct personal binding not found; manage inherited access at its group")
    try:
        row.enabled = False
        await log_audit(
            session,
            user_id=admin.id,
            user_name=admin.display_name,
            instance_id=instance_id,
            action="binding.disable",
            status=AuditStatus.SUCCESS,
            target_type="user_instance_binding",
            target_id=row.id,
            required=True,
            commit=False,
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(503, "Access administration unavailable") from exc
    return Response(status_code=204)


@router.post("/accounts/service/{agent_id}/resources/{instance_id}/disable", status_code=204)
async def disable_service_access(
    agent_id: str,
    instance_id: str,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    row = await session.scalar(
        select(AgentInstanceBinding)
        .where(
            AgentInstanceBinding.agent_id == agent_id,
            AgentInstanceBinding.instance_id == instance_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(404, "Direct service binding not found")
    try:
        row.enabled = False
        await log_audit(
            session,
            user_id=admin.id,
            user_name=admin.display_name,
            instance_id=instance_id,
            action="binding.disable",
            status=AuditStatus.SUCCESS,
            target_type="agent_instance_binding",
            target_id=row.id,
            required=True,
            commit=False,
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(503, "Access administration unavailable") from exc
    return Response(status_code=204)


class SQLGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential_id: str
    permission: Permission


@router.put("/accounts/{kind}/{account_id}/resources/{instance_id}")
async def save_sql_grant(
    kind: Literal["personal", "service"],
    account_id: str,
    instance_id: str,
    body: SQLGrantRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """Change the SQL preset without replacing unrelated capability grants."""
    model = UserInstanceBinding if kind == "personal" else AgentInstanceBinding
    account_model = User if kind == "personal" else Agent
    owner = model.user_id if kind == "personal" else model.agent_id
    try:
        account = await session.scalar(select(account_model).where(account_model.id == account_id).with_for_update())
        if account is None:
            raise admin_binding_service.BindingNotFound("Account not found")
        existing = await session.scalar(
            select(model).where(owner == account_id, model.instance_id == instance_id).with_for_update()
        )
        capabilities = set()
        if existing is not None:
            capability_model = UserInstanceBindingCapability if kind == "personal" else AgentInstanceBindingCapability
            # A locking read sees current capability rows even when MySQL's
            # REPEATABLE READ snapshot began during administrator authentication.
            current_rows = list(
                (
                    await session.scalars(
                        select(capability_model)
                        .where(
                            capability_model.binding_id == existing.id,
                        )
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                ).all()
            )
            set_committed_value(existing, "capabilities", current_rows)
            capabilities = {row.capability for row in current_rows}
        capabilities -= {BindingCapability.SQL_READ, BindingCapability.SQL_WRITE}
        capabilities |= {
            BindingCapability.DB_INSTANCE_LIST,
            BindingCapability.DB_INSTANCE_DESCRIBE,
            BindingCapability.SQL_READ,
        }
        if body.permission == Permission.READWRITE:
            capabilities.add(BindingCapability.SQL_WRITE)
        options = dict(
            credential_id=body.credential_id, permission=body.permission, capabilities=capabilities, enabled=True
        )
        if kind == "personal":
            row, created = await admin_binding_service.update_user_instance_access(
                session, user_id=account_id, instance_id=instance_id, **options
            )
        elif existing:
            row = await admin_binding_service.update_agent_instance_binding(
                session, agent_id=account_id, binding_id=existing.id, **options
            )
            created = False
        else:
            row = await admin_binding_service.create_agent_instance_binding(
                session, agent_id=account_id, instance_id=instance_id, admin_id=admin.id, **options
            )
            created = True
        await log_audit(
            session,
            user_id=admin.id,
            user_name=admin.display_name,
            instance_id=instance_id,
            action="binding.create" if created else "binding.update",
            status=AuditStatus.SUCCESS,
            target_type="user_instance_binding" if kind == "personal" else "agent_instance_binding",
            target_id=row.id,
            required=True,
            commit=False,
        )
        await session.commit()
        return {"id": row.id, "enabled": row.enabled}
    except admin_binding_service.BindingNotFound as exc:
        await session.rollback()
        raise HTTPException(404, str(exc)) from exc
    except admin_binding_service.BindingValidationError as exc:
        await session.rollback()
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        await session.rollback()
        raise HTTPException(503, "Access administration unavailable") from exc
