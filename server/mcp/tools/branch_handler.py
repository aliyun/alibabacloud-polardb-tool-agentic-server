from __future__ import annotations

import json
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.audit_logger import log_audit
from server.core.binding_manager import create_db_account
from server.core.crypto import decrypt
from server.core.responses import error_response
from server.core.sql_executor import RateLimitError, SQLExecutionError, _check_rate_limit
from server.models import AuditStatus, DBAccount, Instance, InstanceStatus, InstanceType, User, UserStatus
from server.mcp.tools import get_gateway, resolve_target_instance
from server.mcp.tools.identifier import validate_identifier_minimal


_BRANCH_LIST_MAX_ROWS = 10000


def _disabled_user_error(user: object) -> dict | None:
    if getattr(user, "status", None) == UserStatus.DISABLED:
        return error_response("USER_DISABLED", "Your account has been disabled. Contact admin.")
    return None


def _branch_column_index(columns: list) -> int | None:
    for index, column in enumerate(columns):
        if str(column).strip().lower() == "branch":
            return index
    return None


def _error_payload(result: dict) -> dict | None:
    if not result.get("isError"):
        return None
    try:
        payload = json.loads(result["content"][0]["text"])
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _branch_timeout_unknown(operation: str, branch_name: str) -> dict:
    return error_response(
        "OPERATION_STATUS_UNKNOWN",
        (
            f"{operation} timed out. The operation may still complete on PolarDB; "
            f"call list_branches to verify whether branch '{branch_name}' exists."
        ),
        branch_name=branch_name,
    )


async def _resolve_branch_context(
    user: User,
    session: AsyncSession,
    *,
    instance_id: str | None = None,
    session_factory=None,
    background_tasks=None,
) -> tuple[Instance, DBAccount] | dict:
    if user.status == UserStatus.DISABLED:
        return error_response("USER_DISABLED", "Your account has been disabled. Contact admin.")

    result = await resolve_target_instance(
        user, session, instance_id,
        session_factory=session_factory, background_tasks=background_tasks,
    )
    if isinstance(result, dict):
        return result
    target_instance, _accessible = result

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
    elif target_instance.type == InstanceType.MULTITENANT and db_account.provisioning_step is not None:
        from server.core.tenant_provisioner import ensure_tenant
        try:
            db_account = await ensure_tenant(user, target_instance, session)
        except Exception as e:
            return error_response(
                "TENANT_PROVISION_FAILED",
                f"Tenant provisioning failed at step {db_account.provisioning_step}: {e}",
            )

    return target_instance, db_account


async def _execute_branch_sql(
    user: User,
    session: AsyncSession,
    *,
    sql: str,
    action: str,
    max_rows: int = 1000,
    instance_id: str | None = None,
    session_factory=None,
    background_tasks=None,
    log_success: bool = True,
    read_only: bool = False,
) -> tuple[dict, Instance] | dict:
    if getattr(user, "status", None) == UserStatus.DISABLED:
        return error_response("USER_DISABLED", "Your account has been disabled. Contact admin.")

    try:
        _check_rate_limit(user.id)
    except RateLimitError:
        await log_audit(
            session, user_id=user.id, action=action,
            sql_text=sql, status=AuditStatus.BLOCKED,
            error_message="Rate limited",
            user_name=user.display_name,
        )
        return error_response("RATE_LIMITED", "Too many requests. Please slow down.")

    context = await _resolve_branch_context(
        user, session, instance_id=instance_id,
        session_factory=session_factory, background_tasks=background_tasks,
    )
    if isinstance(context, dict):
        return context
    target_instance, db_account = context

    password = decrypt(db_account.account_password_enc)
    start_time = time.time()
    try:
        result_data = await get_gateway().execute(
            host=target_instance.host or "",
            port=target_instance.port or 3306,
            user=db_account.account_name,
            password=password,
            sql=sql,
            database=None,
            max_rows=max_rows,
            offset=0,
            user_id=user.id,
            instance_id=target_instance.id,
            read_only=read_only,
            branch="",
        )
    except SQLExecutionError as e:
        await log_audit(
            session, user_id=user.id, instance_id=target_instance.id,
            action=action, sql_text=sql, status=AuditStatus.ERROR,
            duration_ms=int((time.time() - start_time) * 1000),
            error_message=str(e),
            user_name=user.display_name, instance_name=target_instance.name,
        )
        return error_response(e.code, e.message)
    except Exception as e:
        await log_audit(
            session, user_id=user.id, instance_id=target_instance.id,
            action=action, sql_text=sql, status=AuditStatus.ERROR,
            duration_ms=int((time.time() - start_time) * 1000),
            error_message=str(e),
            user_name=user.display_name, instance_name=target_instance.name,
        )
        return error_response("SQL_ERROR", str(e))

    if log_success:
        await log_audit(
            session, user_id=user.id, instance_id=target_instance.id,
            action=action, sql_text=sql, status=AuditStatus.SUCCESS,
            duration_ms=int((time.time() - start_time) * 1000),
            row_count=result_data.get("row_count", 0),
            user_name=user.display_name, instance_name=target_instance.name,
        )
    return result_data, target_instance


