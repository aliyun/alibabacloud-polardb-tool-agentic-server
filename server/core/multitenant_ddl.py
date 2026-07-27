from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, cast

from server.models import (
    DBInstanceResource,
    Instance,
    InstanceCredential,
    ProvisioningBackend,
)

_TENANT_RE = re.compile(r"^[A-Za-z0-9_]{1,10}$")
_RESOURCE_CONFIG_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_]{1,20}@[A-Za-z0-9_]{1,10}$")
_DATABASE_RE = re.compile(r"^[A-Za-z0-9_]{1,50}@[A-Za-z0-9_]{1,10}$")


class MultitenantDDLError(Exception):
    pass


class InvalidDatabaseIdentifier(MultitenantDDLError):
    pass


class DDLVerificationError(MultitenantDDLError):
    pass


class ObjectOwnershipConflict(MultitenantDDLError):
    pass


class ActiveTenantSessionsError(MultitenantDDLError):
    pass


def _validate(value: str | None, pattern: re.Pattern[str], kind: str) -> str:
    if value is None or not pattern.fullmatch(value):
        raise InvalidDatabaseIdentifier(f"Invalid {kind}")
    return value


def build_create_resource_config_sql(name: str, min_cpu: int, max_cpu: int) -> str:
    name = _validate(name, _RESOURCE_CONFIG_RE, "resource config name")
    if min_cpu < 0 or max_cpu < 1 or min_cpu > max_cpu:
        raise ValueError("Invalid resource config CPU range")
    return f"CREATE resource_config {name} min_cpu {min_cpu} max_cpu {max_cpu}"


def build_create_tenant_sql(tenant_name: str, resource_config_name: str) -> str:
    tenant_name = _validate(tenant_name, _TENANT_RE, "tenant name")
    resource_config_name = _validate(
        resource_config_name, _RESOURCE_CONFIG_RE, "resource config name"
    )
    return f"CREATE tenant {tenant_name} resource_config {resource_config_name}"


def build_create_user_sql(account_name: str) -> str:
    account_name = _validate(account_name, _ACCOUNT_RE, "account name")
    return (
        f"CREATE USER '{account_name}'@'%%' "
        "IDENTIFIED WITH mysql_native_password BY %s"
    )


def build_create_database_sql(database_name: str) -> str:
    database_name = _validate(database_name, _DATABASE_RE, "database name")
    return f"CREATE DATABASE `{database_name}`"


def build_grant_sql(tenant_name: str, account_name: str) -> str:
    tenant_name = _validate(tenant_name, _TENANT_RE, "tenant name")
    account_name = _validate(account_name, _ACCOUNT_RE, "account name")
    if account_name.rsplit("@", 1)[1] != tenant_name:
        raise InvalidDatabaseIdentifier("Account does not belong to tenant")
    return (
        f"GRANT ALL PRIVILEGES ON `%@{tenant_name}`.* "
        f"TO '{account_name}'@'%' WITH GRANT OPTION"
    )


def build_show_grants_sql(account_name: str) -> str:
    account_name = _validate(account_name, _ACCOUNT_RE, "account name")
    return f"SHOW GRANTS FOR '{account_name}'@'%'"


def build_lock_user_sql(account_name: str) -> str:
    account_name = _validate(account_name, _ACCOUNT_RE, "account name")
    return f"ALTER USER '{account_name}'@'%' ACCOUNT LOCK"


def build_kill_connection_sql(connection_id: int) -> str:
    if isinstance(connection_id, bool) or not isinstance(connection_id, int):
        raise ValueError("Invalid connection ID")
    if connection_id <= 0:
        raise ValueError("Invalid connection ID")
    return f"KILL CONNECTION {connection_id}"


def build_drop_database_sql(database_name: str) -> str:
    database_name = _validate(database_name, _DATABASE_RE, "database name")
    return f"DROP DATABASE `{database_name}`"


def build_drop_tenant_sql(tenant_name: str) -> str:
    tenant_name = _validate(tenant_name, _TENANT_RE, "tenant name")
    return f"DROP tenant {tenant_name}"


def build_drop_resource_config_sql(resource_config_name: str) -> str:
    resource_config_name = _validate(
        resource_config_name, _RESOURCE_CONFIG_RE, "resource config name"
    )
    return f"DROP resource_config {resource_config_name}"


def mysql_error_code(error: BaseException) -> int | None:
    if error.args and isinstance(error.args[0], int):
        return error.args[0]
    return None


