import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from server.core.responses import error_response
from server.core.sql_executor import RateLimitError, SQLExecutionError, reset_rate_limiters
from server.models import AuditStatus
from server.mcp.tools.branch_handler import (
    _execute_branch_sql,
    _resolve_branch_context,
    handle_create_branch,
    handle_delete_branch,
    handle_list_branches,
)


pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clean_rate_limiters():
    reset_rate_limiters()
    yield
    reset_rate_limiters()


def _user():
    return SimpleNamespace(id="uid", display_name="User")


def _target_instance():
    return SimpleNamespace(id="iid", name="inst")


async def test_resolve_branch_context_returns_instance_resolution_error():
    user = SimpleNamespace(id="uid", display_name="User", status=None)
    session = object()
    expected = error_response("INSTANCE_NOT_ACCESSIBLE", "You don't have access to this instance.")

    with patch(
        "server.mcp.tools.branch_handler.resolve_target_instance",
        new_callable=AsyncMock,
        return_value=expected,
    ):
        result = await _resolve_branch_context(user, session, instance_id="iid")

    assert result == expected


async def test_resolve_branch_context_rejects_starting_instance_before_account_lookup():
    from server.models import InstanceStatus

    user = SimpleNamespace(id="uid", display_name="User", status=None)
    session = SimpleNamespace(execute=AsyncMock())
    target_instance = SimpleNamespace(id="iid", status=InstanceStatus.CREATING)

    with patch(
        "server.mcp.tools.branch_handler.resolve_target_instance",
        new_callable=AsyncMock,
        return_value=(target_instance, []),
    ):
        result = await _resolve_branch_context(user, session, instance_id="iid")

    session.execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "error": "INSTANCE_STARTING",
        "message": "Instance is starting, please retry in a few seconds.",
    }


async def test_resolve_branch_context_wraps_account_creation_failure():
    from server.models import InstanceStatus, InstanceType

    user = SimpleNamespace(id="uid", display_name="User", status=None)
    result_proxy = SimpleNamespace(scalar_one_or_none=lambda: None)
    session = SimpleNamespace(execute=AsyncMock(return_value=result_proxy), commit=AsyncMock())
    target_instance = SimpleNamespace(
        id="iid", status=InstanceStatus.ACTIVE, type=InstanceType.SHARED,
    )

    with (
        patch(
            "server.mcp.tools.branch_handler.resolve_target_instance",
            new_callable=AsyncMock,
            return_value=(target_instance, []),
        ),
        patch(
            "server.mcp.tools.branch_handler.create_db_account",
            new_callable=AsyncMock,
            side_effect=RuntimeError("create failed"),
        ) as create_account,
    ):
        result = await _resolve_branch_context(user, session, instance_id="iid")

    create_account.assert_awaited_once()
    session.commit.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "error": "CONNECTION_ERROR",
        "message": "Failed to create database account: create failed",
    }


async def test_resolve_branch_context_ensures_multitenant_account_when_missing():
    from server.models import InstanceStatus, InstanceType

    user = SimpleNamespace(id="uid", display_name="User", status=None)
    result_proxy = SimpleNamespace(scalar_one_or_none=lambda: None)
    session = SimpleNamespace(execute=AsyncMock(return_value=result_proxy))
    target_instance = SimpleNamespace(
        id="iid", status=InstanceStatus.ACTIVE, type=InstanceType.MULTITENANT,
    )
    db_account = SimpleNamespace(account_name="tenant_user", provisioning_step=None)

    with (
        patch(
            "server.mcp.tools.branch_handler.resolve_target_instance",
            new_callable=AsyncMock,
            return_value=(target_instance, []),
        ),
        patch(
            "server.core.tenant_provisioner.ensure_tenant",
            new_callable=AsyncMock,
            return_value=db_account,
        ) as ensure_tenant,
    ):
        result = await _resolve_branch_context(user, session, instance_id="iid")

    ensure_tenant.assert_awaited_once_with(user, target_instance, session)
    assert result == (target_instance, db_account)


