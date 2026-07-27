from __future__ import annotations

import asyncio
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_config
from server.core.access_control import resolve_user_instance_access
from server.core.audit_logger import log_audit
from server.core.binding_manager import (
    create_db_account,
    get_accessible_instances,
    get_user_credential,
)
from server.core.crypto import decrypt
from server.core.ddl_hints import DDL_COMMENT_HINT, should_add_comment_hint
from server.core.responses import error_response
from server.core.sql_gateway import _mentions_session_branch
from server.core.sql_executor import (
    RateLimitError,
    SQLExecutionError,
    SQLRiskLevel,
    classify_sql_risk,
    _check_rate_limit,
    has_multiple_statements,
    has_sql_statement,
    is_readonly_sql,
)
from server.models import (
    Agent,
    AuditStatus,
    Instance,
    InstanceStatus,
    InstanceTopology,
    Permission,
    User,
    UserRole,
    UserStatus,
)
from server.core.instance_manager import instance_category
from server.mcp.tools import (
    _decode_cursor,
    get_gateway,
    resolve_target_instance,
)
from server.mcp.tools.agent_sql_access import resolve_agent_sql_access
from server.mcp.tools.identifier import validate_identifier_minimal

logger = logging.getLogger(__name__)

SQLActor = User | Agent


def _is_agent_actor(actor: SQLActor) -> bool:
    return type(actor) is Agent


def _actor_cache_key(actor: SQLActor) -> str:
    return f"agent:{actor.id}" if _is_agent_actor(actor) else actor.id


def _actor_audit_fields(
    actor: SQLActor,
) -> dict[str, str | None]:
    if _is_agent_actor(actor):
        return {"agent_id": actor.id, "user_name": actor.name}
    display_name = actor.display_name
    return {
        "user_id": actor.id,
        "user_name": (display_name if isinstance(display_name, str) else None),
    }