async def handle_list_branches(
    user: User,
    session: AsyncSession,
    *,
    instance_id: str | None = None,
    session_factory=None,
    background_tasks=None,
) -> dict:
    disabled_error = _disabled_user_error(user)
    if disabled_error is not None:
        return disabled_error

    start_time = time.time()
    result = await _execute_branch_sql(
        user, session, sql="SHOW BRANCHES", action="list_branches",
        instance_id=instance_id, session_factory=session_factory,
        background_tasks=background_tasks, max_rows=_BRANCH_LIST_MAX_ROWS,
        log_success=False, read_only=True,
    )
    if isinstance(result, dict):
        return result
    result_data, target_instance = result

    columns = result_data.get("columns", [])
    rows = result_data.get("rows", [])
    branch_index = _branch_column_index(columns)
    if branch_index is None:
        message = "SHOW BRANCHES result does not include a Branch column."
        await log_audit(
            session, user_id=user.id, instance_id=target_instance.id,
            action="list_branches", sql_text="SHOW BRANCHES", status=AuditStatus.ERROR,
            duration_ms=int((time.time() - start_time) * 1000),
            error_message=message,
            user_name=user.display_name, instance_name=target_instance.name,
        )
        return error_response("UNEXPECTED_RESULT", message)
    branches = []
    for row in rows:
        if len(row) <= branch_index:
            message = "SHOW BRANCHES result row is missing the Branch column."
            await log_audit(
                session, user_id=user.id, instance_id=target_instance.id,
                action="list_branches", sql_text="SHOW BRANCHES", status=AuditStatus.ERROR,
                duration_ms=int((time.time() - start_time) * 1000),
                error_message=message,
                user_name=user.display_name, instance_name=target_instance.name,
            )
            return error_response("UNEXPECTED_RESULT", message)
        branches.append({"branch_name": row[branch_index]})
    await log_audit(
        session, user_id=user.id, instance_id=target_instance.id,
        action="list_branches", sql_text="SHOW BRANCHES", status=AuditStatus.SUCCESS,
        duration_ms=int((time.time() - start_time) * 1000),
        row_count=len(branches),
        user_name=user.display_name, instance_name=target_instance.name,
    )
    payload: dict = {"branches": branches}
    if result_data.get("truncated"):
        payload["truncated"] = True
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


async def handle_create_branch(
    user: User,
    session: AsyncSession,
    *,
    branch_name: str,
    include_databases: list[str] | None = None,
    instance_id: str | None = None,
    session_factory=None,
    background_tasks=None,
) -> dict:
    disabled_error = _disabled_user_error(user)
    if disabled_error is not None:
        return disabled_error

    valid_branch = validate_identifier_minimal(branch_name, "branch_name")
    if isinstance(valid_branch, dict):
        return valid_branch
    branch_name = valid_branch

    if include_databases is None:
        include_databases = []
    elif not isinstance(include_databases, list):
        return error_response("INVALID_IDENTIFIER", "include_databases must be a list.")

    validated_databases = []
    for database in include_databases:
        valid_database = validate_identifier_minimal(database, "include_databases")
        if isinstance(valid_database, dict):
            return valid_database
        validated_databases.append(valid_database)

    sql = f"CREATE BRANCH {branch_name}"
    if validated_databases:
        sql += " WITH DATABASE " + ", ".join(validated_databases)

    result = await _execute_branch_sql(
        user, session, sql=sql, action="create_branch",
        instance_id=instance_id, session_factory=session_factory,
        background_tasks=background_tasks,
    )
    if isinstance(result, dict):
        payload = _error_payload(result)
        if payload and payload.get("error") == "TIMEOUT":
            return _branch_timeout_unknown("CREATE BRANCH", branch_name)
        return result

    payload = {"branch_name": branch_name, "status": "created"}
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


async def handle_delete_branch(
    user: User,
    session: AsyncSession,
    *,
    branch_name: str,
    instance_id: str | None = None,
    session_factory=None,
    background_tasks=None,
) -> dict:
    disabled_error = _disabled_user_error(user)
    if disabled_error is not None:
        return disabled_error

    valid_branch = validate_identifier_minimal(branch_name, "branch_name")
    if isinstance(valid_branch, dict):
        return valid_branch
    branch_name = valid_branch

    result = await _execute_branch_sql(
        user, session, sql=f"DROP BRANCH {branch_name}", action="delete_branch",
        instance_id=instance_id, session_factory=session_factory,
        background_tasks=background_tasks,
    )
    if isinstance(result, dict):
        payload = _error_payload(result)
        if payload and payload.get("error") == "TIMEOUT":
            return _branch_timeout_unknown("DROP BRANCH", branch_name)
        return result

    payload = {"branch_name": branch_name, "status": "deleted"}
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}