async def test_resolve_branch_context_resumes_incomplete_multitenant_account():
    from server.models import InstanceStatus, InstanceType
    from server.models.db_account import TenantProvisioningStep

    user = SimpleNamespace(id="uid", display_name="User", status=None)
    db_account = SimpleNamespace(
        account_name="tenant_user",
        provisioning_step=TenantProvisioningStep.TENANT,
    )
    result_proxy = SimpleNamespace(scalar_one_or_none=lambda: db_account)
    session = SimpleNamespace(execute=AsyncMock(return_value=result_proxy))
    target_instance = SimpleNamespace(
        id="iid", status=InstanceStatus.ACTIVE, type=InstanceType.MULTITENANT,
    )
    completed_account = SimpleNamespace(account_name="tenant_user", provisioning_step=None)

    with (
        patch(
            "server.mcp.tools.branch_handler.resolve_target_instance",
            new_callable=AsyncMock,
            return_value=(target_instance, []),
        ),
        patch(
            "server.core.tenant_provisioner.ensure_tenant",
            new_callable=AsyncMock,
            return_value=completed_account,
        ) as ensure_tenant,
    ):
        result = await _resolve_branch_context(user, session, instance_id="iid")

    ensure_tenant.assert_awaited_once_with(user, target_instance, session)
    assert result == (target_instance, completed_account)


async def test_list_branches_maps_show_branches_rows():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": ["Branch"], "rows": [["MAIN"], ["br1"]]}, _target_instance()),
    ) as execute, patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock):
        result = await handle_list_branches(_user(), object(), instance_id="iid")

    execute.assert_awaited_once()
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "branches": [
            {"branch_name": "MAIN"},
            {"branch_name": "br1"},
        ],
    }
    assert execute.await_args.kwargs["max_rows"] == 10000


async def test_list_branches_exposes_truncated_result():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=(
            {"columns": ["Branch"], "rows": [["MAIN"]], "truncated": True},
            _target_instance(),
        ),
    ), patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock):
        result = await handle_list_branches(_user(), object(), instance_id="iid")

    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "branches": [{"branch_name": "MAIN"}],
        "truncated": True,
    }


async def test_list_branches_finds_branch_column_case_insensitively():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": ["Database", " branch "], "rows": [["app", "br1"]]}, _target_instance()),
    ), patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock):
        result = await handle_list_branches(_user(), object(), instance_id="iid")

    payload = json.loads(result["content"][0]["text"])
    assert payload == {"branches": [{"branch_name": "br1"}]}


async def test_list_branches_rejects_unexpected_columns():
    target_instance = _target_instance()
    user = _user()
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": ["Database"], "rows": [["app"]]}, target_instance),
    ), patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock) as log:
        result = await handle_list_branches(user, object(), instance_id="iid")

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "UNEXPECTED_RESULT"
    assert log.await_args.kwargs["status"] == AuditStatus.ERROR
    assert log.await_args.kwargs["error_message"] == "SHOW BRANCHES result does not include a Branch column."


async def test_list_branches_rejects_missing_column_metadata():
    target_instance = _target_instance()
    user = _user()
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": [], "rows": [["MAIN"]]}, target_instance),
    ), patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock) as log:
        result = await handle_list_branches(user, object(), instance_id="iid")

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "UNEXPECTED_RESULT"
    assert log.await_args.kwargs["status"] == AuditStatus.ERROR
    assert log.await_args.kwargs["error_message"] == "SHOW BRANCHES result does not include a Branch column."


