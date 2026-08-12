from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from server.core.dedicated_mysql import (
    DedicatedMySQL,
    DedicatedGrantVerificationError,
    InvalidDedicatedIdentifier,
    build_dedicated_grant_sql,
    verify_dedicated_grants,
)
from server.core.permission_template_service import (
    CompiledPermissionSnapshot,
    DatabasePrivilege,
    PermissionScope,
    default_permission_snapshot,
    permission_snapshot_to_json,
)


class _FakeCursor:
    def __init__(self, statements):
        self.statements = statements
        self.last_sql = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, sql, _parameters=None):
        self.last_sql = sql
        self.statements.append(sql)

    async def fetchall(self):
        if self.last_sql.startswith("SHOW GRANTS"):
            return [("GRANT SELECT ON `agentic`.* TO 'agentic'@'%'",)]
        return []

    async def fetchone(self):
        return (1,)


class _FakeConnection:
    def __init__(self, statements):
        self.statements = statements

    def cursor(self):
        return _FakeCursor(self.statements)

    def close(self):
        return None


def test_default_permission_snapshot_never_grants_account_administration():
    statements = build_dedicated_grant_sql(
        database="agentic",
        account="agentic",
        snapshot=default_permission_snapshot(PermissionScope.DEDICATED),
    )
    assert len(statements) == 1
    assert "CREATE USER" not in statements[0]
    assert "WITH GRANT OPTION" not in statements[0]
    assert "ON `agentic`.*" in statements[0]


@pytest.mark.parametrize(
    "identifier",
    ["", "has-dash", "contains space", "x" * 65, "`escaped`"],
)
def test_dedicated_identifiers_are_strict(identifier):
    with pytest.raises(InvalidDedicatedIdentifier):
        build_dedicated_grant_sql(
            database=identifier,
            account="agentic",
            snapshot=default_permission_snapshot(PermissionScope.DEDICATED),
        )


def test_create_user_is_a_separate_global_grant_only_when_configured():
    snapshot = CompiledPermissionSnapshot(
        template_id="template",
        revision_id="revision",
        privileges=(DatabasePrivilege.SELECT, DatabasePrivilege.CREATE_USER),
        grant_option=True,
        scope=PermissionScope.DEDICATED,
    )

    assert build_dedicated_grant_sql(
        database="agentic", account="agentic", snapshot=snapshot
    ) == (
        "GRANT SELECT ON `agentic`.* TO 'agentic'@'%' WITH GRANT OPTION",
        "GRANT CREATE USER ON *.* TO 'agentic'@'%' WITH GRANT OPTION",
    )


def test_grant_verification_rejects_extra_privileges():
    snapshot = CompiledPermissionSnapshot(
        template_id="template",
        revision_id="revision",
        privileges=(DatabasePrivilege.SELECT,),
        grant_option=False,
        scope=PermissionScope.DEDICATED,
    )

    with pytest.raises(DedicatedGrantVerificationError):
        verify_dedicated_grants(
            database="agentic",
            account="agentic",
            snapshot=snapshot,
            rows=[
                (
                    "GRANT SELECT, INSERT ON `agentic`.* "
                    "TO 'agentic'@'%'",
                )
            ],
        )


async def test_permission_sync_revokes_old_grants_before_exact_regrant():
    statements = []

    async def connector(**_kwargs):
        return _FakeConnection(statements)

    snapshot = CompiledPermissionSnapshot(
        template_id="template",
        revision_id="revision",
        privileges=(DatabasePrivilege.SELECT,),
        grant_option=False,
        scope=PermissionScope.DEDICATED,
    )
    member = SimpleNamespace(
        host="dedicated.internal",
        port=3306,
        lifecycle_username_ciphertext="lifecycle",
        lifecycle_password_ciphertext="lifecycle-password",
        sandbox_username_ciphertext="agentic",
        sandbox_password_ciphertext="sandbox-password",
        database_name="agentic",
        permission_snapshot_json=permission_snapshot_to_json(snapshot),
    )
    with patch(
        "server.core.dedicated_mysql.decrypt", side_effect=lambda value: value
    ):
        await DedicatedMySQL(connector).synchronize_permissions(member, snapshot)

    revoke_index = statements.index(
        "REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'agentic'@'%'"
    )
    grant_index = statements.index(
        "GRANT SELECT ON `agentic`.* TO 'agentic'@'%'"
    )
    assert revoke_index < grant_index
    assert "SELECT 1" in statements
