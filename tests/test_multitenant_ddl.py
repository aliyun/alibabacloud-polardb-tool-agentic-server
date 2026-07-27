from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from server.core.multitenant_ddl import (
    DDLVerificationError,
    InvalidDatabaseIdentifier,
    MultitenantDDLAdapter,
    ObjectOwnershipConflict,
    build_create_database_sql,
    build_create_resource_config_sql,
    build_create_tenant_sql,
    build_create_user_sql,
    build_drop_database_sql,
    build_drop_resource_config_sql,
    build_drop_tenant_sql,
    build_grant_sql,
    build_lock_user_sql,
    build_kill_connection_sql,
    build_show_grants_sql,
    mysql_error_code,
)
from server.models import (
    DBInstanceResource,
    Instance,
    InstanceCredential,
    ProvisioningBackend,
)


def _resource() -> DBInstanceResource:
    return DBInstanceResource(
        id="dbi-1",
        owner_agent_id="agent-1",
        backend_id="backend-1",
        client_token="token-1",
        request_fingerprint="a" * 64,
        tenant_name="t123456789",
        resource_config_name="rc_t123456789",
        database_name="agentic@t123456789",
    )


def _adapter() -> MultitenantDDLAdapter:
    return MultitenantDDLAdapter(
        AsyncMock(),
        ProvisioningBackend(
            id="backend-1",
            instance_id="instance-1",
            admin_credential_id="credential-1",
            max_active_resources=10,
            resource_min_cpu=0,
            resource_max_cpu=2,
        ),
        Instance(
            id="instance-1",
            cluster_id="pc-1",
            name="Multitenant",
            host="pc.internal",
            port=3306,
        ),
        InstanceCredential(id="credential-1", name="admin"),
        "agentic@t123456789",
    )


def test_builders_match_polardb_multitenant_syntax():
    resource = _resource()
    assert build_create_resource_config_sql(resource.resource_config_name, 0, 2) == (
        "CREATE resource_config rc_t123456789 min_cpu 0 max_cpu 2"
    )
    assert build_create_tenant_sql(resource.tenant_name, resource.resource_config_name) == (
        "CREATE tenant t123456789 resource_config rc_t123456789"
    )
    assert build_create_user_sql("agentic@t123456789") == (
        "CREATE USER 'agentic@t123456789'@'%%' "
        "IDENTIFIED WITH mysql_native_password BY %s"
    )
    assert build_create_database_sql(resource.database_name) == (
        "CREATE DATABASE `agentic@t123456789`"
    )
    assert build_grant_sql(resource.tenant_name, "agentic@t123456789") == (
        "GRANT ALL PRIVILEGES ON `%@t123456789`.* "
        "TO 'agentic@t123456789'@'%' WITH GRANT OPTION"
    )
    assert build_show_grants_sql("agentic@t123456789") == (
        "SHOW GRANTS FOR 'agentic@t123456789'@'%'"
    )
    assert build_lock_user_sql("agentic@t123456789") == (
        "ALTER USER 'agentic@t123456789'@'%' ACCOUNT LOCK"
    )
    assert build_kill_connection_sql(123) == "KILL CONNECTION 123"
    assert build_drop_database_sql(resource.database_name) == (
        "DROP DATABASE `agentic@t123456789`"
    )
    assert build_drop_tenant_sql(resource.tenant_name) == "DROP tenant t123456789"
    assert build_drop_resource_config_sql(resource.resource_config_name) == (
        "DROP resource_config rc_t123456789"
    )


@pytest.mark.parametrize(
    "builder,args",
    [
        (build_create_tenant_sql, ("bad-name", "rc_ok")),
        (build_create_database_sql, ("db`; DROP DATABASE mysql",)),
        (build_create_user_sql, ("agentic@bad-name",)),
        (build_grant_sql, ("bad-name", "agentic@bad-name")),
    ],
)
def test_builders_reject_untrusted_identifiers(builder, args):
    with pytest.raises(InvalidDatabaseIdentifier):
        builder(*args)


def test_password_is_bound_separately_from_create_user_sql():
    sql = build_create_user_sql("agentic@t123456789")
    assert "secret' OR 1=1" not in sql
    assert sql.endswith("BY %s")


def test_mysql_error_code_uses_numeric_driver_code_only():
    assert mysql_error_code(Exception(1396, "localized text")) == 1396
    assert mysql_error_code(Exception("1396 already exists")) is None


async def test_duplicate_tenant_advances_only_when_metadata_matches():
    adapter = _adapter()
    adapter._execute = AsyncMock(side_effect=Exception(1062, "duplicate"))
    adapter.verify_tenant = AsyncMock(return_value=True)

    await adapter.create_tenant(_resource())

    adapter.verify_tenant.assert_awaited_once()


async def test_duplicate_tenant_with_mismatched_ownership_fails():
    adapter = _adapter()
    adapter._execute = AsyncMock(side_effect=Exception(1062, "duplicate"))
    adapter.verify_tenant = AsyncMock(return_value=False)

    with pytest.raises(ObjectOwnershipConflict):
        await adapter.create_tenant(_resource())