class MultitenantDDLAdapter:
    def __init__(
        self,
        pool_manager,
        backend: ProvisioningBackend,
        instance: Instance,
        admin_credential: InstanceCredential,
        account_name: str,
    ) -> None:
        self._pool_manager = pool_manager
        self._backend = backend
        self._instance = instance
        self._admin_credential = admin_credential
        self._account_name = _validate(account_name, _ACCOUNT_RE, "account name")

    def _pool_args(
        self,
    ) -> tuple[ProvisioningBackend, Instance, InstanceCredential]:
        return self._backend, self._instance, self._admin_credential

    async def _execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        async with self._pool_manager.acquire(*self._pool_args()) as connection:
            async with connection.cursor() as cursor:
                if params is None:
                    await cursor.execute(sql)
                else:
                    await cursor.execute(sql, params)

    async def _fetchone(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> tuple | None:
        async with self._pool_manager.acquire(*self._pool_args()) as connection:
            async with connection.cursor() as cursor:
                if params is None:
                    await cursor.execute(sql)
                else:
                    await cursor.execute(sql, params)
                return cast(tuple[Any, ...] | None, await cursor.fetchone())

    async def _fetchall(
        self, sql: str, params: tuple[Any, ...] | None = None
    ) -> list[tuple]:
        async with self._pool_manager.acquire(*self._pool_args()) as connection:
            async with connection.cursor() as cursor:
                if params is None:
                    await cursor.execute(sql)
                else:
                    await cursor.execute(sql, params)
                rows = await cursor.fetchall()
                return list(rows)

    async def _create_and_verify(
        self,
        execute: Callable[[], Awaitable[None]],
        verify: Callable[[], Awaitable[bool]],
        allowed_duplicate_codes: set[int],
        object_kind: str,
    ) -> None:
        duplicate = False
        try:
            await execute()
        except Exception as error:
            if mysql_error_code(error) not in allowed_duplicate_codes:
                raise
            duplicate = True
        if await verify():
            return
        if duplicate:
            raise ObjectOwnershipConflict(
                f"Existing {object_kind} does not match the resource"
            )
        raise DDLVerificationError(f"Created {object_kind} could not be verified")

    async def create_resource_config(self, resource: DBInstanceResource) -> None:
        name = _validate(
            resource.resource_config_name,
            _RESOURCE_CONFIG_RE,
            "resource config name",
        )
        await self._create_and_verify(
            lambda: self._execute(
                build_create_resource_config_sql(
                    name,
                    self._backend.resource_min_cpu,
                    self._backend.resource_max_cpu,
                )
            ),
            lambda: self.verify_resource_config(resource),
            {1062},
            "resource config",
        )

    async def create_tenant(self, resource: DBInstanceResource) -> None:
        sql = build_create_tenant_sql(
            _validate(resource.tenant_name, _TENANT_RE, "tenant name"),
            _validate(
                resource.resource_config_name,
                _RESOURCE_CONFIG_RE,
                "resource config name",
            ),
        )
        await self._create_and_verify(
            lambda: self._execute(sql),
            lambda: self.verify_tenant(resource),
            {1062},
            "tenant",
        )

    async def create_user(
        self, resource: DBInstanceResource, password: str
    ) -> None:
        sql = build_create_user_sql(self._account_name)
        await self._create_and_verify(
            lambda: self._execute(sql, (password,)),
            lambda: self.verify_user(resource),
            {1396},
            "user",
        )

    async def create_database(self, resource: DBInstanceResource) -> None:
        sql = build_create_database_sql(
            _validate(resource.database_name, _DATABASE_RE, "database name")
        )
        await self._create_and_verify(
            lambda: self._execute(sql),
            lambda: self.verify_database(resource),
            {1007},
            "database",
        )

    async def grant_privileges(self, resource: DBInstanceResource) -> None:
        sql = build_grant_sql(
            _validate(resource.tenant_name, _TENANT_RE, "tenant name"),
            self._account_name,
        )
        await self._execute(sql)
        if not await self.verify_grants(resource):
            raise DDLVerificationError("Tenant grant could not be verified")

    async def prepare_cleanup(self, resource: DBInstanceResource) -> None:
        del resource
        account = self._account_name
        try:
            await self._execute(build_lock_user_sql(account))
        except Exception as error:
            if mysql_error_code(error) != 1396:
                raise

        query = (
            "SELECT ID FROM information_schema.PROCESSLIST "
            "WHERE USER = %s AND ID <> CONNECTION_ID()"
        )
        rows = await self._fetchall(query, (account,))
        for row in rows:
            if not row:
                raise DDLVerificationError("Invalid active session metadata")
            try:
                await self._execute(build_kill_connection_sql(row[0]))
            except Exception as error:
                if mysql_error_code(error) != 1094:
                    raise
        remaining = await self._fetchall(query, (account,))
        if remaining:
            raise ActiveTenantSessionsError(
                "Tenant sessions remain after connection termination"
            )

    async def verify_resource_config(
        self, resource: DBInstanceResource
    ) -> bool:
        name = _validate(
            resource.resource_config_name,
            _RESOURCE_CONFIG_RE,
            "resource config name",
        )
        row = await self._fetchone(
            "SELECT resource_config_name, resource_config_min_cpu, "
            "resource_config_max_cpu FROM mysql.tenant_resource_config "
            "WHERE resource_config_name = %s",
            (name,),
        )
        return row == (
            name,
            self._backend.resource_min_cpu,
            self._backend.resource_max_cpu,
        )

    async def verify_tenant(self, resource: DBInstanceResource) -> bool:
        tenant = _validate(resource.tenant_name, _TENANT_RE, "tenant name")
        resource_config = _validate(
            resource.resource_config_name,
            _RESOURCE_CONFIG_RE,
            "resource config name",
        )
        row = await self._fetchone(
            "SELECT tenant_name, resource_config_name FROM mysql.tenants "
            "WHERE tenant_name = %s",
            (tenant,),
        )
        return row == (tenant, resource_config)

    async def verify_user(self, resource: DBInstanceResource) -> bool:
        del resource
        account = self._account_name
        row = await self._fetchone(
            "SELECT User, Host FROM mysql.user WHERE User = %s AND Host = '%%'",
            (account,),
        )
        return row == (account, "%")

    async def verify_database(self, resource: DBInstanceResource) -> bool:
        database = _validate(
            resource.database_name, _DATABASE_RE, "database name"
        )
        row = await self._fetchone(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME = %s",
            (database,),
        )
        return row == (database,)

    async def verify_grants(self, resource: DBInstanceResource) -> bool:
        tenant = _validate(resource.tenant_name, _TENANT_RE, "tenant name")
        account = self._account_name
        rows = await self._fetchall(build_show_grants_sql(account))
        scope = f"ON `%@{tenant}`.*"
        quoted_users = {
            f"TO `{account}`@`%`",
            f"TO '{account}'@'%'",
        }
        return any(
            row
            and scope in str(row[0])
            and any(user in str(row[0]) for user in quoted_users)
            for row in rows
        )

    async def _drop_and_verify_absent(
        self,
        sql: str,
        verify_exists: Callable[[], Awaitable[bool]],
        allowed_missing_codes: set[int],
        object_kind: str,
    ) -> None:
        try:
            await self._execute(sql)
        except Exception as error:
            if mysql_error_code(error) not in allowed_missing_codes:
                if await verify_exists():
                    raise
                # Absence is the goal state; tolerate vendor-specific
                # "not exists" error codes outside the allowlist.
                return
        if await verify_exists():
            raise DDLVerificationError(f"Dropped {object_kind} still exists")

    async def drop_database(self, resource: DBInstanceResource) -> None:
        await self._drop_and_verify_absent(
            build_drop_database_sql(
                _validate(resource.database_name, _DATABASE_RE, "database name")
            ),
            lambda: self.verify_database(resource),
            {1008},
            "database",
        )

    async def drop_tenant(self, resource: DBInstanceResource) -> None:
        await self._drop_and_verify_absent(
            build_drop_tenant_sql(
                _validate(resource.tenant_name, _TENANT_RE, "tenant name")
            ),
            lambda: self.verify_tenant(resource),
            {1091},
            "tenant",
        )

    async def drop_resource_config(
        self, resource: DBInstanceResource
    ) -> None:
        await self._drop_and_verify_absent(
            build_drop_resource_config_sql(
                _validate(
                    resource.resource_config_name,
                    _RESOURCE_CONFIG_RE,
                    "resource config name",
                )
            ),
            lambda: self.verify_resource_config(resource),
            {1091},
            "resource config",
        )

    async def verify_residue_absent(
        self, resource: DBInstanceResource
    ) -> bool:
        database_exists = await self.verify_database(resource)
        tenant_exists = await self.verify_tenant(resource)
        resource_exists = await self.verify_resource_config(resource)
        user_exists = await self.verify_user(resource)
        return not any(
            [database_exists, tenant_exists, resource_exists, user_exists]
        )