async def test_list_branches_rejects_rows_missing_branch_column():
    target_instance = _target_instance()
    user = _user()
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": ["Database", "Branch"], "rows": [["app"]]}, target_instance),
    ), patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock) as log:
        result = await handle_list_branches(user, object(), instance_id="iid")

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "UNEXPECTED_RESULT"
    assert log.await_args.kwargs["status"] == AuditStatus.ERROR
    assert log.await_args.kwargs["error_message"] == "SHOW BRANCHES result row is missing the Branch column."


async def test_list_branches_logs_success_after_parsing():
    target_instance = _target_instance()
    user = _user()
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": ["Branch"], "rows": [["MAIN"], ["br1"]]}, target_instance),
    ), patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock) as log:
        result = await handle_list_branches(user, object(), instance_id="iid")

    payload = json.loads(result["content"][0]["text"])
    assert payload == {"branches": [{"branch_name": "MAIN"}, {"branch_name": "br1"}]}
    assert log.await_args.kwargs["status"] == AuditStatus.SUCCESS
    assert log.await_args.kwargs["row_count"] == 2


async def test_branch_tools_do_not_echo_instance_metadata():
    gateway_result = {
        "columns": ["Branch"],
        "rows": [["MAIN"]],
        "instance_id": "iid",
        "cluster_id": "cid",
    }
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=(gateway_result, _target_instance()),
    ), patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock):
        list_result = await handle_list_branches(_user(), object(), instance_id="iid")

    payload = json.loads(list_result["content"][0]["text"])
    assert payload == {"branches": [{"branch_name": "MAIN"}]}
    assert "instance_id" not in payload
    assert "cluster_id" not in payload

    command_result = {
        "columns": [],
        "rows": [],
        "instance_id": "iid",
        "cluster_id": "cid",
    }
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=(command_result, _target_instance()),
    ):
        create_result = await handle_create_branch(
            object(), object(), branch_name="br_new", instance_id="iid",
        )
        delete_result = await handle_delete_branch(
            object(), object(), branch_name="br_old", instance_id="iid",
        )

    for result in (create_result, delete_result):
        payload = json.loads(result["content"][0]["text"])
        assert "instance_id" not in payload
        assert "cluster_id" not in payload


async def test_execute_branch_sql_preserves_structured_sql_error():
    user = SimpleNamespace(id="uid", display_name="User")
    session = object()
    target_instance = SimpleNamespace(id="iid", host="h", port=3306, name="inst")
    db_account = SimpleNamespace(account_name="u", account_password_enc="enc")
    gateway = SimpleNamespace(execute=AsyncMock(side_effect=SQLExecutionError("Query timed out", "TIMEOUT")))

    with (
        patch(
            "server.mcp.tools.branch_handler._resolve_branch_context",
            new_callable=AsyncMock,
            return_value=(target_instance, db_account),
        ),
        patch("server.mcp.tools.branch_handler.decrypt", return_value="pw"),
        patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway),
        patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock),
    ):
        result = await _execute_branch_sql(user, session, sql="CREATE BRANCH br1", action="create_branch")

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"error": "TIMEOUT", "message": "Query timed out"}
    assert "instance_id" not in payload
    assert "cluster_id" not in payload


async def test_execute_branch_sql_wraps_generic_connection_error():
    user = SimpleNamespace(id="uid", display_name="User")
    session = object()
    target_instance = SimpleNamespace(id="iid", host="h", port=3306, name="inst")
    db_account = SimpleNamespace(account_name="u", account_password_enc="enc")
    gateway = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("Lost connection to MySQL server")))

    with (
        patch(
            "server.mcp.tools.branch_handler._resolve_branch_context",
            new_callable=AsyncMock,
            return_value=(target_instance, db_account),
        ),
        patch("server.mcp.tools.branch_handler.decrypt", return_value="pw"),
        patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway),
        patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock) as log,
    ):
        result = await _execute_branch_sql(user, session, sql="CREATE BRANCH br1", action="create_branch")

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"error": "SQL_ERROR", "message": "Lost connection to MySQL server"}
    assert "instance_id" not in payload
    assert "cluster_id" not in payload
    assert log.await_args.kwargs["status"] == AuditStatus.ERROR
    assert log.await_args.kwargs["error_message"] == "Lost connection to MySQL server"