async def test_unexpected_numeric_code_is_not_treated_as_idempotent():
    error = Exception(1045, "access denied")
    adapter = _adapter()
    adapter._execute = AsyncMock(side_effect=error)
    adapter.verify_tenant = AsyncMock(return_value=True)

    with pytest.raises(Exception) as raised:
        await adapter.create_tenant(_resource())
    assert raised.value is error


async def test_successful_create_still_requires_metadata_verification():
    adapter = _adapter()
    adapter._execute = AsyncMock(return_value=None)
    adapter.verify_database = AsyncMock(return_value=False)

    with pytest.raises(DDLVerificationError):
        await adapter.create_database(_resource())


async def test_create_user_passes_password_as_bound_parameter():
    adapter = _adapter()
    adapter._execute = AsyncMock(return_value=None)
    adapter.verify_user = AsyncMock(return_value=True)

    await adapter.create_user(_resource(), "secret' OR 1=1")

    adapter._execute.assert_awaited_once_with(
        build_create_user_sql("agentic@t123456789"), ("secret' OR 1=1",)
    )


async def test_grant_verification_accepts_official_backtick_form():
    adapter = _adapter()
    adapter._fetchall = AsyncMock(
        return_value=[
            (
                "GRANT ALL PRIVILEGES ON `%@t123456789`.* "
                "TO `agentic@t123456789`@`%` WITH GRANT OPTION",
            )
        ]
    )
    assert await adapter.verify_grants(_resource()) is True


async def test_residue_verification_requires_every_object_to_be_absent():
    adapter = _adapter()
    adapter.verify_database = AsyncMock(return_value=False)
    adapter.verify_tenant = AsyncMock(return_value=False)
    adapter.verify_resource_config = AsyncMock(return_value=False)
    adapter.verify_user = AsyncMock(return_value=True)

    assert await adapter.verify_residue_absent(_resource()) is False


async def test_prepare_cleanup_locks_user_and_terminates_active_sessions():
    adapter = _adapter()
    adapter._execute = AsyncMock()
    adapter._fetchall = AsyncMock(side_effect=[[(41,), (42,)], []])

    await adapter.prepare_cleanup(_resource())

    assert adapter._execute.await_args_list[0].args == (
        build_lock_user_sql("agentic@t123456789"),
    )
    assert adapter._execute.await_args_list[1].args == ("KILL CONNECTION 41",)
    assert adapter._execute.await_args_list[2].args == ("KILL CONNECTION 42",)


async def test_prepare_cleanup_allows_user_to_already_be_absent():
    adapter = _adapter()
    adapter._execute = AsyncMock(side_effect=Exception(1396, "user missing"))
    adapter._fetchall = AsyncMock(return_value=[])

    await adapter.prepare_cleanup(_resource())


async def test_prepare_cleanup_allows_session_to_close_before_kill():
    adapter = _adapter()
    adapter._execute = AsyncMock(
        side_effect=[None, Exception(1094, "unknown thread id")]
    )
    adapter._fetchall = AsyncMock(side_effect=[[(41,)], []])

    await adapter.prepare_cleanup(_resource())


async def test_drop_tenant_tolerates_vendor_not_exists_code_when_absent():
    adapter = _adapter()
    adapter._execute = AsyncMock(
        side_effect=Exception(9901, "can't drop tenant when enable_multi_tenant is OFF")
    )
    adapter.verify_tenant = AsyncMock(return_value=False)

    await adapter.drop_tenant(_resource())


async def test_drop_tenant_reraises_unknown_code_when_object_survives():
    error = Exception(9901, "can't drop tenant when enable_multi_tenant is OFF")
    adapter = _adapter()
    adapter._execute = AsyncMock(side_effect=error)
    adapter.verify_tenant = AsyncMock(return_value=True)

    with pytest.raises(Exception) as raised:
        await adapter.drop_tenant(_resource())
    assert raised.value is error


async def test_drop_resource_config_tolerates_vendor_not_exists_code_when_absent():
    adapter = _adapter()
    adapter._execute = AsyncMock(
        side_effect=Exception(9902, "resource config not exists")
    )
    adapter.verify_resource_config = AsyncMock(return_value=False)

    await adapter.drop_resource_config(_resource())


def test_create_user_sql_survives_driver_pyformat_binding():
    sql = build_create_user_sql("agentic@t123456789")
    rendered = sql % ("s3cret",)
    assert "'agentic@t123456789'@'%'" in rendered
    assert rendered.endswith("BY s3cret")


async def test_verify_user_sql_survives_driver_pyformat_binding():
    adapter = _adapter()
    captured = {}

    async def fake_fetchone(sql, params=None):
        captured["rendered"] = sql % tuple(
            str(p) for p in (params or ())
        )
        return None

    adapter._fetchone = fake_fetchone
    assert await adapter.verify_user(_resource()) is False
    assert "Host = '%'" in captured["rendered"]