async def _credential_resolution_error(
    user: User,
    target_instance: Instance,
    session: AsyncSession,
    *,
    code: str,
    message: str,
) -> dict:
    """Return and best-effort audit a stable prerequisite error."""
    user_id = user.id
    instance_id = target_instance.id
    try:
        if session.in_transaction():
            await session.rollback()
        await log_audit(
            session,
            user_id=user_id,
            instance_id=instance_id,
            action="sql_credential.resolve",
            status=AuditStatus.ERROR,
            error_code=code,
            error_message=message,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        # The prerequisite error is still returned if optional auditing is
        # unavailable. Never log the audit failure or its raw exception.
        pass
    return error_response(code, message)


async def _resolve_user_sql_credential(
    user: User,
    target_instance: Instance,
    session: AsyncSession,
):
    try:
        access = await resolve_user_instance_access(session, user.id, target_instance.id)
    except asyncio.CancelledError:
        raise
    except Exception:
        return await _credential_resolution_error(
            user,
            target_instance,
            session,
            code="CONNECTION_ERROR",
            message="Database credential resolution failed.",
        )
    if access is None:
        return error_response(
            "INSTANCE_NOT_ACCESSIBLE",
            "You don't have access to this instance.",
        )
    if access.permission is None:
        return error_response(
            "CONNECTION_ERROR",
            "Database credential is unavailable.",
        )
    try:
        resolved = await get_user_credential(session, target_instance.id, user.id)
    except asyncio.CancelledError:
        raise
    except Exception:
        return await _credential_resolution_error(
            user,
            target_instance,
            session,
            code="CONNECTION_ERROR",
            message="Database credential resolution failed.",
        )
    if resolved is None:
        if target_instance.topology == InstanceTopology.MULTITENANT:
            from server.core.tenant_provisioner import ensure_tenant

            try:
                binding = await ensure_tenant(
                    user,
                    target_instance,
                    session,
                    permission=access.permission,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                return await _credential_resolution_error(
                    user,
                    target_instance,
                    session,
                    code="TENANT_PROVISION_FAILED",
                    message="Tenant provisioning failed.",
                )
            try:
                resolved = await get_user_credential(session, target_instance.id, user.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                return await _credential_resolution_error(
                    user,
                    target_instance,
                    session,
                    code="CONNECTION_ERROR",
                    message="Database credential resolution failed.",
                )
            if resolved is None:
                return error_response(
                    "CONNECTION_ERROR",
                    "Tenant credential was not created.",
                )
            return resolved
        try:
            await create_db_account(session, target_instance, user)
            await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            return await _credential_resolution_error(
                user,
                target_instance,
                session,
                code="CONNECTION_ERROR",
                message="Database account provisioning failed.",
            )
        try:
            resolved = await get_user_credential(session, target_instance.id, user.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return await _credential_resolution_error(
                user,
                target_instance,
                session,
                code="CONNECTION_ERROR",
                message="Database credential resolution failed.",
            )
        if resolved is None:
            return error_response(
                "CONNECTION_ERROR",
                "Database credential was not created.",
            )
        return resolved

    binding = resolved.binding
    if target_instance.topology == InstanceTopology.MULTITENANT and binding.provisioning_step is not None:
        from server.core.tenant_provisioner import ensure_tenant

        try:
            binding = await ensure_tenant(
                user,
                target_instance,
                session,
                permission=access.permission,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return await _credential_resolution_error(
                user,
                target_instance,
                session,
                code="TENANT_PROVISION_FAILED",
                message="Tenant provisioning failed.",
            )
        try:
            resolved = await get_user_credential(session, target_instance.id, user.id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return await _credential_resolution_error(
                user,
                target_instance,
                session,
                code="CONNECTION_ERROR",
                message="Database credential resolution failed.",
            )
        if resolved is None:
            return error_response(
                "CONNECTION_ERROR",
                "Tenant credential is unavailable.",
            )
    return resolved


async def handle_list_instances(user: User, session: AsyncSession) -> dict:
    """MCP tool: list_instances"""
    accessible = await get_accessible_instances(session, user)
    is_admin = user.role == UserRole.ADMIN
    instances = []
    for item in accessible:
        inst: Instance = item["instance"]
        if inst.topology == InstanceTopology.MULTITENANT and not is_admin:
            entry = {
                "name": inst.name,
                "type": "database",
                "status": inst.status.value,
            }
        else:
            entry = {
                "instance_id": inst.id,
                "cluster_id": inst.cluster_id,
                "name": inst.name,
                "type": instance_category(inst),
                "status": inst.status.value,
                "access_type": item["access_type"],
                "permission": item["permission"],
            }
        instances.append(entry)
    return {"instances": instances}


async def handle_set_default_instance(user: User, session: AsyncSession, *, instance_id: str) -> dict:
    """MCP tool: set_default_instance"""
    target = (await session.execute(select(Instance).where(Instance.id == instance_id))).scalar_one_or_none()
    if target is None:
        return error_response("INSTANCE_NOT_FOUND", "Instance not found.")

    accessible = await get_accessible_instances(session, user)
    if not any(a["instance"].id == instance_id for a in accessible):
        return error_response("INSTANCE_NOT_ACCESSIBLE", "You don't have access to this instance.")

    user.default_instance_id = instance_id
    await session.commit()

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    {
                        "message": f"Default instance set to '{target.name}'.",
                        "instance_id": target.id,
                        "instance_name": target.name,
                    }
                ),
            }
        ],
    }


async def handle_run_sql(
    user: SQLActor,
    session: AsyncSession,
    *,
    sql: str,
    instance_id: str | None = None,
    database: str | None = None,
    branch: str | None = None,
    max_rows: int = 1000,
    cursor: str | None = None,
    confirm: bool = False,
    session_factory=None,
    background_tasks=None,
) -> dict:
    """MCP tool: run_sql"""
    config = get_config()
    start_time = time.time()

    # Check user status
    if not _is_agent_actor(user) and user.status == UserStatus.DISABLED:
        return error_response("USER_DISABLED", "Your account has been disabled. Contact admin.")

    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        return error_response("INVALID_ARGUMENT", "max_rows must be a positive integer.")

    if not has_sql_statement(sql):
        return error_response("INVALID_ARGUMENT", "sql must contain exactly one SQL statement.")

    if has_multiple_statements(sql):
        return error_response(
            "INVALID_ARGUMENT",
            "run_sql accepts exactly one SQL statement. Use run_sql_transaction for multiple statements.",
        )

    if _is_agent_actor(user) and branch is not None:
        return error_response(
            "INVALID_ARGUMENT",
            "branch is not supported for Agent SQL access. Omit branch and "
            "pass an instance_id returned by list_db_instances.",
        )

    if branch is not None:
        valid_branch = validate_identifier_minimal(branch, "branch")
        if isinstance(valid_branch, dict):
            return valid_branch
        branch = valid_branch
        if _mentions_session_branch(sql):
            await log_audit(
                session,
                action="run_sql",
                sql_text=sql,
                status=AuditStatus.BLOCKED,
                error_message="Branch session override blocked",
                **_actor_audit_fields(user),
            )
            return error_response(
                "BLOCKED_SQL",
                "SQL must not reference @@session.branch when branch is provided.",
            )

    # Rate limit check
    try:
        _check_rate_limit(_actor_cache_key(user))
    except RateLimitError:
        await log_audit(
            session,
            action="run_sql",
            sql_text=sql,
            status=AuditStatus.BLOCKED,
            error_message="Rate limited",
            **_actor_audit_fields(user),
        )
        return error_response("RATE_LIMITED", "Too many requests. Please slow down.")

    # SQL risk classification
    risk = classify_sql_risk(sql)

    if risk == SQLRiskLevel.BLOCKED:
        await log_audit(
            session,
            action="run_sql",
            sql_text=sql,
            status=AuditStatus.BLOCKED,
            error_message="Blocked by security policy",
            **_actor_audit_fields(user),
        )
        return error_response("BLOCKED_SQL", "This SQL statement is not allowed by security policy.")

    if risk == SQLRiskLevel.DESTRUCTIVE and not confirm:
        branch_line = f"Branch: {branch}\n" if branch is not None else ""
        confirm_target = (
            "same SQL, same branch, and confirm=true" if branch is not None else "same SQL and confirm=true"
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "⚠️ DESTRUCTIVE OPERATION DETECTED\n\n"
                        f"SQL: {sql}\n"
                        f"{branch_line}"
                        "Risk: This will permanently modify or delete data.\n\n"
                        f"To proceed, call run_sql again with the {confirm_target}."
                    ),
                }
            ],
        }

    if _is_agent_actor(user):
        credential_result = await resolve_agent_sql_access(
            user,
            session,
            instance_id=instance_id,
            database=database,
        )
        if isinstance(credential_result, dict):
            return credential_result
        target_instance = credential_result.instance
        credential = credential_result.credential
        db_name = credential_result.database
        public_instance_id = credential_result.public_instance_id
        expose_cluster_id = credential_result.source == "bound"
        target_type = (
            "db_instance_resource"
            if credential_result.source == "provisioned"
            else "instance"
        )
    else:
        result = await resolve_target_instance(
            user,
            session,
            instance_id,
            session_factory=session_factory,
            background_tasks=background_tasks,
        )
        if isinstance(result, dict):
            return result
        target_instance, _accessible = result
        if target_instance.status == InstanceStatus.CREATING:
            return error_response(
                "INSTANCE_STARTING",
                "Instance is starting, please retry in a few seconds.",
            )
        credential_result = await _resolve_user_sql_credential(user, target_instance, session)
        if isinstance(credential_result, dict):
            return credential_result
        binding = credential_result.binding
        credential = credential_result.credential
        db_name = database
        public_instance_id = target_instance.id
        expose_cluster_id = True
        target_type = "instance"
        if db_name is None and target_instance.topology == InstanceTopology.MULTITENANT and binding.tenant_name:
            db_name = f"agentic@{binding.tenant_name}"

    if target_instance.status == InstanceStatus.CREATING:
        return error_response(
            "INSTANCE_STARTING",
            "Instance is starting, please retry in a few seconds.",
        )

    # Resolve connection parameters
    if credential.username_ciphertext is None or credential.password_ciphertext is None:
        return error_response("CONNECTION_ERROR", "Database credential is unavailable.")
    username = decrypt(credential.username_ciphertext)
    password = decrypt(credential.password_ciphertext)

    offset = _decode_cursor(cursor) if cursor else 0
    effective_max_rows = min(max_rows, config.sql_security.max_rows)

    permission_value = credential_result.permission.value
    read_only = credential_result.permission == Permission.READONLY

    # Application-level read-only enforcement.
    # READONLY users may only run read-only SQL (SELECT, SHOW, DESCRIBE, EXPLAIN).
    # We enforce this here instead of via SET SESSION TRANSACTION READ ONLY,
    # because that SET causes @@session.transaction_read_only = 1, which
    # misleads AI agents that probe this variable to decide whether they can write.
    if read_only and not is_readonly_sql(sql):
        await log_audit(
            session,
            instance_id=target_instance.id,
            action="run_sql",
            sql_text=sql,
            status=AuditStatus.BLOCKED,
            error_message="Write SQL rejected for READONLY principal",
            instance_name=target_instance.name,
            db_name=db_name,
            target_type=target_type,
            target_id=public_instance_id,
            **_actor_audit_fields(user),
        )
        return error_response(
            "READ_ONLY_ACCESS",
            "This instance binding allows read-only SQL. Use SELECT, SHOW, "
            "DESCRIBE, or EXPLAIN statements, or ask an administrator to "
            "grant readwrite access.",
        )

    try:
        gateway = get_gateway()
        result_data = await gateway.execute(
            host=target_instance.host or "",
            port=target_instance.port or 3306,
            user=username,
            password=password,
            sql=sql,
            database=db_name,
            max_rows=effective_max_rows,
            offset=offset,
            user_id=_actor_cache_key(user),
            instance_id=public_instance_id,
            read_only=read_only,
            branch=branch,
        )
    except SQLExecutionError as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_audit(
            session,
            instance_id=target_instance.id,
            action="run_sql",
            status=AuditStatus.ERROR,
            duration_ms=duration_ms,
            error_message=e.message,
            error_code=e.code,
            target_type=target_type,
            target_id=public_instance_id,
            **_actor_audit_fields(user),
        )
        return error_response(e.code, e.message)
    except asyncio.CancelledError:
        raise
    except Exception:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_audit(
            session,
            instance_id=target_instance.id,
            action="run_sql",
            status=AuditStatus.ERROR,
            duration_ms=duration_ms,
            error_message="Internal SQL execution error",
            error_code="INTERNAL_ERROR",
            target_type=target_type,
            target_id=public_instance_id,
            **_actor_audit_fields(user),
        )
        return error_response("INTERNAL_ERROR", "Internal SQL execution error")

    duration_ms = int((time.time() - start_time) * 1000)
    is_mt_user = (
        not _is_agent_actor(user)
        and target_instance.topology == InstanceTopology.MULTITENANT
        and user.role != UserRole.ADMIN
    )
    if not is_mt_user:
        result_data["instance_id"] = public_instance_id
        if expose_cluster_id:
            result_data["cluster_id"] = target_instance.cluster_id

    # Include permission so the agent knows its access level without
    # needing to probe @@session.transaction_read_only.
    result_data["permission"] = permission_value
    if branch is not None:
        result_data["branch"] = branch

    await log_audit(
        session,
        instance_id=target_instance.id,
        action="run_sql",
        sql_text=sql,
        status=AuditStatus.SUCCESS,
        duration_ms=duration_ms,
        row_count=result_data.get("row_count", 0),
        client_info="confirmed_destructive" if risk == SQLRiskLevel.DESTRUCTIVE else None,
        instance_name=target_instance.name,
        db_name=db_name,
        target_type=target_type,
        target_id=public_instance_id,
        **_actor_audit_fields(user),
    )

    # DDL hint: suggest COMMENT after successful DDL without existing COMMENT
    if should_add_comment_hint(sql):
        result_data["hint"] = DDL_COMMENT_HINT

    return {"content": [{"type": "text", "text": json.dumps(result_data)}]}