async def test_execute_branch_sql_rate_limits_before_resolving_context():
    user = SimpleNamespace(id="uid", display_name="User")
    session = object()

    with (
        patch("server.mcp.tools.branch_handler._check_rate_limit", side_effect=RateLimitError()),
        patch(
            "server.mcp.tools.branch_handler._resolve_branch_context",
            new_callable=AsyncMock,
        ) as resolve_context,
        patch("server.mcp.tools.branch_handler.get_gateway") as get_gateway,
        patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock) as log,
    ):
        result = await _execute_branch_sql(user, session, sql="CREATE BRANCH br1", action="create_branch")

    resolve_context.assert_not_awaited()
    get_gateway.assert_not_called()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"error": "RATE_LIMITED", "message": "Too many requests. Please slow down."}
    assert log.await_args.kwargs["status"] == AuditStatus.BLOCKED
    assert log.await_args.kwargs["error_message"] == "Rate limited"


async def test_execute_branch_sql_rejects_disabled_user_before_rate_limit():
    from server.models import UserStatus

    user = SimpleNamespace(id="uid", display_name="User", status=UserStatus.DISABLED)
    session = object()

    with (
        patch("server.mcp.tools.branch_handler._check_rate_limit", side_effect=RateLimitError()) as check_rate,
        patch(
            "server.mcp.tools.branch_handler._resolve_branch_context",
            new_callable=AsyncMock,
        ) as resolve_context,
        patch("server.mcp.tools.branch_handler.get_gateway") as get_gateway,
        patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock) as log,
    ):
        result = await _execute_branch_sql(user, session, sql="CREATE BRANCH br1", action="create_branch")

    check_rate.assert_not_called()
    resolve_context.assert_not_awaited()
    get_gateway.assert_not_called()
    log.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "error": "USER_DISABLED",
        "message": "Your account has been disabled. Contact admin.",
    }


async def test_create_branch_rejects_disabled_user_before_identifier_validation():
    from server.models import UserStatus

    user = SimpleNamespace(id="uid", display_name="User", status=UserStatus.DISABLED)

    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            user, object(), branch_name="bad;name", include_databases=["also;bad"],
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "error": "USER_DISABLED",
        "message": "Your account has been disabled. Contact admin.",
    }


async def test_delete_branch_rejects_disabled_user_before_identifier_validation():
    from server.models import UserStatus

    user = SimpleNamespace(id="uid", display_name="User", status=UserStatus.DISABLED)

    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_delete_branch(user, object(), branch_name="bad;name")

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {
        "error": "USER_DISABLED",
        "message": "Your account has been disabled. Contact admin.",
    }


async def test_execute_branch_sql_delegates_permissions_to_database():
    user = SimpleNamespace(id="uid", display_name="User")
    session = object()
    target_instance = SimpleNamespace(id="iid", host="h", port=3306, name="inst")
    db_account = SimpleNamespace(account_name="u", account_password_enc="enc")
    gateway = SimpleNamespace(execute=AsyncMock(return_value={"columns": [], "rows": [], "row_count": 0}))

    with (
        patch(
            "server.mcp.tools.branch_handler._resolve_branch_context",
            new_callable=AsyncMock,
            return_value=(target_instance, db_account),
        ),
        patch("server.mcp.tools.branch_handler.decrypt", return_value="pw"),
        patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway),
        patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock),
    ):
        result = await _execute_branch_sql(user, session, sql="CREATE BRANCH br1", action="create_branch")

    assert "isError" not in result
    gateway.execute.assert_awaited_once()
    assert gateway.execute.await_args.kwargs["read_only"] is False
    assert gateway.execute.await_args.kwargs["branch"] == ""


