from __future__ import annotations

import json
import logging
import time
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import CallToolResult, TextContent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.auth.principal import (
    Principal,
    PrincipalAuthenticationError,
    PrincipalKind,
    get_current_principal,
)
from server.core.access_control import (
    EffectiveInstanceAccess,
    resolve_agent_instance_access,
    resolve_user_instance_access,
)
from server.core.crypto import decrypt
from server.core.audit_logger import log_audit
from server.core.credential_policy import (
    is_valid_direct_access_credential,
)
from server.core.db_instance_query import (
    DBInstancePage,
    DBInstanceView,
    query_db_instances,
)
from server.core.db_instance_service import (
    DBInstanceNotFound,
    DBInstanceServiceError,
    create_db_instance_resource,
    delete_db_instance_resource,
    describe_db_instance_resource,
)
from server.core.signed_cursor import InvalidCursor
from server.core.tool_rate_limit import (
    RateLimitExceeded,
    check_describe_rate_limit,
    check_list_rate_limit,
    reset_tool_rate_limiters,
)
from server.mcp.authorized_server import (
    has_eligible_provisioning_backend,
)
from server.models import (
    AgentInstanceBinding,
    AllocationMode,
    BindingCapability,
    DBInstanceResource,
    InstanceCredential,
    UserInstanceBinding,
    AuditStatus,
)

logger = logging.getLogger(__name__)


class RequiredDBInstanceAuditUnavailable(Exception):
    pass


def _duration_ms(started_at: float) -> int:
    return max(0, int((time.perf_counter() - started_at) * 1000))


async def _write_db_instance_audit(
    session: AsyncSession,
    principal: Principal,
    *,
    action: str,
    status: AuditStatus,
    started_at: float,
    target_id: str | None = None,
    instance_id: str | None = None,
    error_code: str | None = None,
    required: bool,
    commit: bool,
) -> None:
    user_id = (
        principal.id if principal.kind == PrincipalKind.USER else None
    )
    agent_id = (
        principal.id if principal.kind == PrincipalKind.AGENT else None
    )
    await log_audit(
        session,
        user_id=user_id,
        agent_id=agent_id,
        action=action,
        target_type="db_instance",
        target_id=target_id,
        instance_id=instance_id,
        status=status,
        error_code=error_code,
        duration_ms=_duration_ms(started_at),
        required=required,
        commit=commit,
    )


async def _best_effort_audit(
    session: AsyncSession,
    principal: Principal,
    **kwargs: Any,
) -> None:
    try:
        await _write_db_instance_audit(
            session,
            principal,
            required=False,
            commit=True,
            **kwargs,
        )
    except Exception:
        await session.rollback()
        logger.warning(
            "db instance read audit unavailable",
            extra={"audit_action": kwargs["action"]},
        )


async def _required_error_audit(
    session: AsyncSession,
    principal: Principal,
    **kwargs: Any,
) -> bool:
    try:
        await session.rollback()
        await _write_db_instance_audit(
            session,
            principal,
            required=True,
            commit=True,
            **kwargs,
        )
        return True
    except Exception:
        await session.rollback()
        logger.error(
            "required db instance audit unavailable",
            extra={"audit_action": kwargs["action"]},
        )
        return False


def _audit_unavailable() -> CallToolResult:
    return _error(
        "AUDIT_UNAVAILABLE",
        "Required security audit is unavailable",
    )


def reset_describe_rate_limiters() -> None:
    reset_tool_rate_limiters()


def db_instance_result(
    payload: dict[str, Any], *, is_error: bool = False
) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            )
        ],
        isError=is_error,
    )


def _error(code: str, message: str, **details: Any) -> CallToolResult:
    return db_instance_result(
        {"error": code, "message": message, **details},
        is_error=True,
    )


def _service_error(error: DBInstanceServiceError) -> CallToolResult:
    return _error(error.code, str(error))


async def resolve_request_principal(
    session: AsyncSession,
) -> Principal:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        raise PrincipalAuthenticationError("Authentication required")
    return await get_current_principal(session, access_token.subject)


