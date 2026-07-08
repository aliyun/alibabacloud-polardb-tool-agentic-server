from __future__ import annotations

import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import get_config
from server.core.audit_logger import log_audit
from server.core.binding_manager import create_db_account, get_accessible_instances
from server.core.crypto import decrypt
from server.core.ddl_hints import DDL_COMMENT_HINT, should_add_comment_hint
from server.core.responses import error_response
from server.core.sql_gateway import _mentions_session_branch
from server.core.sql_executor import (
    RateLimitError, SQLExecutionError, SQLRiskLevel,
    classify_sql_risk, _check_rate_limit,
    has_multiple_statements, has_sql_statement, is_readonly_sql,
)
from server.models import (
    AuditStatus, DBAccount, Instance, User,
    UserRole, UserStatus, InstanceType, InstanceStatus,
)
from server.mcp.tools import (
    _decode_cursor,
    _get_user_permission,
    get_gateway,
    resolve_target_instance,
)
from server.mcp.tools.identifier import validate_identifier_minimal

logger = logging.getLogger(__name__)


async def handle_list_instances(user: User, session: AsyncSession) -> dict:
    """MCP tool: list_instances"""
    accessible = await get_accessible_instances(session, user)
    is_admin = user.role == UserRole.ADMIN
    instances = []
    for item in accessible:
        inst: Instance = item["instance"]
        if inst.type == InstanceType.MULTITENANT and not is_admin:
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
                "type": inst.type.value,
                "status": inst.status.value,
                "access_type": item["access_type"],
                "permission": item["permission"],
            }
        instances.append(entry)
    return {"instances": instances}


async def handle_set_default_instance(
    user: User, session: AsyncSession, *, instance_id: str
) -> dict:
    """MCP tool: set_default_instance"""
    target = (await session.execute(
        select(Instance).where(Instance.id == instance_id)
    )).scalar_one_or_none()
    if target is None:
        return error_response("INSTANCE_NOT_FOUND", "Instance not found.")

    accessible = await get_accessible_instances(session, user)
    if not any(a["instance"].id == instance_id for a in accessible):
        return error_response("INSTANCE_NOT_ACCESSIBLE", "You don't have access to this instance.")

    user.default_instance_id = instance_id
    await session.commit()

    return {
        "content": [{
            "type": "text",
            "text": json.dumps({
                "message": f"Default instance set to '{target.name}'.",
                "instance_id": target.id,
                "instance_name": target.name,
            }),
        }],
    }