async def test_execute_branch_sql_marks_list_as_read_only():
    user = SimpleNamespace(id="uid", display_name="User")
    session = object()
    target_instance = SimpleNamespace(id="iid", host="h", port=3306, name="inst")
    db_account = SimpleNamespace(account_name="u", account_password_enc="enc")
    gateway = SimpleNamespace(execute=AsyncMock(return_value={
        "columns": ["Branch"],
        "rows": [("MAIN",)],
        "row_count": 1,
        "truncated": False,
    }))

    with (
        patch(
            "server.mcp.tools.branch_handler._resolve_branch_context",
            new_callable=AsyncMock,
            return_value=(target_instance, db_account),
        ),
        patch("server.mcp.tools.branch_handler.decrypt", return_value="pw"),
        patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway),
        patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock),
    ):
        result = await _execute_branch_sql(
            user, session, sql="SHOW BRANCHES", action="list_branches",
            log_success=False, read_only=True,
        )

    assert result == (
        {"columns": ["Branch"], "rows": [("MAIN",)], "row_count": 1, "truncated": False},
        target_instance,
    )
    assert gateway.execute.await_args.kwargs["read_only"] is True
    assert gateway.execute.await_args.kwargs["branch"] == ""


async def test_execute_branch_sql_logs_success_audit_fields():
    user = SimpleNamespace(id="uid", display_name="User")
    session = object()
    target_instance = SimpleNamespace(id="iid", host="h", port=3306, name="inst")
    db_account = SimpleNamespace(account_name="u", account_password_enc="enc")
    gateway = SimpleNamespace(execute=AsyncMock(return_value={"columns": [], "rows": [], "row_count": 0}))

    with (
        patch(
            "server.mcp.tools.branch_handler._resolve_branch_context",
            new_callable=AsyncMock,
            return_value=(target_instance, db_account),
        ),
        patch("server.mcp.tools.branch_handler.decrypt", return_value="pw"),
        patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway),
        patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock) as log,
    ):
        result = await _execute_branch_sql(user, session, sql="DROP BRANCH br1", action="delete_branch")

    assert result == ({"columns": [], "rows": [], "row_count": 0}, target_instance)
    assert log.await_args.kwargs["instance_id"] == "iid"
    assert log.await_args.kwargs["action"] == "delete_branch"
    assert log.await_args.kwargs["sql_text"] == "DROP BRANCH br1"
    assert log.await_args.kwargs["status"] == AuditStatus.SUCCESS
    assert log.await_args.kwargs["row_count"] == 0
    assert log.await_args.kwargs["user_name"] == "User"
    assert log.await_args.kwargs["instance_name"] == "inst"


async def test_execute_branch_sql_forces_default_branch_session():
    user = SimpleNamespace(id="uid", display_name="User")
    session = object()
    target_instance = SimpleNamespace(id="iid", host="h", port=3306, name="inst")
    db_account = SimpleNamespace(account_name="u", account_password_enc="enc")
    gateway = SimpleNamespace(execute=AsyncMock(return_value={"columns": [], "rows": [], "row_count": 0}))

    with (
        patch(
            "server.mcp.tools.branch_handler._resolve_branch_context",
            new_callable=AsyncMock,
            return_value=(target_instance, db_account),
        ),
        patch("server.mcp.tools.branch_handler.decrypt", return_value="pw"),
        patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway),
        patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock),
    ):
        await _execute_branch_sql(user, session, sql="CREATE BRANCH br1", action="create_branch")

    gateway.execute.assert_awaited_once()
    assert gateway.execute.await_args.kwargs["database"] is None
    assert gateway.execute.await_args.kwargs["branch"] == ""


