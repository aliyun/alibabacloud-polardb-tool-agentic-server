"""Tests for the describe_schema MCP handler."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.core.binding_manager import UserSQLCredential
from server.core.sql_gateway import SQLGateway
from server.mcp.tools.schema_handler import handle_describe_schema
from server.models import Permission


@pytest.fixture
def mock_user():
    from server.models import UserRole, UserStatus

    user = MagicMock()
    user.id = "user-1"
    user.default_instance_id = "inst-1"
    user.status = UserStatus.ACTIVE
    user.role = UserRole.MEMBER
    return user


@pytest.fixture
def mock_instance():
    from server.models import InstanceStatus, InstanceTopology

    inst = MagicMock()
    inst.id = "inst-1"
    inst.cluster_id = "cluster-1"
    inst.host = "localhost"
    inst.port = 3306
    inst.topology = InstanceTopology.SINGLE_TENANT
    inst.status = InstanceStatus.ACTIVE
    return inst


@pytest.fixture
def mock_db_account():
    acc = MagicMock()
    acc.username_ciphertext = "encrypted-user"
    acc.password_ciphertext = "encrypted-password"
    acc.tenant_name = None
    acc.provisioning_step = None
    return acc


def _make_session(db_account):
    """Build an AsyncMock session whose execute() returns a mocked Result."""
    session = AsyncMock()
    session.add = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = db_account
    session.execute.return_value = result_mock
    return session


def _make_mock_gateway(table_rows, columns_rows=None):
    """Build a mock gateway whose connection cursor returns canned rows.

    The first ``cur.execute`` resolves to ``table_rows``; subsequent calls
    consume entries from ``columns_rows`` (defaulting to empty lists).
    Each call is recorded in ``gateway._call_log`` as ``(sql, params)`` so
    tests can assert parameterization.
    """
    if columns_rows is None:
        columns_rows = []
    responses: list = [list(table_rows), *[list(c) for c in columns_rows]]
    table_desc = ["TABLE_NAME", "TABLE_COMMENT", "TABLE_ROWS", "CREATE_TIME"]
    column_desc = ["COLUMN_NAME", "COLUMN_TYPE", "COLUMN_COMMENT"]
    descriptions = [table_desc] + [column_desc] * max(len(columns_rows), 1)

    state = {"i": 0}
    call_log: list = []

    cur = MagicMock()
    cur.description = []

    async def cur_execute(sql, params=None):
        call_log.append((sql, params))
        idx = state["i"]
        desc_names = descriptions[idx] if idx < len(descriptions) else ["c"]
        cur.description = [(name,) for name in desc_names]

    async def cur_fetchall():
        idx = state["i"]
        state["i"] += 1
        if idx < len(responses):
            return [tuple(r) for r in responses[idx]]
        return []

    cur.execute = AsyncMock(side_effect=cur_execute)
    cur.fetchall = AsyncMock(side_effect=cur_fetchall)

    cursor_cm = MagicMock()
    cursor_cm.__aenter__ = AsyncMock(return_value=cur)
    cursor_cm.__aexit__ = AsyncMock(return_value=None)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor_cm)

    cache = MagicMock()
    cache.acquire = AsyncMock(return_value=conn)
    cache.release = AsyncMock()
    gateway = SQLGateway(cache)
    gateway._call_log = call_log
    return gateway


def _patches(gateway, mock_instance, tenant_name=None):
    accessible = [{"instance": mock_instance, "permission": "readwrite"}]
    credential = UserSQLCredential(
        binding=MagicMock(tenant_name=tenant_name),
        credential=MagicMock(
            username_ciphertext="encrypted-user",
            password_ciphertext="encrypted-password",
        ),
        permission=Permission.READWRITE,
    )
    return (
        patch(
            "server.mcp.tools.schema_handler.resolve_target_instance",
            AsyncMock(return_value=(mock_instance, accessible)),
        ),
        patch(
            "server.mcp.tools.schema_handler.get_gateway",
            return_value=gateway,
        ),
        patch(
            "server.mcp.tools.schema_handler._resolve_user_sql_credential",
            AsyncMock(return_value=credential),
        ),
        patch(
            "server.mcp.tools.schema_handler.decrypt",
            return_value="pw",
        ),
    )


class TestDescribeSchemaEmpty:
    @pytest.mark.asyncio
    async def test_no_tables(self, mock_user, mock_instance, mock_db_account):
        """Empty database returns empty tables list."""
        gateway = _make_mock_gateway(table_rows=[])
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            result = await handle_describe_schema(mock_user, session)

        text = result["content"][0]["text"]
        data = json.loads(text)
        assert data["tables"] == []
        assert data["has_more"] is False
        assert "next_cursor" not in data


class TestDescribeSchemaPagination:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_tables", [True, "2", 0, -1])
    async def test_rejects_invalid_max_tables_before_query(
        self, mock_user, mock_instance, mock_db_account, max_tables,
    ):
        """``max_tables`` must be positive to avoid non-advancing cursors."""
        gateway = _make_mock_gateway(table_rows=[])
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            result = await handle_describe_schema(
                mock_user, session, max_tables=max_tables,
            )

        assert result.get("isError") is True
        payload = json.loads(result["content"][0]["text"])
        assert payload == {
            "error": "INVALID_ARGUMENT",
            "message": "max_tables must be a positive integer.",
        }
        gateway._cache.acquire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_has_more_when_results_exceed_max(
        self, mock_user, mock_instance, mock_db_account,
    ):
        """When DB returns more rows than max_tables, ``has_more`` is True."""
        # max_tables=2 — handler asks for 3 rows; DB returns 3 → has_more.
        tables_rows = [
            [f"table_{i}", f"comment {i}", 100, "2026-06-15"]
            for i in range(3)
        ]
        # Two tables survive truncation → two columns queries follow.
        columns_rows = [
            [["id", "INT", "primary key"]],
            [["id", "INT", "primary key"]],
        ]
        gateway = _make_mock_gateway(tables_rows, columns_rows)
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            result = await handle_describe_schema(
                mock_user, session, max_tables=2,
            )

        text = result["content"][0]["text"]
        data = json.loads(text)
        assert len(data["tables"]) == 2
        assert data["has_more"] is True
        assert "next_cursor" in data
        # Each surviving table includes its columns block.
        assert data["tables"][0]["columns"] == [
            {"name": "id", "type": "INT", "comment": "primary key"},
        ]

    @pytest.mark.asyncio
    async def test_max_tables_capped_at_100(
        self, mock_user, mock_instance, mock_db_account,
    ):
        """``max_tables`` over 100 must be capped to 100."""
        gateway = _make_mock_gateway(table_rows=[])
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            await handle_describe_schema(
                mock_user, session, max_tables=500,
            )

        # First call is the tables query.
        sql, params = gateway._call_log[0]
        # LIMIT param is effective_max + 1 = 100 + 1 = 101.
        assert params[-2] == 101


class TestDescribeSchemaInjection:
    @pytest.mark.asyncio
    async def test_table_pattern_with_injection_attempt(
        self, mock_user, mock_instance, mock_db_account,
    ):
        """SQL injection in ``table_pattern`` must be safely parameterized."""
        gateway = _make_mock_gateway(table_rows=[])
        session = _make_session(mock_db_account)

        evil = "'; DROP TABLE users; --"
        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            result = await handle_describe_schema(
                mock_user, session, table_pattern=evil,
            )

        # Verify SQL is parameterized — not concatenated with the value.
        sql, params = gateway._call_log[0]
        assert "TABLE_NAME LIKE %s" in sql
        assert evil not in sql
        assert evil in params

        text = result["content"][0]["text"]
        data = json.loads(text)
        assert "tables" in data
        assert data["tables"] == []


class TestDescribeSchemaIncludeColumns:
    @pytest.mark.asyncio
    async def test_include_columns_false_skips_columns_query(
        self, mock_user, mock_instance, mock_db_account,
    ):
        """When ``include_columns=False``, no column queries should run."""
        tables_rows = [["t1", "t1 comment", 10, "2026-06-15"]]
        gateway = _make_mock_gateway(tables_rows)
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            result = await handle_describe_schema(
                mock_user, session, include_columns=False,
            )

        # Only the tables query should have been executed.
        assert len(gateway._call_log) == 1
        text = result["content"][0]["text"]
        data = json.loads(text)
        assert len(data["tables"]) == 1
        assert "columns" not in data["tables"][0]


class TestDescribeSchemaCursor:
    @pytest.mark.asyncio
    async def test_cursor_decoded_into_offset(
        self, mock_user, mock_instance, mock_db_account,
    ):
        """A cursor token is decoded and supplied as the OFFSET parameter."""
        from server.core.sql_executor import encode_cursor

        gateway = _make_mock_gateway(table_rows=[])
        session = _make_session(mock_db_account)

        cursor_token = encode_cursor(40)
        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            await handle_describe_schema(
                mock_user, session, cursor=cursor_token, max_tables=10,
            )

        sql, params = gateway._call_log[0]
        # Last param is OFFSET.
        assert params[-1] == 40
        # Second-to-last is LIMIT (effective_max + 1).
        assert params[-2] == 11


class TestDescribeSchemaMultitenant:
    @pytest.mark.asyncio
    async def test_multitenant_resolves_agentic_at_tenant(
        self, mock_user, mock_instance, mock_db_account,
    ):
        """Multitenant instance with tenant_name resolves to ``agentic@tenant``."""
        from server.models import InstanceTopology

        mock_instance.topology = InstanceTopology.MULTITENANT

        gateway = _make_mock_gateway(table_rows=[])
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(
            gateway, mock_instance, tenant_name="alice"
        )
        with p1, p2, p3, p4:
            await handle_describe_schema(mock_user, session)

        # The TABLE_SCHEMA parameter should be the resolved tenant database.
        sql, params = gateway._call_log[0]
        assert params[0] == "agentic@alice"
        # Connection acquired with the same database name.
        gateway._cache.acquire.assert_called_once()
        kwargs = gateway._cache.acquire.call_args.kwargs
        assert kwargs["database"] == "agentic@alice"


class TestDescribeSchemaNoCredential:
    @pytest.mark.asyncio
    async def test_personal_instance_no_account_returns_error(
        self, mock_user, mock_instance,
    ):
        """A personal instance without a credential returns a stable error."""
        gateway = _make_mock_gateway(table_rows=[])
        session = _make_session(db_account=None)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        no_credential = patch(
            "server.mcp.tools.schema_handler._resolve_user_sql_credential",
            AsyncMock(
                return_value={
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {
                                    "error": "NO_DB_ACCOUNT",
                                    "message": "No database account found.",
                                }
                            ),
                        }
                    ],
                    "isError": True,
                }
            ),
        )
        with p1, p2, p4, no_credential:
            result = await handle_describe_schema(mock_user, session)

        assert result.get("isError") is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "NO_DB_ACCOUNT"


class TestDescribeSchemaErrorPropagation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure_stage", ["acquire", "execute"])
    async def test_unexpected_database_errors_are_sanitized_and_audited(
        self,
        mock_user,
        mock_instance,
        mock_db_account,
        caplog,
        failure_stage,
    ):
        sentinel = (
            "password=SECRET host=private db=secret_db "
            "pattern=SENSITIVE_PATTERN"
        )
        gateway = _make_mock_gateway(table_rows=[])
        if failure_stage == "acquire":
            gateway._cache.acquire = AsyncMock(
                side_effect=RuntimeError(sentinel)
            )
        else:
            conn = gateway._cache.acquire.return_value
            cur = conn.cursor.return_value.__aenter__.return_value
            cur.execute = AsyncMock(side_effect=RuntimeError(sentinel))
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            result = await handle_describe_schema(
                mock_user,
                session,
                database="secret_db",
                table_pattern="SENSITIVE_PATTERN",
            )

        payload = json.loads(result["content"][0]["text"])
        expected_code = (
            "CONNECTION_ERROR"
            if failure_stage == "acquire"
            else "SQL_ERROR"
        )
        assert payload == {
            "error": expected_code,
            "message": (
                "Database connection failed"
                if failure_stage == "acquire"
                else "Database operation failed"
            ),
        }
        assert sentinel not in caplog.text
        audit = session.add.call_args.args[0]
        assert audit.action == "describe_schema"
        assert audit.error_code == expected_code
        assert sentinel not in (audit.metadata_json or "")
        assert "SENSITIVE_PATTERN" not in (audit.metadata_json or "")
        assert "secret_db" not in (audit.metadata_json or "")

    @pytest.mark.asyncio
    async def test_cancellation_is_not_translated(
        self, mock_user, mock_instance, mock_db_account,
    ):
        gateway = _make_mock_gateway(table_rows=[])
        gateway._cache.acquire = AsyncMock(
            side_effect=asyncio.CancelledError
        )
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4, pytest.raises(asyncio.CancelledError):
            await handle_describe_schema(mock_user, session)

    @pytest.mark.asyncio
    async def test_resolve_instance_error_passthrough(self, mock_user):
        """Errors from resolve_target_instance pass straight through."""
        err = {"content": [{"type": "text", "text": "{}"}], "isError": True}
        with patch(
            "server.mcp.tools.schema_handler.resolve_target_instance",
            AsyncMock(return_value=err),
        ):
            result = await handle_describe_schema(mock_user, AsyncMock())

        assert result is err

    @pytest.mark.asyncio
    async def test_creating_instance_returns_starting_error(
        self, mock_user, mock_instance, mock_db_account,
    ):
        """An instance still in CREATING state returns INSTANCE_STARTING."""
        from server.models import InstanceStatus

        mock_instance.status = InstanceStatus.CREATING
        accessible = [{"instance": mock_instance, "permission": "readwrite"}]
        session = _make_session(mock_db_account)

        with patch(
            "server.mcp.tools.schema_handler.resolve_target_instance",
            AsyncMock(return_value=(mock_instance, accessible)),
        ):
            result = await handle_describe_schema(mock_user, session)

        assert result.get("isError") is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "INSTANCE_STARTING"

    @pytest.mark.asyncio
    async def test_tables_query_error_discards_connection(
        self, mock_user, mock_instance, mock_db_account,
    ):
        """A failed schema query must discard its cached connection."""
        gateway = _make_mock_gateway(table_rows=[])
        conn = gateway._cache.acquire.return_value
        cur = conn.cursor.return_value.__aenter__.return_value
        cur.execute = AsyncMock(side_effect=RuntimeError("boom"))
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            result = await handle_describe_schema(mock_user, session)

        assert result.get("isError") is True
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "SQL_ERROR"
        gateway._cache.release.assert_awaited_once_with(
            mock_user.id, mock_instance.id, conn, discard=True,
        )

    @pytest.mark.asyncio
    async def test_columns_query_error_discards_connection_and_returns_empty_columns(
        self, mock_user, mock_instance, mock_db_account,
    ):
        """A failed columns query should not poison the cached connection."""
        gateway = _make_mock_gateway([["t1", "comment", 1, None]])
        conn = gateway._cache.acquire.return_value
        cur = conn.cursor.return_value.__aenter__.return_value
        original_execute = cur.execute
        call_count = 0

        async def execute_side_effect(sql, params=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("columns failed")
            return await original_execute(sql, params)

        cur.execute = AsyncMock(side_effect=execute_side_effect)
        session = _make_session(mock_db_account)

        p1, p2, p3, p4 = _patches(gateway, mock_instance)
        with p1, p2, p3, p4:
            result = await handle_describe_schema(mock_user, session)

        payload = json.loads(result["content"][0]["text"])
        assert payload["tables"][0]["columns"] == []
        assert gateway._cache.release.await_count == 2
        assert gateway._cache.release.await_args_list[0].kwargs["discard"] is False
        assert gateway._cache.release.await_args_list[1].kwargs["discard"] is True
