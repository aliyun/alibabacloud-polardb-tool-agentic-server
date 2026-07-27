from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import (
    and_,
    case,
    false,
    literal,
    or_,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.principal import Principal, PrincipalKind
from server.core.access_control import (
    EffectiveInstanceAccess,
    resolve_agent_instance_access,
    resolve_user_instance_access,
)
from server.core.credential_policy import (
    direct_access_credential_sql_predicate,
)
from server.core.db_instance_contract import resource_capabilities
from server.core.db_instance_service import (
    DBInstanceServiceError,
    UnsupportedDBType,
)
from server.core.signed_cursor import (
    CursorPayload,
    SignedCursorCodec,
    hash_filters,
)
from server.models import (
    Agent,
    AgentInstanceBinding,
    AgentInstanceBindingCapability,
    AgentStatus,
    AllocationMode,
    BindingCapability,
    BindingOrigin,
    DBInstanceResource,
    DBInstanceStatus,
    Instance,
    InstanceCredential,
    InstanceEngine,
    InstanceStatus,
    User,
    UserInstanceBinding,
    UserInstanceBindingCapability,
    UserStatus,
)

_MANAGEMENT_CAPABILITIES = (
    BindingCapability.DB_INSTANCE_LIST,
    BindingCapability.DB_INSTANCE_DESCRIBE,
    BindingCapability.DB_INSTANCE_CREDENTIALS_READ,
)
_CAPABILITY_NAMES = {
    BindingCapability.DB_INSTANCE_LIST: "list",
    BindingCapability.DB_INSTANCE_DESCRIBE: "describe",
    BindingCapability.DB_INSTANCE_CREDENTIALS_READ: "credentials_read",
    BindingCapability.SQL_READ: "run_sql_read",
    BindingCapability.SQL_WRITE: "run_sql_write",
}
_CAPABILITY_ORDER = {
    name: index
    for index, name in enumerate(
        (
            "list",
            "describe",
            "credentials_read",
            "create",
            "delete",
            "run_sql_read",
            "run_sql_write",
        )
    )
}
_VALID_SOURCES = frozenset(
    {"auto_provisioned", "bound", "provisioned"}
)
_VALID_STATUSES = frozenset(
    {status.value for status in InstanceStatus}
    | {status.value for status in DBInstanceStatus}
)


class InvalidDBInstanceFilter(DBInstanceServiceError):
    code = "INVALID_ARGUMENT"


@dataclass(frozen=True)
class DBInstanceView:
    db_instance_id: str
    name: str | None
    usage: str | None
    db_type: str
    source: str
    status: str
    permission: str | None
    capabilities: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class DBInstancePage:
    instances: list[DBInstanceView]
    has_more: bool
    next_cursor: str | None


def _normalized_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _validated_filters(
    db_type: object,
    source: object,
    status: object,
) -> tuple[str | None, str | None, str | None]:
    if db_type is not None:
        if not isinstance(db_type, str):
            raise UnsupportedDBType("Unsupported database type")
        db_type = db_type.lower()
        if db_type != InstanceEngine.POLARDB_MYSQL.value:
            raise UnsupportedDBType("Unsupported database type")
    if source is not None:
        if not isinstance(source, str):
            raise InvalidDBInstanceFilter("Invalid source filter")
        source = source.lower()
        if source not in _VALID_SOURCES:
            raise InvalidDBInstanceFilter("Invalid source filter")
    if status is not None:
        if not isinstance(status, str):
            raise InvalidDBInstanceFilter("Invalid status filter")
        status = status.lower()
        if status not in _VALID_STATUSES:
            raise InvalidDBInstanceFilter("Invalid status filter")
        status = status.upper()
    return db_type, source, status


def _physical_source_expression():
    return case(
        (
            Instance.allocation_mode.in_(
                (
                    AllocationMode.AUTO_PROVISIONED,
                    AllocationMode.POOLED,
                )
            ),
            literal("auto_provisioned"),
        ),
        else_=literal("bound"),
    )


def _user_rows(
    principal: Principal,
    *,
    db_type: str | None,
    source: str | None,
    status: str | None,
):
    source_expression = _physical_source_expression()
    statement = (
        select(
            literal("physical").label("row_kind"),
            Instance.id.label("db_instance_id"),
            Instance.created_at.label("created_at"),
            source_expression.label("source"),
        )
        .select_from(UserInstanceBinding)
        .join(Instance, Instance.id == UserInstanceBinding.instance_id)
        .join(User, User.id == UserInstanceBinding.user_id)
        .join(
            UserInstanceBindingCapability,
            UserInstanceBindingCapability.binding_id
            == UserInstanceBinding.id,
        )
        .join(
            InstanceCredential,
            InstanceCredential.id == UserInstanceBinding.credential_id,
        )
        .where(
            UserInstanceBinding.user_id == principal.id,
            UserInstanceBinding.enabled.is_(True),
            UserInstanceBinding.origin == BindingOrigin.ADMIN,
            User.status == UserStatus.ACTIVE,
            UserInstanceBindingCapability.capability.in_(
                _MANAGEMENT_CAPABILITIES
            ),
            direct_access_credential_sql_predicate(Instance.id),
        )
        .distinct()
    )
    if db_type is not None:
        statement = statement.where(
            Instance.engine == InstanceEngine.POLARDB_MYSQL
        )
    if source is not None:
        statement = statement.where(
            source_expression == source
            if source in ("auto_provisioned", "bound")
            else false()
        )
    if status is not None:
        try:
            physical_status = InstanceStatus(status.lower())
        except ValueError:
            statement = statement.where(false())
        else:
            statement = statement.where(Instance.status == physical_status)
    return statement


def _agent_rows(
    principal: Principal,
    *,
    db_type: str | None,
    source: str | None,
    status: str | None,
):
    statement = (
        select(
            literal("physical").label("row_kind"),
            Instance.id.label("db_instance_id"),
            Instance.created_at.label("created_at"),
            literal("bound").label("source"),
        )
        .select_from(AgentInstanceBinding)
        .join(Agent, Agent.id == AgentInstanceBinding.agent_id)
        .join(Instance, Instance.id == AgentInstanceBinding.instance_id)
        .join(
            AgentInstanceBindingCapability,
            AgentInstanceBindingCapability.binding_id
            == AgentInstanceBinding.id,
        )
        .join(
            InstanceCredential,
            InstanceCredential.id == AgentInstanceBinding.credential_id,
        )
        .where(
            AgentInstanceBinding.agent_id == principal.id,
            AgentInstanceBinding.enabled.is_(True),
            Agent.status == AgentStatus.ACTIVE,
            AgentInstanceBindingCapability.capability.in_(
                _MANAGEMENT_CAPABILITIES
            ),
            direct_access_credential_sql_predicate(Instance.id),
        )
        .distinct()
    )
    if db_type is not None:
        statement = statement.where(
            Instance.engine == InstanceEngine.POLARDB_MYSQL
        )
    if source is not None and source != "bound":
        statement = statement.where(false())
    if status is not None:
        try:
            physical_status = InstanceStatus(status.lower())
        except ValueError:
            statement = statement.where(false())
        else:
            statement = statement.where(Instance.status == physical_status)
    return statement


def _resource_rows(
    principal: Principal,
    *,
    db_type: str | None,
    source: str | None,
    status: str | None,
):
    statement = (
        select(
            literal("resource").label("row_kind"),
            DBInstanceResource.id.label("db_instance_id"),
            DBInstanceResource.created_at.label("created_at"),
            literal("provisioned").label("source"),
        )
        .select_from(DBInstanceResource)
        .join(Agent, Agent.id == DBInstanceResource.owner_agent_id)
        .where(
            DBInstanceResource.owner_agent_id == principal.id,
            Agent.status == AgentStatus.ACTIVE,
        )
    )
    if db_type is not None:
        statement = statement.where(
            DBInstanceResource.engine == InstanceEngine.POLARDB_MYSQL
        )
    if source is not None and source != "provisioned":
        statement = statement.where(false())
    if status is not None:
        try:
            resource_status = DBInstanceStatus(status.lower())
        except ValueError:
            statement = statement.where(false())
        else:
            statement = statement.where(
                DBInstanceResource.status == resource_status
            )
    else:
        statement = statement.where(
            DBInstanceResource.status != DBInstanceStatus.DELETED
        )
    return statement


def _capability_names(
    access: EffectiveInstanceAccess,
) -> tuple[str, ...]:
    names = {
        _CAPABILITY_NAMES[capability]
        for capability in access.capabilities
        if capability in _CAPABILITY_NAMES
    }
    return tuple(sorted(names, key=_CAPABILITY_ORDER.__getitem__))


async def _physical_view(
    session: AsyncSession,
    principal: Principal,
    db_instance_id: str,
    source: str,
) -> DBInstanceView | None:
    if principal.kind == PrincipalKind.USER:
        access = await resolve_user_instance_access(
            session, principal.id, db_instance_id
        )
    else:
        access = await resolve_agent_instance_access(
            session, principal.id, db_instance_id
        )
    if (
        access is None
        or BindingCapability.DB_INSTANCE_LIST
        not in access.capabilities
    ):
        return None
    instance = access.instance
    return DBInstanceView(
        db_instance_id=instance.id,
        name=instance.name,
        usage=instance.usage,
        db_type=instance.engine.value,
        source=source,
        status=instance.status.value.upper(),
        permission=(
            access.permission.value
            if access.permission is not None
            else None
        ),
        capabilities=_capability_names(access),
        created_at=_normalized_datetime(instance.created_at),
    )


async def _resource_view(
    session: AsyncSession, db_instance_id: str
) -> DBInstanceView | None:
    resource = await session.get(DBInstanceResource, db_instance_id)
    if resource is None:
        return None
    await session.refresh(resource, ["credentials"])
    capabilities = resource_capabilities(resource)
    credential_is_usable = "credentials_read" in capabilities
    return DBInstanceView(
        db_instance_id=resource.id,
        name=resource.name,
        usage=None,
        db_type=resource.engine.value,
        source="provisioned",
        status=resource.status.value.upper(),
        permission=(
            "readwrite"
            if (
                resource.status == DBInstanceStatus.READY
                and credential_is_usable
            )
            else None
        ),
        capabilities=capabilities,
        created_at=_normalized_datetime(resource.created_at),
    )


async def query_db_instances(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = 50,
    db_type: str | None = None,
    source: str | None = None,
    status: str | None = None,
) -> DBInstancePage:
    if isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")

    db_type, source, status = _validated_filters(
        db_type, source, status
    )
    filter_hash = hash_filters(
        db_type=db_type, source=source, status=status
    )
    codec = SignedCursorCodec()
    decoded_cursor = (
        codec.decode(cursor, expected_filter_hash=filter_hash)
        if cursor is not None
        else None
    )

    if principal.kind == PrincipalKind.USER:
        physical = _user_rows(
            principal,
            db_type=db_type,
            source=source,
            status=status,
        )
        resources = _resource_rows(
            principal,
            db_type=db_type,
            source=source,
            status=status,
        ).where(false())
    else:
        physical = _agent_rows(
            principal,
            db_type=db_type,
            source=source,
            status=status,
        )
        resources = _resource_rows(
            principal,
            db_type=db_type,
            source=source,
            status=status,
        )

    accessible = union_all(physical, resources).subquery()
    statement = select(accessible)
    if decoded_cursor is not None:
        cursor_created_at = datetime.fromisoformat(
            decoded_cursor.created_at
        )
        statement = statement.where(
            or_(
                accessible.c.created_at < cursor_created_at,
                and_(
                    accessible.c.created_at == cursor_created_at,
                    accessible.c.db_instance_id
                    < decoded_cursor.db_instance_id,
                ),
            )
        )
    statement = statement.order_by(
        accessible.c.created_at.desc(),
        accessible.c.db_instance_id.desc(),
    ).limit(limit + 1)
    rows = (await session.execute(statement)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    instances: list[DBInstanceView] = []
    for row in rows:
        if row.row_kind == "resource":
            view = await _resource_view(session, row.db_instance_id)
        else:
            view = await _physical_view(
                session,
                principal,
                row.db_instance_id,
                row.source,
            )
        if view is not None:
            instances.append(view)

    next_cursor = None
    if has_more and instances:
        last = instances[-1]
        next_cursor = codec.encode(
            CursorPayload(
                version=1,
                issued_at=int(time.time()),
                created_at=last.created_at.isoformat(),
                db_instance_id=last.db_instance_id,
                filter_hash=filter_hash,
            )
        )
    return DBInstancePage(
        instances=instances,
        has_more=has_more,
        next_cursor=next_cursor,
    )