async def test_execute_branch_sql_can_skip_success_audit():
    user = SimpleNamespace(id="uid", display_name="User")
    session = object()
    target_instance = SimpleNamespace(id="iid", host="h", port=3306, name="inst")
    db_account = SimpleNamespace(account_name="u", account_password_enc="enc")
    gateway = SimpleNamespace(execute=AsyncMock(return_value={"columns": ["Branch"], "rows": [["MAIN"]]}))

    with (
        patch(
            "server.mcp.tools.branch_handler._resolve_branch_context",
            new_callable=AsyncMock,
            return_value=(target_instance, db_account),
        ),
        patch("server.mcp.tools.branch_handler.decrypt", return_value="pw"),
        patch("server.mcp.tools.branch_handler.get_gateway", return_value=gateway),
        patch("server.mcp.tools.branch_handler.log_audit", new_callable=AsyncMock) as log,
    ):
        result = await _execute_branch_sql(
            user, session, sql="SHOW BRANCHES", action="list_branches",
            log_success=False,
        )

    assert result == ({"columns": ["Branch"], "rows": [["MAIN"]]}, target_instance)
    log.assert_not_awaited()


async def test_create_branch_generates_create_branch_sql():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": [], "rows": []}, object()),
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="br_new",
            include_databases=None,
            instance_id="iid",
        )

    assert execute.await_args.kwargs["sql"] == "CREATE BRANCH br_new"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"branch_name": "br_new", "status": "created"}


async def test_create_branch_empty_include_databases_omits_with_clause():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": [], "rows": []}, object()),
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="br_new",
            include_databases=[],
            instance_id="iid",
        )

    assert execute.await_args.kwargs["sql"] == "CREATE BRANCH br_new"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"branch_name": "br_new", "status": "created"}


async def test_create_branch_preserves_branch_name_before_sql_and_output():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": [], "rows": []}, object()),
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="Br_New",
            include_databases=None,
            instance_id="iid",
        )

    assert execute.await_args.kwargs["sql"] == "CREATE BRANCH Br_New"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"branch_name": "Br_New", "status": "created"}


async def test_create_branch_generates_with_database_sql():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": [], "rows": []}, object()),
    ) as execute:
        await handle_create_branch(
            object(), object(),
            branch_name="br_new",
            include_databases=["db1", "db2"],
            instance_id="iid",
        )

    assert execute.await_args.kwargs["sql"] == "CREATE BRANCH br_new WITH DATABASE db1, db2"


async def test_create_branch_rejects_minimal_injection_chars():
    result = await handle_create_branch(
        object(), object(),
        branch_name="br1;DROP",
        include_databases=None,
    )

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


@pytest.mark.parametrize(
    "branch_name",
    [
        "br,other",
        "br`x",
        "br'x",
        'br"x',
        "br#x",
        "br/*x*/",
        "br--x",
        "br x",
    ],
)
async def test_create_branch_rejects_structural_identifier_chars_before_sql(branch_name):
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name=branch_name,
            include_databases=None,
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


async def test_create_branch_rejects_empty_branch_name_before_sql():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="",
            include_databases=None,
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


async def test_create_branch_rejects_overlong_branch_name_before_sql():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="b" * 257,
            include_databases=None,
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


async def test_create_branch_rejects_non_string_branch_name_before_sql():
    invalid_branch_name: Any = 123
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name=invalid_branch_name,
            include_databases=None,
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


async def test_create_branch_rejects_invalid_include_database_before_sql():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="br1",
            include_databases=["db1", "bad;db"],
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


@pytest.mark.parametrize(
    "database_name",
    [
        "db,other",
        "db`x",
        "db'x",
        'db"x',
        "db#x",
        "db/*x*/",
        "db--x",
        "db x",
    ],
)
async def test_create_branch_rejects_structural_include_database_chars_before_sql(database_name):
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="br1",
            include_databases=["db1", database_name],
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


async def test_create_branch_rejects_non_list_include_databases_before_sql():
    invalid_include_databases: Any = "db1"
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="br1",
            include_databases=invalid_include_databases,
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


