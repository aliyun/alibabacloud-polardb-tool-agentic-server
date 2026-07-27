from __future__ import annotations

import asyncmy  # type: ignore[import-untyped]

_ENABLE_MULTITENANT_SQL = "SHOW VARIABLES LIKE 'enable_multi_tenant'"
_RDS_KILL_USER_LIST_SQL = "SHOW VARIABLES LIKE 'rds_kill_user_list'"


class ConnectionTestError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _error_code(exc: Exception) -> str:
    args = getattr(exc, "args", ())
    mysql_code = args[0] if args and isinstance(args[0], int) else None
    if mysql_code in {1044, 1045}:
        return "AUTHENTICATION_FAILED"
    if mysql_code in {2002, 2003, 2005, 2006, 2013}:
        return "CONNECTION_FAILED"
    return "CONNECTION_TEST_FAILED"


def _error_message(code: str) -> str:
    if code == "AUTHENTICATION_FAILED":
        return "Database authentication failed"
    if code == "CONNECTION_FAILED":
        return "Database endpoint is unreachable"
    return "Database connection test failed"


async def _read_required_variable(
    cursor,
    sql: str,
    name: str,
) -> str:
    await cursor.execute(sql)
    row = await cursor.fetchone()
    if (
        not isinstance(row, (tuple, list))
        or len(row) < 2
        or not isinstance(row[0], str)
        or row[0].casefold() != name.casefold()
        or not isinstance(row[1], str)
    ):
        raise ConnectionTestError(
            "MULTITENANT_PREFLIGHT_FAILED",
            "PolarDB multitenant prerequisites could not be verified",
        )
    return row[1]


async def test_mysql_connection(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    database: str | None = None,
    require_multitenant: bool = False,
) -> None:
    connection = None
    try:
        connection = await asyncmy.connect(
            host=host,
            port=port,
            user=username,
            password=password,
            db=database,
            connect_timeout=10,
            autocommit=True,
        )
        async with connection.cursor() as cursor:
            await cursor.execute("SELECT 1")
            if await cursor.fetchone() != (1,):
                raise ConnectionTestError(
                    "QUERY_FAILED",
                    "Database returned an unexpected SELECT 1 result",
                )
            if require_multitenant:
                enabled = await _read_required_variable(
                    cursor,
                    _ENABLE_MULTITENANT_SQL,
                    "enable_multi_tenant",
                )
                if enabled.strip().casefold() != "on":
                    raise ConnectionTestError(
                        "MULTITENANT_DISABLED",
                        "PolarDB multitenant mode is not enabled. "
                        "Enable it and restart the cluster before "
                        "registration.",
                    )
                kill_user_list = await _read_required_variable(
                    cursor,
                    _RDS_KILL_USER_LIST_SQL,
                    "rds_kill_user_list",
                )
                allowed_users = {
                    value.strip()
                    for value in kill_user_list.split(",")
                    if value.strip()
                }
                if username not in allowed_users:
                    raise ConnectionTestError(
                        "MULTITENANT_ADMIN_REQUIRED",
                        "The supplied account is not a supported "
                        "PolarDB high-privilege account.",
                    )
    except ConnectionTestError:
        raise
    except Exception as exc:
        code = _error_code(exc)
        raise ConnectionTestError(code, _error_message(code)) from exc
    finally:
        if connection is not None:
            try:
                await connection.ensure_closed()
            except Exception:
                pass