async def require_agent_principal(
    session: AsyncSession,
) -> Principal:
    principal = await resolve_request_principal(session)
    if principal.kind != PrincipalKind.AGENT:
        raise PrincipalAuthenticationError("Agent authentication required")
    return principal


def _serialize_view(view: DBInstanceView) -> dict[str, Any]:
    return {
        "db_instance_id": view.db_instance_id,
        "name": view.name,
        "usage": view.usage,
        "db_type": view.db_type,
        "source": view.source,
        "status": view.status,
        "permission": view.permission,
        "capabilities": list(view.capabilities),
    }


def serialize_db_instance_page(page: DBInstancePage) -> dict[str, Any]:
    return {
        "instances": [_serialize_view(item) for item in page.instances],
        "has_more": page.has_more,
        "next_cursor": page.next_cursor,
    }


def serialize_resource(resource: DBInstanceResource) -> dict[str, Any]:
    return {
        "db_instance_id": resource.id,
        "name": resource.name,
        "db_type": resource.engine.value,
        "source": "provisioned",
        "status": resource.status.value.upper(),
    }


def _physical_source(access: EffectiveInstanceAccess) -> str:
    return (
        "auto_provisioned"
        if access.instance.allocation_mode
        in (AllocationMode.AUTO_PROVISIONED, AllocationMode.POOLED)
        else "bound"
    )


def _capability_names(
    capabilities: frozenset[BindingCapability],
) -> list[str]:
    mapping = {
        BindingCapability.DB_INSTANCE_LIST: "list",
        BindingCapability.DB_INSTANCE_DESCRIBE: "describe",
        BindingCapability.DB_INSTANCE_CREDENTIALS_READ:
            "credentials_read",
        BindingCapability.SQL_READ: "run_sql_read",
        BindingCapability.SQL_WRITE: "run_sql_write",
    }
    order = {
        name: index
        for index, name in enumerate(
            (
                "list",
                "describe",
                "credentials_read",
                "run_sql_read",
                "run_sql_write",
            )
        )
    }
    names = {
        mapping[capability]
        for capability in capabilities
        if capability in mapping
    }
    return sorted(names, key=order.__getitem__)


def _direct_credential(
    access: EffectiveInstanceAccess,
) -> InstanceCredential | None:
    binding = access.binding
    if not isinstance(
        binding, (UserInstanceBinding, AgentInstanceBinding)
    ):
        return None
    credential = binding.credential
    if (
        credential is None
        or binding.credential_id != credential.id
        or not is_valid_direct_access_credential(
            credential, access.instance.id
        )
    ):
        return None
    return credential


async def _describe_physical(
    session: AsyncSession,
    principal: Principal,
    db_instance_id: str,
) -> dict[str, Any]:
    access = (
        await resolve_user_instance_access(
            session, principal.id, db_instance_id
        )
        if principal.kind == PrincipalKind.USER
        else await resolve_agent_instance_access(
            session, principal.id, db_instance_id
        )
    )
    if (
        access is None
        or BindingCapability.DB_INSTANCE_DESCRIBE
        not in access.capabilities
    ):
        raise DBInstanceNotFound("Database instance not found")
    credential = _direct_credential(access)
    if credential is None:
        raise DBInstanceNotFound("Database instance not found")
    instance = access.instance
    payload: dict[str, Any] = {
        "db_instance_id": instance.id,
        "name": instance.name,
        "usage": instance.usage,
        "db_type": instance.engine.value,
        "source": _physical_source(access),
        "status": instance.status.value.upper(),
        "permission": (
            access.permission.value
            if access.permission is not None
            else None
        ),
        "capabilities": _capability_names(access.capabilities),
    }
    if instance.host is not None:
        payload["host"] = instance.host
    if instance.port is not None:
        payload["port"] = instance.port
    if (
        BindingCapability.DB_INSTANCE_CREDENTIALS_READ
        in access.capabilities
    ):
        username_ciphertext = credential.username_ciphertext
        password_ciphertext = credential.password_ciphertext
        assert username_ciphertext is not None
        assert password_ciphertext is not None
        try:
            payload.update(
                {
                    "database": credential.database_name,
                    "username": decrypt(username_ciphertext),
                    "password": decrypt(password_ciphertext),
                }
            )
        except Exception as error:
            raise DBInstanceNotFound(
                "Database instance not found"
            ) from error
    return payload


