from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from server.models import Base, PermissionTemplate


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with AsyncSession(engine, expire_on_commit=False) as value:
        yield value
    await engine.dispose()


def test_default_template_has_expected_safe_privileges_in_stable_order():
    from server.core.permission_template_service import (
        DEFAULT_DATABASE_PRIVILEGES,
        DatabasePrivilege,
        PermissionScope,
        default_permission_snapshot,
    )

    assert DEFAULT_DATABASE_PRIVILEGES == (
        DatabasePrivilege.CREATE,
        DatabasePrivilege.DROP,
        DatabasePrivilege.ALTER,
        DatabasePrivilege.INDEX,
        DatabasePrivilege.REFERENCES,
        DatabasePrivilege.CREATE_VIEW,
        DatabasePrivilege.SHOW_VIEW,
        DatabasePrivilege.CREATE_ROUTINE,
        DatabasePrivilege.ALTER_ROUTINE,
        DatabasePrivilege.EXECUTE,
        DatabasePrivilege.SELECT,
        DatabasePrivilege.INSERT,
        DatabasePrivilege.UPDATE,
        DatabasePrivilege.DELETE,
        DatabasePrivilege.CREATE_TEMPORARY_TABLES,
        DatabasePrivilege.LOCK_TABLES,
    )
    snapshot = default_permission_snapshot(PermissionScope.MULTITENANT)
    assert snapshot.privileges == DEFAULT_DATABASE_PRIVILEGES
    assert DatabasePrivilege.CREATE_USER not in snapshot.privileges
    assert snapshot.grant_option is False
    assert snapshot.legacy_all_privileges is False


def test_compiler_reorders_and_deduplicates_admin_selected_privileges():
    from server.core.permission_template_service import (
        DatabasePrivilege,
        PermissionScope,
        compile_permission_snapshot,
    )

    snapshot = compile_permission_snapshot(
        revision_id="ptr-custom-v1",
        template_id="pt-custom",
        privileges_json=json.dumps(
            ["SELECT", "CREATE USER", "SELECT", "DROP"]
        ),
        grant_option=True,
        scope=PermissionScope.DEDICATED,
    )

    assert snapshot.privileges == (
        DatabasePrivilege.DROP,
        DatabasePrivilege.SELECT,
        DatabasePrivilege.CREATE_USER,
    )
    assert snapshot.grant_option is True


@pytest.mark.parametrize(
    "payload",
    [
        '"SELECT"',
        '["ALL PRIVILEGES"]',
        '["SELECT", 1]',
        '["UNKNOWN"]',
    ],
)
def test_compiler_rejects_raw_or_unknown_privilege_payloads(payload: str):
    from server.core.permission_template_service import (
        PermissionScope,
        compile_permission_snapshot,
    )

    with pytest.raises(ValueError, match="privilege"):
        compile_permission_snapshot(
            revision_id="ptr-invalid",
            template_id="pt-invalid",
            privileges_json=payload,
            grant_option=False,
            scope=PermissionScope.DEDICATED,
        )


def test_multitenant_scope_rejects_global_account_administration():
    from server.core.permission_template_service import (
        PermissionScope,
        compile_permission_snapshot,
    )

    with pytest.raises(ValueError, match="Dedicated scope"):
        compile_permission_snapshot(
            revision_id="ptr-global",
            template_id="pt-global",
            privileges_json='["CREATE USER"]',
            grant_option=False,
            scope=PermissionScope.MULTITENANT,
        )


def test_snapshot_json_round_trip_preserves_legacy_contract():
    from server.core.permission_template_service import (
        PermissionScope,
        legacy_permission_snapshot,
        permission_snapshot_from_json,
        permission_snapshot_to_json,
    )

    original = legacy_permission_snapshot(PermissionScope.MULTITENANT)
    encoded = permission_snapshot_to_json(original)
    decoded = permission_snapshot_from_json(encoded)

    assert encoded == (
        '{"grant_option":true,"legacy":true,'
        '"privileges":["ALL PRIVILEGES"],"scope":"tenant"}'
    )
    assert decoded == original


async def test_template_edits_create_immutable_incrementing_revisions(
    session: AsyncSession,
):
    from server.core.permission_template_service import (
        create_permission_template_revision,
    )

    template = PermissionTemplate(name="custom-template")
    session.add(template)
    await session.commit()

    first = await create_permission_template_revision(
        session,
        template_id=template.id,
        privileges=["SELECT"],
        grant_option=False,
        created_by_user_id=None,
    )
    second = await create_permission_template_revision(
        session,
        template_id=template.id,
        privileges=["DROP", "SELECT"],
        grant_option=True,
        created_by_user_id=None,
    )

    await session.refresh(first)
    assert first.revision == 1
    assert first.privileges_json == '["SELECT"]'
    assert first.grant_option is False
    assert second.revision == 2
    assert second.privileges_json == '["DROP","SELECT"]'
    assert second.grant_option is True