async def test_create_branch_rejects_blank_include_database_before_sql():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="br1",
            include_databases=["db1", "  "],
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


async def test_create_branch_rejects_non_string_include_database_before_sql():
    invalid_include_databases: Any = ["db1", 123]
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_create_branch(
            object(), object(),
            branch_name="br1",
            include_databases=invalid_include_databases,
        )

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


async def test_create_branch_returns_sql_error():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=error_response("SQL_ERROR", "Branch already exists"),
    ):
        result = await handle_create_branch(
            object(), object(),
            branch_name="br_existing",
            include_databases=None,
        )

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"error": "SQL_ERROR", "message": "Branch already exists"}


async def test_create_branch_preserves_structured_sql_error():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=error_response("SQL_ERROR", "Branch already exists"),
    ):
        result = await handle_create_branch(
            object(), object(),
            branch_name="br_existing",
            include_databases=None,
        )

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"error": "SQL_ERROR", "message": "Branch already exists"}


async def test_create_branch_timeout_returns_unknown_status():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=error_response(
            "TIMEOUT",
            "Query timed out",
            instance_id="iid",
            cluster_id="cid",
        ),
    ):
        result = await handle_create_branch(
            object(), object(),
            branch_name="br_slow",
            include_databases=None,
        )

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "OPERATION_STATUS_UNKNOWN"
    assert payload["branch_name"] == "br_slow"
    assert "list_branches" in payload["message"]
    assert "instance_id" not in payload
    assert "cluster_id" not in payload


async def test_delete_branch_generates_drop_branch_sql():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": [], "rows": []}, object()),
    ) as execute:
        result = await handle_delete_branch(
            object(), object(),
            branch_name="br_old",
            instance_id="iid",
        )

    assert execute.await_args.kwargs["sql"] == "DROP BRANCH br_old"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"branch_name": "br_old", "status": "deleted"}


async def test_delete_branch_preserves_branch_name_before_sql_and_output():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": [], "rows": []}, object()),
    ) as execute:
        result = await handle_delete_branch(
            object(), object(),
            branch_name="Br_Old",
            instance_id="iid",
        )

    assert execute.await_args.kwargs["sql"] == "DROP BRANCH Br_Old"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"branch_name": "Br_Old", "status": "deleted"}


@pytest.mark.parametrize("branch_name", ["MAIN", "main"])
async def test_delete_branch_delegates_main_branch_to_sql(branch_name):
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=({"columns": [], "rows": []}, object()),
    ) as execute:
        result = await handle_delete_branch(object(), object(), branch_name=branch_name)

    assert execute.await_args.kwargs["sql"] == f"DROP BRANCH {branch_name}"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"branch_name": branch_name, "status": "deleted"}


async def test_delete_branch_rejects_empty_branch_name_before_sql():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
    ) as execute:
        result = await handle_delete_branch(object(), object(), branch_name="")

    execute.assert_not_awaited()
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "INVALID_IDENTIFIER"


async def test_delete_branch_returns_sql_error():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=error_response("SQL_ERROR", "Branch does not exist"),
    ):
        result = await handle_delete_branch(
            object(), object(),
            branch_name="br_missing",
        )

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"error": "SQL_ERROR", "message": "Branch does not exist"}


async def test_delete_branch_timeout_returns_unknown_status():
    with patch(
        "server.mcp.tools.branch_handler._execute_branch_sql",
        new_callable=AsyncMock,
        return_value=error_response(
            "TIMEOUT",
            "Query timed out",
            instance_id="iid",
            cluster_id="cid",
        ),
    ):
        result = await handle_delete_branch(
            object(), object(),
            branch_name="br_slow_drop",
        )

    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"] == "OPERATION_STATUS_UNKNOWN"
    assert payload["branch_name"] == "br_slow_drop"
    assert "list_branches" in payload["message"]
    assert "instance_id" not in payload
    assert "cluster_id" not in payload