async def handle_list_db_instances(
    session: AsyncSession,
    principal: Principal,
    *,
    cursor: str | None = None,
    limit: int = 50,
    db_type: str | None = None,
    source: str | None = None,
    status: str | None = None,
) -> CallToolResult:
    started_at = time.perf_counter()
    try:
        await check_list_rate_limit(principal)
        page = await query_db_instances(
            session,
            principal,
            cursor=cursor,
            limit=limit,
            db_type=db_type,
            source=source,
            status=status,
        )
        result = db_instance_result(serialize_db_instance_page(page))
        await _best_effort_audit(
            session,
            principal,
            action="db_instance.list",
            status=AuditStatus.SUCCESS,
            started_at=started_at,
        )
        return result
    except RateLimitExceeded as error:
        result = _error(
            "RATE_LIMITED",
            "List rate limit exceeded",
            retry_after_seconds=error.retry_after_seconds,
        )
        await _best_effort_audit(
            session,
            principal,
            action="db_instance.list",
            status=AuditStatus.ERROR,
            error_code="RATE_LIMITED",
            started_at=started_at,
        )
        return result
    except InvalidCursor:
        error_code = "INVALID_CURSOR"
        result = _error(error_code, "Invalid cursor")
    except DBInstanceServiceError as error:
        error_code = error.code
        result = _service_error(error)
    except ValueError as error:
        error_code = "INVALID_ARGUMENT"
        result = _error(error_code, str(error))
    await _best_effort_audit(
        session,
        principal,
        action="db_instance.list",
        status=AuditStatus.ERROR,
        error_code=error_code,
        started_at=started_at,
    )
    return result