async def handle_run_sql(
    user: User,
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
    if user.status == UserStatus.DISABLED:
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

    if branch is not None:
        valid_branch = validate_identifier_minimal(branch, "branch")
        if isinstance(valid_branch, dict):
            return valid_branch
        branch = valid_branch
        if _mentions_session_branch(sql):
            await log_audit(
                session, user_id=user.id, action="run_sql",
                sql_text=sql, status=AuditStatus.BLOCKED,
                error_message="Branch session override blocked",
                user_name=user.display_name,
            )
            return error_response(
                "BLOCKED_SQL",
                "SQL must not reference @@session.branch when branch is provided.",
            )

    # Rate limit check
    try:
        _check_rate_limit(user.id)
    except RateLimitError:
        await log_audit(
            session, user_id=user.id, action="run_sql",
            sql_text=sql, status=AuditStatus.BLOCKED,
            error_message="Rate limited",
            user_name=user.display_name,
        )
        return error_response("RATE_LIMITED", "Too many requests. Please slow down.")

    # SQL risk classification
    risk = classify_sql_risk(sql)

    if risk == SQLRiskLevel.BLOCKED:
        await log_audit(
            session, user_id=user.id, action="run_sql",
            sql_text=sql, status=AuditStatus.BLOCKED,
            error_message="Blocked by security policy",
            user_name=user.display_name,
        )
        return error_response("BLOCKED_SQL", "This SQL statement is not allowed by security policy.")

    if risk == SQLRiskLevel.DESTRUCTIVE and not confirm:
        branch_line = f"Branch: {branch}\n" if branch is not None else ""
        confirm_target = "same SQL, same branch, and confirm=true" if branch is not None else "same SQL and confirm=true"
        return {
            "content": [{
                "type": "text",
                "text": (
                    "⚠️ DESTRUCTIVE OPERATION DETECTED\n\n"
                    f"SQL: {sql}\n"
                    f"{branch_line}"
                    "Risk: This will permanently modify or delete data.\n\n"
                    f"To proceed, call run_sql again with the {confirm_target}."
                ),
            }],
        }

    # Resolve instance
    result = await resolve_target_instance(
        user, session, instance_id,
        session_factory=session_factory, background_tasks=background_tasks,
    )
    if isinstance(result, dict):
        return result
    target_instance, accessible = result

    if target_instance.status == InstanceStatus.CREATING:
        return error_response("INSTANCE_STARTING", "Instance is starting, please retry in a few seconds.")

    # Get DB account
    db_account = (await session.execute(
        select(DBAccount).where(
            DBAccount.instance_id == target_instance.id,
            DBAccount.user_id == user.id,
        )
    )).scalar_one_or_none()

    if db_account is None:
        if target_instance.type == InstanceType.MULTITENANT:
            from server.core.tenant_provisioner import ensure_tenant
            try:
                db_account = await ensure_tenant(user, target_instance, session)
            except Exception as e:
                return error_response("TENANT_PROVISION_FAILED",
                    f"Tenant provisioning failed: {e}")
        else:
            try:
                db_account = await create_db_account(session, target_instance, user)
                await session.commit()
            except Exception as e:
                return error_response("CONNECTION_ERROR", f"Failed to create database account: {e}")
    elif (target_instance.type == InstanceType.MULTITENANT
          and db_account.provisioning_step is not None):
        from server.core.tenant_provisioner import ensure_tenant
        try:
            db_account = await ensure_tenant(user, target_instance, session)
        except Exception as e:
            return error_response("TENANT_PROVISION_FAILED",
                f"Tenant provisioning failed at step {db_account.provisioning_step}: {e}")

    # Resolve connection parameters
    password = decrypt(db_account.account_password_enc)
    db_name = database
    if db_name is None and target_instance.type == InstanceType.MULTITENANT and db_account.tenant_name:
        db_name = f"agentic@{db_account.tenant_name}"

    offset = _decode_cursor(cursor) if cursor else 0
    effective_max_rows = min(max_rows, config.sql_security.max_rows)

    permission_value = _get_user_permission(accessible, target_instance)
    from server.models import Permission
    read_only = (permission_value == Permission.READONLY.value)

    # Application-level read-only enforcement.
    # READONLY users may only run read-only SQL (SELECT, SHOW, DESCRIBE, EXPLAIN).
    # We enforce this here instead of via SET SESSION TRANSACTION READ ONLY,
    # because that SET causes @@session.transaction_read_only = 1, which
    # misleads AI agents that probe this variable to decide whether they can write.
    if read_only and not is_readonly_sql(sql):
        await log_audit(
            session, user_id=user.id, instance_id=target_instance.id,
            action="run_sql", sql_text=sql, status=AuditStatus.BLOCKED,
            error_message="Write SQL rejected for READONLY user",
            user_name=user.display_name, instance_name=target_instance.name, db_name=db_name,
        )
        return error_response(
            "READ_ONLY_ACCESS",
            "Your access is read-only. Write operations (INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, etc.) are not allowed.",
        )

    try:
        gateway = get_gateway()
        result_data = await gateway.execute(
            host=target_instance.host or "",
            port=target_instance.port or 3306,
            user=db_account.account_name,
            password=password,
            sql=sql,
            database=db_name,
            max_rows=effective_max_rows,
            offset=offset,
            user_id=user.id,
            instance_id=target_instance.id,
            read_only=read_only,
            branch=branch,
        )
    except SQLExecutionError as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_audit(
            session, user_id=user.id, instance_id=target_instance.id,
            action="run_sql", sql_text=sql, status=AuditStatus.ERROR,
            duration_ms=duration_ms, error_message=str(e),
            user_name=user.display_name, instance_name=target_instance.name, db_name=db_name,
        )
        return error_response(e.code, e.message)
    except Exception as e:
        if branch is None:
            raise
        duration_ms = int((time.time() - start_time) * 1000)
        await log_audit(
            session, user_id=user.id, instance_id=target_instance.id,
            action="run_sql", sql_text=sql, status=AuditStatus.ERROR,
            duration_ms=duration_ms, error_message=str(e),
            user_name=user.display_name, instance_name=target_instance.name, db_name=db_name,
        )
        return error_response("SQL_ERROR", str(e))

    duration_ms = int((time.time() - start_time) * 1000)
    is_mt_user = target_instance.type == InstanceType.MULTITENANT and user.role != UserRole.ADMIN
    if not is_mt_user:
        result_data["instance_id"] = target_instance.id
        result_data["cluster_id"] = target_instance.cluster_id

    # Include permission so the agent knows its access level without
    # needing to probe @@session.transaction_read_only.
    result_data["permission"] = permission_value
    if branch is not None:
        result_data["branch"] = branch

    await log_audit(
        session, user_id=user.id, instance_id=target_instance.id,
        action="run_sql", sql_text=sql, status=AuditStatus.SUCCESS,
        duration_ms=duration_ms, row_count=result_data.get("row_count", 0),
        client_info="confirmed_destructive" if risk == SQLRiskLevel.DESTRUCTIVE else None,
        user_name=user.display_name, instance_name=target_instance.name, db_name=db_name,
    )

    # DDL hint: suggest COMMENT after successful DDL without existing COMMENT
    if should_add_comment_hint(sql):
        result_data["hint"] = DDL_COMMENT_HINT

    return {"content": [{"type": "text", "text": json.dumps(result_data)}]}


async def handle_run_sql_transaction(
    user: User,
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

    if user.status == UserStatus.DISABLED:
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
        _check_rate_limit(user.id)
    except RateLimitError:
        await log_audit(
            session, user_id=user.id, action="run_sql_transaction",
            sql_text="; ".join(sql_statements), status=AuditStatus.BLOCKED,
            error_message="Rate limited",
            user_name=user.display_name,
        )
        return error_response("RATE_LIMITED", "Too many requests. Please slow down.")

    destructive_stmts = []
    for i, stmt in enumerate(sql_statements):
        risk = classify_sql_risk(stmt)
        if risk == SQLRiskLevel.BLOCKED:
            await log_audit(
                session, user_id=user.id, action="run_sql_transaction",
                sql_text=stmt, status=AuditStatus.BLOCKED,
                error_message=f"Statement {i+1} blocked by security policy",
                user_name=user.display_name,
            )
            return error_response(
                "BLOCKED_SQL",
                f"Statement {i+1} is not allowed by security policy: {stmt[:100]}",
            )
        if risk == SQLRiskLevel.DESTRUCTIVE:
            destructive_stmts.append((i + 1, stmt))

    if destructive_stmts and not confirm:
        lines = [f"  {idx}. {stmt}" for idx, stmt in destructive_stmts]
        return {
            "content": [{
                "type": "text",
                "text": (
                    "⚠️ DESTRUCTIVE OPERATION DETECTED\n\n"
                    "The following statements require confirmation:\n"
                    + "\n".join(lines)
                    + "\n\nTo proceed, call run_sql_transaction again with confirm=true."
                ),
            }],
        }

    result = await resolve_target_instance(
        user, session, instance_id,
        session_factory=session_factory, background_tasks=background_tasks,
    )
    if isinstance(result, dict):
        return result
    target_instance, accessible = result

    if target_instance.status == InstanceStatus.CREATING:
        return error_response("INSTANCE_STARTING", "Instance is starting, please retry in a few seconds.")

    db_account = (await session.execute(
        select(DBAccount).where(
            DBAccount.instance_id == target_instance.id,
            DBAccount.user_id == user.id,
        )
    )).scalar_one_or_none()

    if db_account is None:
        if target_instance.type == InstanceType.MULTITENANT:
            from server.core.tenant_provisioner import ensure_tenant
            try:
                db_account = await ensure_tenant(user, target_instance, session)
            except Exception as e:
                return error_response("TENANT_PROVISION_FAILED", f"Tenant provisioning failed: {e}")
        else:
            try:
                db_account = await create_db_account(session, target_instance, user)
                await session.commit()
            except Exception as e:
                return error_response("CONNECTION_ERROR", f"Failed to create database account: {e}")
    elif (target_instance.type == InstanceType.MULTITENANT
          and db_account.provisioning_step is not None):
        from server.core.tenant_provisioner import ensure_tenant
        try:
            db_account = await ensure_tenant(user, target_instance, session)
        except Exception as e:
            return error_response("TENANT_PROVISION_FAILED",
                f"Tenant provisioning failed at step {db_account.provisioning_step}: {e}")

    password = decrypt(db_account.account_password_enc)
    db_name = database
    if db_name is None and target_instance.type == InstanceType.MULTITENANT and db_account.tenant_name:
        db_name = f"agentic@{db_account.tenant_name}"

    permission_value = _get_user_permission(accessible, target_instance)
    from server.models import Permission
    read_only = (permission_value == Permission.READONLY.value)

    # Application-level read-only enforcement for transactions.
    if read_only:
        for i, stmt in enumerate(sql_statements):
            if not is_readonly_sql(stmt):
                await log_audit(
                    session, user_id=user.id, instance_id=target_instance.id,
                    action="run_sql_transaction", sql_text=stmt,
                    status=AuditStatus.BLOCKED,
                    error_message=f"Statement {i+1} rejected for READONLY user",
                    user_name=user.display_name, instance_name=target_instance.name, db_name=db_name,
                )
                return error_response(
                    "READ_ONLY_ACCESS",
                    f"Your access is read-only. Statement {i+1} is a write operation and is not allowed: {stmt[:100]}",
                )

    try:
        gateway = get_gateway()
        results = await gateway.execute_transaction(
            host=target_instance.host or "",
            port=target_instance.port or 3306,
            user=db_account.account_name,
            password=password,
            sql_statements=sql_statements,
            database=db_name,
            user_id=user.id,
            instance_id=target_instance.id,
            read_only=read_only,
        )
    except SQLExecutionError as e:
        duration_ms = int((time.time() - start_time) * 1000)
        await log_audit(
            session, user_id=user.id, instance_id=target_instance.id,
            action="run_sql_transaction", sql_text="; ".join(sql_statements),
            status=AuditStatus.ERROR, duration_ms=duration_ms, error_message=str(e),
            user_name=user.display_name, instance_name=target_instance.name, db_name=db_name,
        )
        return error_response(e.code, e.message)

    duration_ms = int((time.time() - start_time) * 1000)
    result_data = {"results": results, "statement_count": len(results)}
    is_mt_user = target_instance.type == InstanceType.MULTITENANT and user.role != UserRole.ADMIN
    if not is_mt_user:
        result_data["instance_id"] = target_instance.id
        result_data["cluster_id"] = target_instance.cluster_id

    # Include permission so the agent knows its access level.
    result_data["permission"] = permission_value

    has_destructive = bool(destructive_stmts)
    await log_audit(
        session, user_id=user.id, instance_id=target_instance.id,
        action="run_sql_transaction", sql_text="; ".join(sql_statements),
        status=AuditStatus.SUCCESS, duration_ms=duration_ms,
        row_count=sum(r.get("row_count", 0) for r in results),
        client_info="confirmed_destructive" if has_destructive else None,
        user_name=user.display_name, instance_name=target_instance.name, db_name=db_name,
    )

    return {"content": [{"type": "text", "text": json.dumps(result_data)}]}