async def handle_run_sql_transaction(
    user: SQLActor,
    session: AsyncSession,
    *,
    sql_statements: list[str],
    instance_id: str | None = None,
    database: str | None = None,
    confirm: bool = False,
    session_factory=None,
    background_tasks=None,
) -> dict:
    """MCP tool: run_sql_transaction"""
    start_time = time.time()

    if not _is_agent_actor(user) and user.status == UserStatus.DISABLED:
        return error_response("USER_DISABLED", "Your account has been disabled. Contact admin.")

    if not sql_statements:
        return error_response(
            "INVALID_ARGUMENT",
            "sql_statements must contain at least one SQL statement.",
        )

    if any(not has_sql_statement(stmt) for stmt in sql_statements):
        return error_response(
            "INVALID_ARGUMENT",
            "Each sql_statements item must contain exactly one SQL statement.",
        )

    if any(has_multiple_statements(stmt) for stmt in sql_statements):
        return error_response(
            "INVALID_ARGUMENT",
            "Each sql_statements item must contain exactly one SQL statement.",
        )

    try:
        _check_rate_limit(_actor_cache_key(user))
    except RateLimitError:
        await log_audit(
            session,
            action="run_sql_transaction",
            sql_text="; ".join(sql_statements),
            status=AuditStatus.BLOCKED,
            error_message="Rate limited",
            **_actor_audit_fields(user),
        )
        return error_response("RATE_LIMITED", "Too many requests. Please slow down.")

    destructive_stmts = []
    for i, stmt in enumerate(sql_statements):
        risk = classify_sql_risk(stmt)
        if risk == SQLRiskLevel.BLOCKED:
            await log_audit(
                session,
                action="run_sql_transaction",
                sql_text=stmt,
                status=AuditStatus.BLOCKED,
                error_message=f"Statement {i + 1} blocked by security policy",
                **_actor_audit_fields(user),
            )
            return error_response(
                "BLOCKED_SQL",
                f"Statement {i + 1} is not allowed by security policy: {stmt[:100]}",
            )
        if risk == SQLRiskLevel.DESTRUCTIVE:
            destructive_stmts.append((i + 1, stmt))

    if destructive_stmts and not confirm:
        lines = [f"  {idx}. {stmt}" for idx, stmt in destructive_stmts]
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "⚠️ DESTRUCTIVE OPERATION DETECTED\n\n"
                        "The following statements require confirmation:\n"
                        + "\n".join(lines)
                        + "\n\nTo proceed, call run_sql_transaction again with confirm=true."
                    ),
                }
            ],
        }

    if _is_agent_actor(user):
        credential_result = await resolve_agent_sql_access(
            user,
            session,
            instance_id=instance_id,
            database=database,
        )
        if isinstance(credential_result, dict):
            return credential_result
        target_instance = credential_result.instance
        credential = credential_result.credential
        db_name = credential_result.database
        public_instance_id = credential_result.public_instance_id
        expose_cluster_id = credential_result.source == "bound"
        target_type = (
            "db_instance_resource"
            if credential_result.source == "provisioned"
            else "instance"
        )
    else:
        result = await resolve_target_instance(
            user,
            session,
            instance_id,
            session_factory=session_factory,
            background_tasks=background_tasks,
        )
        if isinstance(result, dict):
            return result
        target_instance, _accessible = result
        if target_instance.status == InstanceStatus.CREATING:
            return error_response(
                "INSTANCE_STARTING",
                "Instance is starting, please retry in a few seconds.",
            )
        credential_result = await _resolve_user_sql_credential(user, target_instance, session)
        if isinstance(credential_result, dict):
            return credential_result
        binding = credential_result.binding
        credential = credential_result.credential
        db_name = database
        public_instance_id = target_instance.id
        expose_cluster_id = True
        target_type = "instance"
        if db_name is None and target_instance.topology == InstanceTopology.MULTITENANT and binding.tenant_name:
            db_name = f"agentic@{binding.tenant_name}"

    if target_instance.status == InstanceStatus.CREATING:
        return error_response(
            "INSTANCE_STARTING",
            "Instance is starting, please retry in a few seconds.",
        )

    if credential.username_ciphertext is None or credential.password_ciphertext is None:
        return error_response("CONNECTION_ERROR", "Database credential is unavailable.")
    username = decrypt(credential.username_ciphertext)
    password = decrypt(credential.password_ciphertext)

    permission_value = credential_result.permission.value
    read_only = credential_result.permission == Permission.READONLY

    # Application-level read-only enforcement for transactions.
    if read_only:
        for i, stmt in enumerate(sql_statements):
            if not is_readonly_sql(stmt):
                await log_audit(
                    session,
                    instance_id=target_instance.id,
                    action="run_sql_transaction",
                    sql_text=stmt,
                    status=AuditStatus.BLOCKED,
                    error_message=(f"Statement {i + 1} rejected for READONLY principal"),
                    instance_name=target_instance.name,
                    db_name=db_name,
                    target_type=target_type,
                    target_id=public_instance_id,
                    **_actor_audit_fields(user),
                )
                return error_response(
                    "READ_ONLY_ACCESS",
                    "This instance binding allows read-only SQL. Use SELECT, "
                    "SHOW, DESCRIBE, or EXPLAIN statements, or ask an "
                    "administrator to grant readwrite access. "
                    f"Statement {i + 1} is not read-only.",
                )

    try:
        gateway = get_gateway()
        results = await gateway.execute_transaction(
            host=target_instance.host or "",
            port=target_instance.port or 3306,
            user=username,
            password=password,
            sql_statements=sql_statements,
            database=db_name,
            user_id=_actor_cache_key(user),
            instance_id=public_instance_id,
            read_only=read_only,
        )
    except SQLExecutionError as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_audit(
            session,
            instance_id=target_instance.id,
            action="run_sql_transaction",
            status=AuditStatus.ERROR,
            duration_ms=duration_ms,
            error_message=e.message,
            error_code=e.code,
            target_type=target_type,
            target_id=public_instance_id,
            **_actor_audit_fields(user),
        )
        return error_response(e.code, e.message)
    except asyncio.CancelledError:
        raise
    except Exception:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_audit(
            session,
            instance_id=target_instance.id,
            action="run_sql_transaction",
            status=AuditStatus.ERROR,
            duration_ms=duration_ms,
            error_message="Internal SQL execution error",
            error_code="INTERNAL_ERROR",
            target_type=target_type,
            target_id=public_instance_id,
            **_actor_audit_fields(user),
        )
        return error_response("INTERNAL_ERROR", "Internal SQL execution error")

    duration_ms = int((time.time() - start_time) * 1000)
    result_data = {"results": results, "statement_count": len(results)}
    is_mt_user = (
        not _is_agent_actor(user)
        and target_instance.topology == InstanceTopology.MULTITENANT
        and user.role != UserRole.ADMIN
    )
    if not is_mt_user:
        result_data["instance_id"] = public_instance_id
        if expose_cluster_id:
            result_data["cluster_id"] = target_instance.cluster_id

    # Include permission so the agent knows its access level.
    result_data["permission"] = permission_value

    has_destructive = bool(destructive_stmts)
    await log_audit(
        session,
        instance_id=target_instance.id,
        action="run_sql_transaction",
        sql_text="; ".join(sql_statements),
        status=AuditStatus.SUCCESS,
        duration_ms=duration_ms,
        row_count=sum(r.get("row_count", 0) for r in results),
        client_info="confirmed_destructive" if has_destructive else None,
        instance_name=target_instance.name,
        db_name=db_name,
        target_type=target_type,
        target_id=public_instance_id,
        **_actor_audit_fields(user),
    )

    return {"content": [{"type": "text", "text": json.dumps(result_data)}]}