async def handle_create_db_instance(
    session: AsyncSession,
    principal: Principal,
    *,
    client_token: str,
    db_type: str,
    name: str | None,
) -> CallToolResult:
    started_at = time.perf_counter()
    if principal.kind != PrincipalKind.AGENT:
        if not await _required_error_audit(
            session,
            principal,
            action="db_instance.create",
            status=AuditStatus.ERROR,
            error_code="AUTH_REQUIRED",
            started_at=started_at,
        ):
            return _audit_unavailable()
        return _error("AUTH_REQUIRED", "Agent authentication required.")
    audit_target_id: str | None = None
    try:
        existing_id = (
            await session.execute(
                select(DBInstanceResource.id)
                .where(
                    DBInstanceResource.owner_agent_id == principal.id,
                    DBInstanceResource.client_token == client_token,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        audit_target_id = existing_id
        if (
            existing_id is None
            and not await has_eligible_provisioning_backend(
                session, principal.id
            )
        ):
            if not await _required_error_audit(
                session,
                principal,
                action="db_instance.create",
                status=AuditStatus.ERROR,
                error_code="NO_PROVISIONING_BACKEND",
                started_at=started_at,
            ):
                return _audit_unavailable()
            return _error(
                "NO_PROVISIONING_BACKEND",
                "Agent has no active, healthy provisioning backend",
            )

        async def audit_before_commit(
            audit_session: AsyncSession,
            resource: DBInstanceResource,
        ) -> None:
            try:
                await _write_db_instance_audit(
                    audit_session,
                    principal,
                    action="db_instance.create",
                    target_id=resource.id,
                    status=AuditStatus.SUCCESS,
                    started_at=started_at,
                    required=True,
                    commit=False,
                )
            except Exception as error:
                raise RequiredDBInstanceAuditUnavailable from error

        # Principal resolution and eligibility checks use this session.  End
        # that read transaction before the service enters its write guard;
        # the service reauthorizes the agent and backend under the guard.
        if session.in_transaction():
            await session.rollback()
        resource = await create_db_instance_resource(
            session,
            agent_id=principal.id,
            client_token=client_token,
            db_type=db_type,
            name=name,
            before_commit=audit_before_commit,
        )
        return db_instance_result(serialize_resource(resource))
    except RequiredDBInstanceAuditUnavailable:
        await session.rollback()
        return _audit_unavailable()
    except DBInstanceServiceError as error:
        error_code = error.code
        result = _service_error(error)
    except ValueError as error:
        error_code = "INVALID_ARGUMENT"
        result = _error(error_code, str(error))
    if not await _required_error_audit(
        session,
        principal,
        action="db_instance.create",
        target_id=audit_target_id,
        status=AuditStatus.ERROR,
        error_code=error_code,
        started_at=started_at,
    ):
        return _audit_unavailable()
    return result


async def handle_describe_db_instance(
    session: AsyncSession,
    principal: Principal,
    db_instance_id: str,
) -> CallToolResult:
    started_at = time.perf_counter()
    try:
        await check_describe_rate_limit(principal, db_instance_id)
        resource = await session.get(DBInstanceResource, db_instance_id)
        payload = (
            await describe_db_instance_resource(
                session, principal, db_instance_id
            )
            if resource is not None
            else await _describe_physical(
                session, principal, db_instance_id
            )
        )
        result = db_instance_result(payload)
        await _best_effort_audit(
            session,
            principal,
            action="db_instance.describe",
            target_id=db_instance_id,
            instance_id=(
                db_instance_id if resource is None else None
            ),
            status=AuditStatus.SUCCESS,
            started_at=started_at,
        )
        return result
    except RateLimitExceeded as error:
        result = _error(
            "RATE_LIMITED",
            "Describe rate limit exceeded",
            retry_after_seconds=error.retry_after_seconds,
        )
        error_code = "RATE_LIMITED"
    except DBInstanceServiceError as error:
        error_code = error.code
        result = _service_error(error)
    await _best_effort_audit(
        session,
        principal,
        action="db_instance.describe",
        target_id=db_instance_id,
        status=AuditStatus.ERROR,
        error_code=error_code,
        started_at=started_at,
    )
    return result


async def handle_delete_db_instance(
    session: AsyncSession,
    principal: Principal,
    db_instance_id: str,
) -> CallToolResult:
    started_at = time.perf_counter()
    if principal.kind != PrincipalKind.AGENT:
        if not await _required_error_audit(
            session,
            principal,
            action="db_instance.delete",
            target_id=db_instance_id,
            status=AuditStatus.ERROR,
            error_code="AUTH_REQUIRED",
            started_at=started_at,
        ):
            return _audit_unavailable()
        return _error("AUTH_REQUIRED", "Agent authentication required.")
    try:

        async def audit_before_commit(
            audit_session: AsyncSession,
            resource: DBInstanceResource,
        ) -> None:
            try:
                await _write_db_instance_audit(
                    audit_session,
                    principal,
                    action="db_instance.delete",
                    target_id=resource.id,
                    status=AuditStatus.SUCCESS,
                    started_at=started_at,
                    required=True,
                    commit=False,
                )
            except Exception as error:
                raise RequiredDBInstanceAuditUnavailable from error

        if session.in_transaction():
            await session.rollback()
        resource = await delete_db_instance_resource(
            session,
            principal.id,
            db_instance_id,
            before_commit=audit_before_commit,
        )
        return db_instance_result(serialize_resource(resource))
    except RequiredDBInstanceAuditUnavailable:
        await session.rollback()
        return _audit_unavailable()
    except DBInstanceServiceError as error:
        if not await _required_error_audit(
            session,
            principal,
            action="db_instance.delete",
            target_id=db_instance_id,
            status=AuditStatus.ERROR,
            error_code=error.code,
            started_at=started_at,
        ):
            return _audit_unavailable()
        return _service_error(error)
