from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import asyncmy  # type: ignore[import-untyped]

from server.core.crypto import decrypt
from server.core.permission_template_service import (
    CompiledPermissionSnapshot,
    DatabasePrivilege,
    PermissionScope,
    permission_snapshot_from_json,
)
from server.models import DedicatedPoolMember

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_GRANT_RE = re.compile(r"^GRANT (.+) ON (.+) TO ", re.IGNORECASE)


class DedicatedMySQLError(Exception):
    pass


class InvalidDedicatedIdentifier(DedicatedMySQLError):
    pass


class DedicatedGrantVerificationError(DedicatedMySQLError):
    pass


def _identifier(value: str, kind: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise InvalidDedicatedIdentifier(f"Invalid {kind}")
    return value


def build_dedicated_grant_sql(
    *,
    database: str,
    account: str,
    snapshot: CompiledPermissionSnapshot,
) -> tuple[str, ...]:
    database = _identifier(database, "database name")
    account = _identifier(account, "account name")
    if snapshot.scope is not PermissionScope.DEDICATED:
        raise ValueError("Dedicated grant requires global permission scope")

    database_privileges = [
        privilege.value
        for privilege in snapshot.privileges
        if privilege is not DatabasePrivilege.CREATE_USER
    ]
    statements: list[str] = []
    grant_option = " WITH GRANT OPTION" if snapshot.grant_option else ""
    if snapshot.legacy_all_privileges:
        statements.append(
            f"GRANT ALL PRIVILEGES ON `{database}`.* "
            f"TO '{account}'@'%'{grant_option}"
        )
    elif database_privileges:
        statements.append(
            f"GRANT {', '.join(database_privileges)} ON `{database}`.* "
            f"TO '{account}'@'%'{grant_option}"
        )
    if DatabasePrivilege.CREATE_USER in snapshot.privileges:
        statements.append(
            f"GRANT CREATE USER ON *.* TO '{account}'@'%'{grant_option}"
        )
    if not statements:
        raise ValueError("Dedicated grant requires at least one privilege")
    return tuple(statements)


def _row_text(row: Any) -> str:
    if isinstance(row, (tuple, list)) and row:
        return str(row[0])
    return str(row)


def verify_dedicated_grants(
    *,
    database: str,
    account: str,
    snapshot: CompiledPermissionSnapshot,
    rows: Sequence[Any],
) -> None:
    database = _identifier(database, "database name")
    _identifier(account, "account name")
    expected_database = (
        {"ALL PRIVILEGES"}
        if snapshot.legacy_all_privileges
        else {
            privilege.value
            for privilege in snapshot.privileges
            if privilege is not DatabasePrivilege.CREATE_USER
        }
    )
    expected_global = (
        {"CREATE USER"}
        if DatabasePrivilege.CREATE_USER in snapshot.privileges
        else set()
    )
    actual_database: set[str] = set()
    actual_global: set[str] = set()
    saw_grant_option = False
    unexpected_scope = False
    database_scope = f"`{database}`.*".upper()

    for row in rows:
        text = " ".join(_row_text(row).split())
        upper = text.upper()
        if upper.startswith("GRANT USAGE ON *.*"):
            continue
        match = _GRANT_RE.match(text)
        if match is None:
            unexpected_scope = True
            continue
        privileges = {
            value.strip().upper() for value in match.group(1).split(",")
        }
        scope = match.group(2).upper()
        if scope == database_scope:
            actual_database.update(privileges)
        elif scope == "*.*":
            actual_global.update(privileges)
        else:
            unexpected_scope = True
        saw_grant_option = saw_grant_option or " WITH GRANT OPTION" in upper

    if (
        unexpected_scope
        or actual_database != expected_database
        or actual_global != expected_global
        or saw_grant_option != snapshot.grant_option
    ):
        raise DedicatedGrantVerificationError(
            "Sandbox grants do not match the permission snapshot"
        )


Connector = Callable[..., Awaitable[Any]]


async def _close(connection: Any) -> None:
    result = connection.close()
    if inspect.isawaitable(result):
        await result


class DedicatedMySQL:
    def __init__(self, connector: Connector = asyncmy.connect) -> None:
        self._connector = connector

    @staticmethod
    def _required(member: DedicatedPoolMember) -> tuple[str, int, str, str, str, str]:
        values = (
            member.host,
            member.port,
            member.lifecycle_username_ciphertext,
            member.lifecycle_password_ciphertext,
            member.sandbox_username_ciphertext,
            member.sandbox_password_ciphertext,
            member.database_name,
            member.permission_snapshot_json,
        )
        if any(value is None for value in values):
            raise DedicatedMySQLError("Dedicated member credentials are incomplete")
        return (
            str(member.host),
            int(member.port),
            decrypt(str(member.lifecycle_username_ciphertext)),
            decrypt(str(member.lifecycle_password_ciphertext)),
            decrypt(str(member.sandbox_username_ciphertext)),
            decrypt(str(member.sandbox_password_ciphertext)),
        )

    async def apply_permissions(self, member: DedicatedPoolMember) -> None:
        host, port, lifecycle_user, lifecycle_password, sandbox_user, _ = (
            self._required(member)
        )
        snapshot = permission_snapshot_from_json(
            str(member.permission_snapshot_json)
        )
        statements = build_dedicated_grant_sql(
            database=str(member.database_name),
            account=sandbox_user,
            snapshot=snapshot,
        )
        connection = await self._connector(
            host=host,
            port=port,
            user=lifecycle_user,
            password=lifecycle_password,
            connect_timeout=10,
        )
        try:
            async with connection.cursor() as cursor:
                for statement in statements:
                    await cursor.execute(statement)
        finally:
            await _close(connection)

    async def synchronize_permissions(
        self,
        member: DedicatedPoolMember,
        snapshot: CompiledPermissionSnapshot,
    ) -> None:
        """Replace effective sandbox grants, then verify the exact result."""
        host, port, lifecycle_user, lifecycle_password, sandbox_user, _ = (
            self._required(member)
        )
        statements = build_dedicated_grant_sql(
            database=str(member.database_name),
            account=sandbox_user,
            snapshot=snapshot,
        )
        connection = await self._connector(
            host=host,
            port=port,
            user=lifecycle_user,
            password=lifecycle_password,
            connect_timeout=10,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "REVOKE ALL PRIVILEGES, GRANT OPTION FROM "
                    f"'{_identifier(sandbox_user, 'account name')}'@'%'"
                )
                for statement in statements:
                    await cursor.execute(statement)
        finally:
            await _close(connection)
        await self.verify(member)

    async def verify(self, member: DedicatedPoolMember) -> None:
        (
            host,
            port,
            lifecycle_user,
            lifecycle_password,
            sandbox_user,
            sandbox_password,
        ) = self._required(member)
        database = str(member.database_name)
        snapshot = permission_snapshot_from_json(
            str(member.permission_snapshot_json)
        )
        lifecycle_connection = await self._connector(
            host=host,
            port=port,
            user=lifecycle_user,
            password=lifecycle_password,
            connect_timeout=10,
        )
        try:
            async with lifecycle_connection.cursor() as cursor:
                await cursor.execute(
                    f"SHOW GRANTS FOR '{_identifier(sandbox_user, 'account name')}'@'%'"
                )
                rows = await cursor.fetchall()
            verify_dedicated_grants(
                database=database,
                account=sandbox_user,
                snapshot=snapshot,
                rows=rows,
            )
        finally:
            await _close(lifecycle_connection)

        sandbox_connection = await self._connector(
            host=host,
            port=port,
            user=sandbox_user,
            password=sandbox_password,
            db=database,
            connect_timeout=10,
        )
        try:
            async with sandbox_connection.cursor() as cursor:
                await cursor.execute("SELECT 1")
                row = await cursor.fetchone()
            if row is None or int(row[0]) != 1:
                raise DedicatedGrantVerificationError(
                    "Sandbox credential health check failed"
                )
        finally:
            await _close(sandbox_connection)

    async def disconnect(self, member: DedicatedPoolMember) -> None:
        host, port, lifecycle_user, lifecycle_password, sandbox_user, _ = (
            self._required(member)
        )
        connection = await self._connector(
            host=host,
            port=port,
            user=lifecycle_user,
            password=lifecycle_password,
            connect_timeout=10,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    f"ALTER USER '{_identifier(sandbox_user, 'account name')}'@'%' ACCOUNT LOCK"
                )
                query = (
                    "SELECT ID FROM information_schema.PROCESSLIST "
                    "WHERE USER = %s AND ID <> CONNECTION_ID()"
                )
                await cursor.execute(query, (sandbox_user,))
                rows = await cursor.fetchall()
                for row in rows:
                    connection_id = int(row[0])
                    if connection_id <= 0:
                        raise DedicatedMySQLError(
                            "Invalid sandbox session identifier"
                        )
                    await cursor.execute(f"KILL CONNECTION {connection_id}")
                await cursor.execute(query, (sandbox_user,))
                if await cursor.fetchall():
                    raise DedicatedMySQLError(
                        "Sandbox sessions remain connected"
                    )
        finally:
            await _close(connection)

    async def restore(self, member: DedicatedPoolMember) -> None:
        host, port, lifecycle_user, lifecycle_password, sandbox_user, _ = (
            self._required(member)
        )
        connection = await self._connector(
            host=host,
            port=port,
            user=lifecycle_user,
            password=lifecycle_password,
            connect_timeout=10,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    f"ALTER USER '{_identifier(sandbox_user, 'account name')}'@'%' ACCOUNT UNLOCK"
                )
        finally:
            await _close(connection)
        await self.apply_permissions(member)
        await self.verify(member)

    async def drop_sandbox(self, member: DedicatedPoolMember) -> None:
        host, port, lifecycle_user, lifecycle_password, sandbox_user, _ = (
            self._required(member)
        )
        database = _identifier(str(member.database_name), "database name")
        connection = await self._connector(
            host=host,
            port=port,
            user=lifecycle_user,
            password=lifecycle_password,
            connect_timeout=10,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute(f"DROP DATABASE IF EXISTS `{database}`")
                await cursor.execute(
                    f"DROP USER IF EXISTS '{_identifier(sandbox_user, 'account name')}'@'%'"
                )
        finally:
            await _close(connection)
